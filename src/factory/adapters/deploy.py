"""Deployer port for API engine stage s8: where the tenant API runs.

local      the FastAPI app is mounted in this process (the control plane serves it after s8)
cloud_run  a Cloud Run service per tenant runs `factory api --tenant <id>` from the same image (it loads the s8 job
           and the s7 manifest from the BlobStore), then API Gateway is put in front of it. API Gateway accepts
           OpenAPI 2.0, so the generated 3.1 document is converted (openapi3_to_swagger2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from factory.config import AdapterConfig

log = logging.getLogger("factory.deploy")


@dataclass
class Deployment:
    gateway_url: str
    revision: str
    target: str


class LocalDeployer:
    def __init__(self, cfg: AdapterConfig) -> None:
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 8080))

    def deploy(self, tenant_id: str, base_path: str, openapi: dict | None = None) -> Deployment:
        # Nothing to provision: the control plane mounts the app in-process after s8 completes.
        return Deployment(gateway_url=f"http://{self.host}:{self.port}{base_path}", revision="local-00001", target="in-process uvicorn")

    def health(self) -> str:
        return f"ok (local, {self.host}:{self.port})"


# ---------------------------------------------------------------- OpenAPI 3.x -> 2.0 for API Gateway
def _ref2(ref: str) -> str:
    return ref.replace("#/components/schemas/", "#/definitions/")


def _schema2(s: Any) -> Any:
    """Swagger 2.0 schema: refs rewritten, anyOf-with-null flattened (2.0 has no nullable/anyOf)."""
    if isinstance(s, dict):
        if "anyOf" in s:
            options = [o for o in s["anyOf"] if o.get("type") != "null"]
            base = _schema2(options[0]) if len(options) == 1 else {"type": "object"}
            return {**{k: v for k, v in s.items() if k not in ("anyOf",)}, **base}
        return {k: (_ref2(v) if k == "$ref" else _schema2(v)) for k, v in s.items() if k not in ("examples", "const")}
    if isinstance(s, list):
        return [_schema2(x) for x in s]
    return s


def openapi3_to_swagger2(spec: dict, backend_url: str, title: str) -> dict:
    """Minimal OpenAPI 3.x -> Swagger 2.0 for API Gateway. The Authorization header is passed through to the
    backend (disable_auth), where the OAuth2 client-credentials check runs."""
    paths: dict[str, Any] = {}
    for path, ops in spec.get("paths", {}).items():
        out_ops = {}
        for method, op in ops.items():
            params = []
            for p in op.get("parameters", []):
                schema = _schema2(p.get("schema", {}))
                params.append({"name": p["name"], "in": p["in"], "required": p.get("required", p["in"] == "path"),
                               "type": schema.get("type", "string"), **({"format": schema["format"]} if "format" in schema else {})})
            content = op.get("requestBody", {}).get("content", {})
            if "application/json" in content:
                params.append({"name": "body", "in": "body", "required": op["requestBody"].get("required", False),
                               "schema": _schema2(content["application/json"].get("schema", {}))})
            elif "application/x-www-form-urlencoded" in content:
                schema = content["application/x-www-form-urlencoded"].get("schema", {})
                props = spec.get("components", {}).get("schemas", {}).get(schema.get("$ref", "").split("/")[-1], {}).get("properties", {})
                params += [{"name": n, "in": "formData", "type": "string"} for n in props]
            responses = {code: {"description": r.get("description", "")} for code, r in op.get("responses", {}).items()}
            out_ops[method] = {"operationId": op.get("operationId", f"{method}_{re.sub(r'[^a-z0-9]+', '_', path.lower())}"),
                               "summary": op.get("summary", ""), "parameters": params, "responses": responses or {"200": {"description": "OK"}}}
            if "application/x-www-form-urlencoded" in content:
                out_ops[method]["consumes"] = ["application/x-www-form-urlencoded"]
        paths[path] = out_ops
    return {
        "swagger": "2.0",
        "info": {"title": title, "version": spec.get("info", {}).get("version", "1.0.0")},
        "schemes": ["https"],
        "produces": ["application/json"],
        "x-google-backend": {"address": backend_url, "path_translation": "APPEND_PATH_TO_ADDRESS", "disable_auth": True},
        "paths": paths,
        "definitions": {k: _schema2(v) for k, v in spec.get("components", {}).get("schemas", {}).items()},
    }


class CloudRunDeployer:
    """Profile keys: project, region, image, service_account, gateway (true|false), gateway_service_account."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.cfg = cfg
        self.project, self.region = cfg.get("project"), cfg.get("region", "us-central1")

    def _deploy_service(self, tenant_id: str) -> tuple[str, str]:
        from google.api_core.exceptions import NotFound
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        parent = f"projects/{self.project}/locations/{self.region}"
        sid = re.sub(r"[^a-z0-9-]", "-", f"api-{tenant_id}".lower())[:49]
        name = f"{parent}/services/{sid}"
        container = run_v2.Container(
            image=self.cfg.get("image"), args=["api", "--tenant", tenant_id],
            env=[run_v2.EnvVar(name="FACTORY_PROFILE", value="gcp")]
            + ([run_v2.EnvVar(name="FACTORY_S2_JSON", value=self.cfg.get("s2_json"))] if self.cfg.get("s2_json") else []),
            ports=[run_v2.ContainerPort(container_port=8080)],
        )
        template = run_v2.RevisionTemplate(containers=[container], service_account=self.cfg.get("service_account"),
                                           scaling=run_v2.RevisionScaling(min_instance_count=0, max_instance_count=int(self.cfg.get("max_instances", 10))))
        try:
            existing = client.get_service(name=name)
            existing.template = template
            service = client.update_service(service=existing).result()
        except NotFound:
            service = client.create_service(parent=parent, service_id=sid, service=run_v2.Service(template=template)).result()
        log.info("Cloud Run service %s ready at %s (revision %s)", sid, service.uri, service.latest_ready_revision)
        return service.uri, service.latest_ready_revision.split("/")[-1]

    def _deploy_gateway(self, tenant_id: str, backend_url: str, openapi: dict) -> str:
        from google.api_core.exceptions import NotFound
        from google.cloud import apigateway_v1

        gw = apigateway_v1.ApiGatewayServiceClient()
        api_id = re.sub(r"[^a-z0-9-]", "-", f"hf-{tenant_id}".lower())[:63]
        parent = f"projects/{self.project}/locations/global"
        try:
            gw.get_api(name=f"{parent}/apis/{api_id}")
        except NotFound:
            gw.create_api(parent=parent, api_id=api_id, api=apigateway_v1.Api(display_name=api_id)).result()
        swagger = openapi3_to_swagger2(openapi, backend_url, f"{tenant_id} healthcare API (synthetic)")
        config_id = "cfg-" + hashlib.sha256(json.dumps(swagger, sort_keys=True).encode()).hexdigest()[:12]  # same spec -> same config
        doc = apigateway_v1.ApiConfig.OpenApiDocument(document=apigateway_v1.ApiConfig.File(
            path="openapi.yaml", contents=json.dumps(swagger).encode()))
        config_name = f"{parent}/apis/{api_id}/configs/{config_id}"
        try:
            gw.get_api_config(name=config_name)
        except NotFound:
            gw.create_api_config(parent=f"{parent}/apis/{api_id}", api_config_id=config_id, api_config=apigateway_v1.ApiConfig(
                openapi_documents=[doc], gateway_service_account=self.cfg.get("gateway_service_account"))).result()
        gw_parent = f"projects/{self.project}/locations/{self.region}"
        gw_name = f"{gw_parent}/gateways/{api_id}"
        try:
            gateway = gw.get_gateway(name=gw_name)
            gateway.api_config = config_name
            gateway = gw.update_gateway(gateway=gateway).result()
        except NotFound:
            gateway = gw.create_gateway(parent=gw_parent, gateway_id=api_id,
                                        gateway=apigateway_v1.Gateway(api_config=config_name)).result()
        return f"https://{gateway.default_hostname}"

    def deploy(self, tenant_id: str, base_path: str, openapi: dict | None = None) -> Deployment:
        uri, revision = self._deploy_service(tenant_id)
        if self.cfg.get("gateway", True) and openapi:
            host = self._deploy_gateway(tenant_id, uri, openapi)
            return Deployment(gateway_url=f"{host}{base_path}", revision=revision, target=f"API Gateway -> Cloud Run {uri}")
        return Deployment(gateway_url=f"{uri}{base_path}", revision=revision, target="Cloud Run (no gateway)")

    def health(self) -> str:
        return f"ok (Cloud Run {self.project}/{self.region}, image {self.cfg.get('image')})"
