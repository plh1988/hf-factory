"""REST control plane: trigger Data Engine stages s3-s7 and API Engine stage s8 one call at a time.

  POST /factory/data-engine/s3 .. /s7    body = the stage job payload from the spec
  POST /factory/api-engine/s8            body = the s8 job payload from the spec
  GET  /factory/runs[/{tenant}]          stage status per tenant
  GET  /factory/runs/{tenant}/manifest   manifest after s7
  GET  /factory/runs/{tenant}/rule-report
  DELETE /factory/runs/{tenant}          drop the tenant's in-memory run and start over
  GET/POST /factory/blueprints           list or register stub blueprints

After s8 succeeds, the tenant's business API (/oauth/token, /v1/..., /docs) is served by this same server.
Control-plane docs: /factory/docs. Local only: bind to 127.0.0.1 (no auth on /factory in v1).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from factory.api_engine.stage import run_api_engine
from factory.contracts import Blueprint
from factory.control.registry import JobRejected, Registry, validate_api_job, validate_data_job
from factory.data_engine.runner import STAGES, DataEngineError, run_stage

EXAMPLE_NOTE = "Paste the stage payload from the architecture spec (scenario S1). See tests/fixtures/s1_jobs/."


def _rejected(exc: JobRejected) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, "details": exc.details}, status_code=exc.status)


def create_control_app(registry: Registry) -> FastAPI:
    app = FastAPI(title="Healthcare AI Platform Factory - engine control plane", version="0.2.0",
                  description=__doc__, docs_url="/factory/docs", openapi_url="/factory/openapi.json", redoc_url=None)
    app.state.registry = registry

    def unmount_tenant_api() -> None:
        app.router.routes[:] = [r for r in app.router.routes if not (isinstance(r, Mount) and r.name == "tenant-api")]

    def mount_tenant_api(tenant_app: FastAPI) -> None:
        unmount_tenant_api()
        app.router.routes.append(Mount("/", app=tenant_app, name="tenant-api"))

    # ------------------------------------------------------------ data engine stages
    def data_stage(stage: str):
        job_name = STAGES[stage][0]

        def handler(payload: dict[str, Any] = Body(..., description=EXAMPLE_NOTE),
                    tenant: str | None = Query(None, description="Override tenant resolution")) -> Any:
            with registry.lock:
                try:
                    tid = registry.resolve_tenant(payload, tenant)
                    run = registry.run_for(tid)
                    notes = validate_data_job(stage, payload, run)
                except JobRejected as exc:
                    return _rejected(exc)
                if payload.get("shards"):
                    run.ctx.shards = int(payload["shards"])
                run.stores.blobs.put(f"jobs/{stage}.json", json.dumps(payload, indent=2).encode(), "application/json")
                try:
                    res = run_stage(run.ctx, stage, rules=payload.get("rules"), rule_sets=payload.get("rule_sets"),
                                    privacy_options=payload.get("privacy_scan"), run=run.data if stage == "s7" else None)
                except DataEngineError as exc:
                    run.completed.pop(stage, None)
                    return JSONResponse({"error": "STAGE_FAILED", "message": str(exc), "stage": stage, "tenant": tid},
                                        status_code=500)
                run.completed[stage] = res
                # a re-run of an earlier stage invalidates the API deployment until s7/s8 are run again
                if stage != "s7":
                    run.completed.pop("s7", None)
                    if run.api is not None:
                        unmount_tenant_api()
                    run.api = None
                run.updated_at = time.time()
                registry.active = tid
                body = res.model_dump(mode="json")
                if stage == "s7":
                    requested = payload.get("consumes") or {}
                    for k in ("entity_count", "total_rows"):
                        if k in requested:
                            notes.append(f"consumes.{k}: payload {requested[k]}, generated {body.get(k)} "
                                         "(spec figures are illustrative estimates).")
                return {"job_id": f"job_{uuid.uuid4().hex[:12]}", "tenant": tid, "job": job_name, **body,
                        "duration_min": round(res.duration_s / 60, 2), "notes": notes,
                        "row_id_note": "IDs are UUIDv5 from (dataset_id, entity, seed, row_index)",
                        "next_stage": run.status()["next_stage"]}

        handler.__name__ = f"run_{stage}_{job_name}"
        return handler

    for stage, (job_name, _, _) in STAGES.items():
        app.post(f"/factory/data-engine/{stage}", tags=["Data engine"], summary=f"{stage}: {job_name}",
                 operation_id=f"run_{stage}")(data_stage(stage))

    # ------------------------------------------------------------ api engine
    @app.post("/factory/api-engine/s8", tags=["API engine"], summary="s8: api_generate_and_deploy", operation_id="run_s8")
    def run_s8(payload: dict[str, Any] = Body(..., description=EXAMPLE_NOTE),
               tenant: str | None = Query(None, description="Override tenant resolution")) -> Any:
        with registry.lock:
            try:
                tid = registry.resolve_tenant(payload, tenant)
                run = registry.run_for(tid)
                notes = validate_api_job(payload, run)
            except JobRejected as exc:
                return _rejected(exc)
            run.stores.blobs.put("jobs/s8.json", json.dumps(payload, indent=2).encode(), "application/json")
            api = run_api_engine(run.bp, run.stores, run.data.manifest_uri, job=payload)
            run.api = api
            run.updated_at = time.time()
            registry.active = tid
            if api.result.status == "DEPLOYED":
                mount_tenant_api(api.app)
            body = api.result.model_dump(mode="json")
            failed = [t for t in api.contract_tests if not t["passed"]]
            return JSONResponse({"job_id": f"job_{uuid.uuid4().hex[:12]}", "tenant": tid, "job": "api_generate_and_deploy",
                                 **body, "duration_min": round(api.result.duration_s / 60, 2), "notes": notes,
                                 "failed_contract_tests": failed,
                                 "api_live": api.result.status == "DEPLOYED",
                                 "try_it": {"swagger": body["gateway_url"].rsplit("/v1", 1)[0] + "/docs",
                                            "credentials": f"GET /factory/runs/{tid}/credentials (local profiles only)"}},
                                status_code=200 if api.result.status == "DEPLOYED" else 500)

    # ------------------------------------------------------------ status and artifacts
    @app.get("/factory/runs", tags=["Runs"])
    def list_runs() -> dict[str, Any]:
        return {"active": registry.active, "runs": [r.status() for r in registry.runs.values()],
                "blueprints_without_run": sorted(set(registry.blueprints) - set(registry.runs))}

    def _run(tenant: str):
        if tenant not in registry.runs:
            raise JobRejected(404, "NO_RUN", f"No run for tenant {tenant!r}. Start with POST /factory/data-engine/s3.")
        return registry.runs[tenant]

    @app.get("/factory/runs/{tenant}", tags=["Runs"])
    def get_run(tenant: str) -> Any:
        try:
            run = _run(tenant)
        except JobRejected as exc:
            return _rejected(exc)
        return {**run.status(), "results": {s: r.model_dump(mode="json") for s, r in run.completed.items()},
                "s8": run.api.result.model_dump(mode="json") if run.api else None}

    @app.get("/factory/runs/{tenant}/manifest", tags=["Runs"])
    def get_manifest(tenant: str) -> Any:
        try:
            run = _run(tenant)
            if run.data.manifest is None:
                raise JobRejected(404, "NO_MANIFEST", "Run s7 first.")
        except JobRejected as exc:
            return _rejected(exc)
        return run.data.manifest.model_dump(mode="json")

    @app.get("/factory/runs/{tenant}/rule-report", tags=["Runs"])
    def get_rule_report(tenant: str) -> Any:
        try:
            run = _run(tenant)
            data = run.stores.blobs.get("reports/rule-report.json")
        except (JobRejected, KeyError):
            return _rejected(JobRejected(404, "NO_REPORT", "Run s7 first."))
        return json.loads(data)

    @app.get("/factory/runs/{tenant}/credentials", tags=["Runs"],
             summary="Local test credentials for the tenant API (memory profiles only)")
    def get_credentials(tenant: str) -> Any:
        try:
            run = _run(tenant)
            if run.api is None:
                raise JobRejected(404, "NOT_DEPLOYED", "Run s8 first.")
            if not run.stores.profile.startswith("memory"):
                raise JobRejected(403, "USE_SECRET_MANAGER", "Read the secret from Secret Manager in cloud profiles.")
        except JobRejected as exc:
            return _rejected(exc)
        creds = json.loads(run.stores.blobs.get("secrets/api-client.json"))
        return {**creds, "token_url": "/oauth/token", "grant_type": "client_credentials"}

    @app.delete("/factory/runs/{tenant}", tags=["Runs"], summary="Drop a tenant's in-memory run")
    def delete_run(tenant: str) -> Any:
        with registry.lock:
            run = registry.runs.get(tenant)
            if run and run.api is not None:
                unmount_tenant_api()
            return {"tenant": tenant, "deleted": registry.reset(tenant)}

    @app.get("/factory/blueprints", tags=["Blueprints"])
    def list_blueprints() -> dict[str, Any]:
        return {"blueprints": [{"tenant": t, "blueprint_id": b.blueprint_id, "blueprint_hash": b.blueprint_hash,
                                "use_cases": b.scope.use_cases, "members": b.data_spec.volumes.members}
                               for t, b in sorted(registry.blueprints.items())]}

    @app.post("/factory/blueprints", tags=["Blueprints"], status_code=201,
              summary="Register a stub blueprint (stands in for the Blueprint Engine)")
    def register_blueprint(bp: Blueprint) -> Any:
        try:
            registry.register(bp)
        except JobRejected as exc:
            return _rejected(exc)
        return {"tenant": bp.tenant.id, "blueprint_id": bp.blueprint_id, "blueprint_hash": bp.blueprint_hash,
                "data_spec_hash": bp.data_spec_hash}

    @app.get("/factory/healthz", tags=["Ops"], include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "profile": registry.profile.name, "blueprints": sorted(registry.blueprints)}

    return app
