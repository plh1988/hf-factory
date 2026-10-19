"""POST /factory/api-engine/s8: generate, test and deploy the tenant business API."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from factory.api_engine.stage import run_api_engine
from factory.contracts import ApiJob
from factory.control.deps import TenantApiMount, get_registry, get_tenant_api
from factory.control.registry import Registry
from factory.control.validation import validate_api_job

router = APIRouter(tags=["API engine"])


@router.post("/factory/api-engine/s8", summary="s8: api_generate_and_deploy", operation_id="run_s8")
def run_s8(job: ApiJob, tenant: str | None = Query(None, description="Override tenant resolution"),
           registry: Registry = Depends(get_registry), tenant_api: TenantApiMount = Depends(get_tenant_api)) -> Any:
    with registry.lock:
        tid = registry.resolve_tenant(job.tenant_hint(), tenant, "s8")
        run = registry.run_for(tid, "s8")
        notes = validate_api_job(job, run)
        run.stores.blobs.put("jobs/s8.json", job.model_dump_json(indent=2).encode(), "application/json")
        api = run_api_engine(job, run.stores, run.data.manifest, run.data.manifest_uri)
        run.api = api
        run.updated_at = time.time()
        registry.active = tid
        deployed = api.result.status == "DEPLOYED"
        if deployed:
            tenant_api.mount(api.app)
        body = api.result.model_dump(mode="json")
        return JSONResponse({"job_id": f"job_{uuid.uuid4().hex[:12]}", "tenant": tid, "job": "api_generate_and_deploy",
                             **body, "duration_min": round(api.result.duration_s / 60, 2), "notes": notes,
                             "failed_contract_tests": [t for t in api.contract_tests if not t["passed"]],
                             "api_live": deployed,
                             "try_it": {"swagger": (body["gateway_url"] or "").rsplit(job.base_path, 1)[0] + "/docs",
                                        "credentials": f"GET /factory/runs/{tid}/credentials (memory profiles only)"}},
                            status_code=200 if deployed else 500)
