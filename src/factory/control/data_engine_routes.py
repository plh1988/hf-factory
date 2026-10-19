"""POST /factory/data-engine/s3 .. s7: run one Data Engine stage from its job payload."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from factory.contracts import DataJob
from factory.control.deps import TenantApiMount, get_registry, get_tenant_api
from factory.control.errors import JobRejected, RejectCode
from factory.control.registry import Registry, Run
from factory.control.validation import validate_data_job
from factory.data_engine.runner import STAGES, DataEngineError, run_stage

router = APIRouter(tags=["Data engine"])


def _validate(registry: Registry, stage: str, job: DataJob, tenant: str | None) -> tuple[str, Run, list[str]]:
    """Resolve tenant and run, then validate the payload. A rejected payload leaves the run exactly as it was
    (and a rejected s3 that would have started a run leaves no run behind)."""
    tid = registry.resolve_tenant(job.tenant_hint(), tenant, stage)
    run = registry.run_for(tid, stage, job)
    snapshot = run.spec.model_copy(deep=True)
    try:
        return tid, run, validate_data_job(stage, job, run)
    except JobRejected:
        run.spec.__dict__.update(snapshot.__dict__)
        if stage == "s3" and not run.completed:
            registry.reset(tid)
        raise


def _invalidate_downstream(run: Run, tenant_api: TenantApiMount) -> None:
    """An earlier stage changed the data: validation (s7) and the deployed API (s8) must run again."""
    run.completed.pop("s7", None)
    run.data.manifest = None
    if run.api is not None:
        tenant_api.unmount()
    run.api = None


def stage_handler(stage: str) -> Callable[..., Any]:
    """The POST handler for one data-engine stage."""
    job_name = STAGES[stage].job

    def handler(job: DataJob, tenant: str | None = Query(None, description="Override tenant resolution"),
                registry: Registry = Depends(get_registry), tenant_api: TenantApiMount = Depends(get_tenant_api)) -> Any:
        with registry.lock:  # one job at a time: stages share in-memory state
            tid, run, notes = _validate(registry, stage, job, tenant)
            raw = job.model_dump(mode="json", exclude_none=True)
            run.stores.blobs.put(f"jobs/{stage}.json", json.dumps(raw, indent=2).encode(), "application/json")
            try:
                res = run_stage(run.ctx, stage, rules=job.rules, rule_sets=job.rule_sets,
                                privacy_options=job.privacy_scan, run=run.data if stage == "s7" else None)
            except DataEngineError as exc:
                run.completed.pop(stage, None)
                return JSONResponse({"error": RejectCode.STAGE_FAILED, "message": str(exc), "stage": stage, "tenant": tid},
                                    status_code=RejectCode.STAGE_FAILED.status)
            run.completed[stage] = res
            if stage != "s7":
                _invalidate_downstream(run, tenant_api)
            run.updated_at = time.time()
            registry.active = tid
            body = res.model_dump(mode="json")
            if stage == "s7":
                for k in ("entity_count", "total_rows"):
                    if k in job.consumes:
                        notes.append(f"consumes.{k}: payload {job.consumes[k]:,}, generated {body.get(k):,}.")
            return {"job_id": f"job_{uuid.uuid4().hex[:12]}", "tenant": tid, "job": job_name, **body,
                    "duration_min": round(res.duration_s / 60, 2), "notes": notes,
                    "row_id_note": "IDs are UUIDv5 from (dataset_id, entity, seed, row_index)",
                    "next_stage": run.status()["next_stage"]}

    handler.__name__ = f"run_{stage}_{job_name}"
    return handler


for _spec in STAGES.values():
    router.post(f"/factory/data-engine/{_spec.name}", summary=f"{_spec.name}: {_spec.job}", operation_id=f"run_{_spec.name}")(
        stage_handler(_spec.name))
