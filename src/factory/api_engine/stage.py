"""Stage s8: generate the API contract, publish OpenAPI + tool manifest, run contract tests, deploy."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from factory.adapters.ports import Stores
from factory.api_engine.app import create_app, tool_manifest
from factory.api_engine.contract_tests import contract_tests
from factory.contracts import ApiJob, Manifest, StageResult



@dataclass
class ApiEngineRun:
    app: FastAPI
    result: StageResult
    contract_tests: list[dict[str, Any]] = field(default_factory=list)


def run_api_engine(job: ApiJob, stores: Stores, manifest: Manifest, manifest_uri: str | None) -> ApiEngineRun:
    """Stage s8 from its payload. Tenant, seed and as-of date come from the s7 manifest the job consumes."""
    t0 = time.perf_counter()
    stores.events.publish("scenario-events", {"stage": "s8", "job": "api_generate_and_deploy", "state": "STARTED"})
    app = create_app(job, stores, tenant_id=manifest.tenant_id, seed=manifest.root_seed, as_of=manifest.as_of_date)
    spec = app.openapi()
    openapi_uri = stores.blobs.put("api/openapi.json", json.dumps(spec, indent=2).encode(), "application/json")
    tools = tool_manifest(spec, job.base_path) if job.agent_tools else []
    tools_uri = stores.blobs.put("api/tools.json", json.dumps(tools, indent=2).encode(), "application/json") if job.agent_tools else None
    tests = contract_tests(app, job, stores, manifest.as_of_date.isoformat())
    passed = sum(t["passed"] for t in tests)
    deployment = stores.deployer.deploy(manifest.tenant_id, job.base_path, openapi=spec) if passed == len(tests) else None
    result = StageResult(
        stage="s8", status="DEPLOYED" if passed == len(tests) else "FAILED", duration_s=round(time.perf_counter() - t0, 2),
        gateway_url=deployment.gateway_url if deployment else None, revision=deployment.revision if deployment else None,
        target=deployment.target if deployment else "not deployed (contract tests failed)",
        openapi_uri=openapi_uri, tool_manifest_uri=tools_uri, endpoint_count=len(spec["paths"]) - 1, tools_published=len(tools),
        contract_tests={"total": len(tests), "passed": passed}, credentials_secret_ref=app.state.clients.secret_ref,
        rate_limit={"per_client_rpm": app.state.limiter.per_minute},
        fault_injection=job.fault_injection.model_dump(), agent_tools=job.agent_tools,
        consumes={"tables": stores.tables.uri, "manifest": manifest_uri},
        error=None if passed == len(tests) else "contract tests failed",
    )
    stores.events.publish("scenario-events", {"stage": "s8", "state": result.status, "contract_tests": result.contract_tests})
    return ApiEngineRun(app=app, result=result, contract_tests=tests)
