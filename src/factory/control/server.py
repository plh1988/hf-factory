"""REST control plane: trigger Data Engine stages s3-s7 and API Engine stage s8, one call each.

  POST /factory/data-engine/s3 .. /s7    body = the stage job payload (architecture spec, scenario S1)
  POST /factory/api-engine/s8            body = the s8 job payload
  GET  /factory/runs[/{tenant}]          stage status per tenant
  GET  /factory/runs/{tenant}/manifest   manifest after s7
  GET  /factory/runs/{tenant}/rule-report
  GET  /factory/runs/{tenant}/tables/{entity}?limit=20&scenario_id=PCS-02   peek at generated rows
  GET  /factory/runs/{tenant}/credentials   local test credentials for the tenant API (memory profiles)
  DELETE /factory/runs/{tenant}          drop the tenant's in-memory run and start over

The engines use only their payloads; there is no blueprint. s3 starts a run (tenant from consumes.alloydb).
After s8 succeeds, the tenant's business API (/oauth/token, /v1/..., /docs) is served by this same server.
Control-plane docs: /factory/docs. Local only: bind to 127.0.0.1 (no auth on /factory in v1).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from factory.api_engine.stage import run_api_engine
from factory.contracts import ApiJob, DataJob
from factory.control.registry import JobRejected, Registry, validate_api_job, validate_data_job
from factory.data_engine import catalog
from factory.data_engine.runner import STAGES, DataEngineError, run_stage


def _rejected(exc: JobRejected) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message, "details": exc.details}, status_code=exc.status)


def create_control_app(registry: Registry) -> FastAPI:
    app = FastAPI(title="Healthcare AI Platform Factory - engine control plane", version="0.3.0",
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

        def handler(job: DataJob, tenant: str | None = Query(None, description="Override tenant resolution")) -> Any:
            with registry.lock:
                run = None
                try:
                    tid = registry.resolve_tenant(job.tenant_hint(), tenant, stage)
                    run = registry.run_for(tid, stage, job)
                    snapshot = run.spec.model_copy(deep=True)
                    try:
                        notes = validate_data_job(stage, job, run)
                    except JobRejected:
                        run.spec.__dict__.update(snapshot.__dict__)  # a rejected payload changes nothing
                        if stage == "s3" and not run.completed:
                            registry.reset(tid)
                        raise
                except JobRejected as exc:
                    return _rejected(exc)
                raw = job.model_dump(mode="json", exclude_none=True)
                run.stores.blobs.put(f"jobs/{stage}.json", json.dumps(raw, indent=2).encode(), "application/json")
                try:
                    res = run_stage(run.ctx, stage, rules=job.rules, rule_sets=job.rule_sets,
                                    privacy_options=job.privacy_scan, run=run.data if stage == "s7" else None)
                except DataEngineError as exc:
                    run.completed.pop(stage, None)
                    return JSONResponse({"error": "STAGE_FAILED", "message": str(exc), "stage": stage, "tenant": tid}, status_code=500)
                run.completed[stage] = res
                if stage != "s7":  # an earlier stage changed: validation and the API must run again
                    run.completed.pop("s7", None)
                    run.data.manifest = None
                    if run.api is not None:
                        unmount_tenant_api()
                    run.api = None
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

    for stage, (job_name, _, _) in STAGES.items():
        app.post(f"/factory/data-engine/{stage}", tags=["Data engine"], summary=f"{stage}: {job_name}",
                 operation_id=f"run_{stage}")(data_stage(stage))

    # ------------------------------------------------------------ api engine
    @app.post("/factory/api-engine/s8", tags=["API engine"], summary="s8: api_generate_and_deploy", operation_id="run_s8")
    def run_s8(job: ApiJob, tenant: str | None = Query(None, description="Override tenant resolution")) -> Any:
        with registry.lock:
            try:
                tid = registry.resolve_tenant(job.tenant_hint(), tenant, "s8")
                run = registry.run_for(tid, "s8")
                notes = validate_api_job(job, run)
            except JobRejected as exc:
                return _rejected(exc)
            run.stores.blobs.put("jobs/s8.json", job.model_dump_json(indent=2).encode(), "application/json")
            api = run_api_engine(job, run.stores, run.data.manifest, run.data.manifest_uri)
            run.api = api
            run.updated_at = time.time()
            registry.active = tid
            if api.result.status == "DEPLOYED":
                mount_tenant_api(api.app)
            body = api.result.model_dump(mode="json")
            return JSONResponse({"job_id": f"job_{uuid.uuid4().hex[:12]}", "tenant": tid, "job": "api_generate_and_deploy",
                                 **body, "duration_min": round(api.result.duration_s / 60, 2), "notes": notes,
                                 "failed_contract_tests": [t for t in api.contract_tests if not t["passed"]],
                                 "api_live": api.result.status == "DEPLOYED",
                                 "try_it": {"swagger": (body["gateway_url"] or "").rsplit(job.base_path, 1)[0] + "/docs",
                                            "credentials": f"GET /factory/runs/{tid}/credentials (memory profiles only)"}},
                                status_code=200 if api.result.status == "DEPLOYED" else 500)

    # ------------------------------------------------------------ status and artifacts
    def _run(tenant: str):
        if tenant not in registry.runs:
            raise JobRejected(404, "NO_RUN", f"No run for tenant {tenant!r}. Start with POST /factory/data-engine/s3.")
        return registry.runs[tenant]

    @app.get("/factory/runs", tags=["Runs"])
    def list_runs() -> dict[str, Any]:
        return {"active": registry.active, "runs": [r.status() for r in registry.runs.values()]}

    @app.get("/factory/runs/{tenant}", tags=["Runs"])
    def get_run(tenant: str) -> Any:
        try:
            run = _run(tenant)
        except JobRejected as exc:
            return _rejected(exc)
        return {**run.status(), "run_spec": run.spec.model_dump(mode="json"),
                "results": {s: r.model_dump(mode="json") for s, r in run.completed.items()},
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

    @app.get("/factory/runs/{tenant}/tables/{entity}", tags=["Runs"], summary="Peek at generated rows (find IDs for testing)")
    def peek(tenant: str, entity: str, limit: int = Query(20, ge=1, le=500),
             scenario_id: str | None = Query(None, description="e.g. PCS-02")) -> Any:
        try:
            run = _run(tenant)
            if entity not in catalog.PRIMARY_KEY or not run.stores.tables.exists(entity):
                raise JobRejected(404, "NO_TABLE", f"No generated table {entity!r} for tenant {tenant!r}.")
        except JobRejected as exc:
            return _rejected(exc)
        where, params = ("WHERE scenario_id = ?", [scenario_id]) if scenario_id else ("", [])
        rows = run.stores.tables.query(f'SELECT * FROM "{entity}" {where} ORDER BY 1 LIMIT {limit}', params).to_pylist()
        return JSONResponse(json.loads(json.dumps({"entity": entity, "rows": rows}, default=str)))

    # SQL valid in both DuckDB and BigQuery (aliases in GROUP BY, EXTRACT for dates)
    summary_sql = {
        "by_status": "SELECT status, count(*) AS claims, sum(total_billed) AS billed, sum(total_paid) AS paid, "
                     "sum(member_resp) AS member_resp FROM claim_header GROUP BY status ORDER BY status",
        "denials_by_category": "SELECT denial_category, carc, count(*) AS denials FROM claim_denial "
                               "GROUP BY denial_category, carc ORDER BY denials DESC",
        "by_month": "SELECT EXTRACT(YEAR FROM service_from_dt) AS yr, EXTRACT(MONTH FROM service_from_dt) AS mon, "
                    "count(*) AS claims, sum(total_paid) AS paid FROM claim_header GROUP BY yr, mon ORDER BY yr, mon",
        "appeals_by_outcome": "SELECT status, outcome, count(*) AS appeals FROM appeal_record GROUP BY status, outcome "
                              "ORDER BY status, outcome",
    }

    @app.get("/factory/runs/{tenant}/analytics/claims-summary", tags=["Runs"],
             summary="Claims analytics (BigQuery in the gcp profile, the in-memory tables locally)")
    def claims_summary(tenant: str) -> Any:
        try:
            run = _run(tenant)
        except JobRejected as exc:
            return _rejected(exc)
        out: dict[str, Any] = {"tenant": tenant, "source": run.stores.analytics.uri}
        for name, sql in summary_sql.items():
            try:
                out[name] = run.stores.analytics.query(sql).to_pylist()
            except Exception as exc:  # e.g. the table was not generated for this scenario
                out[name] = {"unavailable": str(exc).splitlines()[0][:200]}
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    @app.get("/factory/runs/{tenant}/credentials", tags=["Runs"], summary="Local test credentials for the tenant API (memory profiles)")
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

    @app.get("/factory/healthz", tags=["Ops"], include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "profile": registry.profile.name, "runs": sorted(registry.runs)}

    return app
