"""Deployer port for API engine stage s8: where the API backend runs.

local     -> the FastAPI app is served by uvicorn in this process (see `factory up`)
cloud_run -> Cloud Run service from the pinned image + API Gateway (not implemented in v1)
"""

from __future__ import annotations

from dataclasses import dataclass

from factory.config import AdapterConfig


@dataclass
class Deployment:
    gateway_url: str
    revision: str
    target: str


class LocalDeployer:
    def __init__(self, cfg: AdapterConfig) -> None:
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 8080))

    def deploy(self, tenant_id: str, base_path: str) -> Deployment:
        # Nothing to provision: the CLI serves the app in-process after s8 completes.
        return Deployment(
            gateway_url=f"http://{self.host}:{self.port}{base_path}",
            revision="local-00001",
            target="in-process uvicorn",
        )

    def health(self) -> str:
        return f"ok (local, {self.host}:{self.port})"


class CloudRunDeployer:
    impl_note = (
        "google-cloud-run: deploy pinned api-backend image with FACTORY_PROFILE=gcp, tenant blueprint "
        "and s2 output refs; google-cloud-api-gateway: API config from openapi (check OpenAPI 2.0 requirement)"
    )

    def __init__(self, cfg: AdapterConfig) -> None:
        self.cfg = cfg

    def deploy(self, tenant_id: str, base_path: str) -> Deployment:
        raise NotImplementedError(f"CloudRunDeployer is not implemented in v1: {self.impl_note}")

    def health(self) -> str:
        return f"not implemented in v1 ({self.impl_note})"
