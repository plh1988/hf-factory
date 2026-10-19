"""GET/DELETE /factory/runs/...: run status, manifest, rule report, row peeks, analytics and test credentials."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from factory.control.deps import TenantApiMount, get_registry, get_tenant_api
from factory.control.errors import JobRejected, RejectCode
from factory.control.registry import Registry
from factory.data_engine import catalog

router = APIRouter(tags=["Runs"])

# SQL valid in both DuckDB and BigQuery (aliases in GROUP BY, EXTRACT for dates)
CLAIMS_SUMMARY_SQL = {
    "by_status": "SELECT status, count(*) AS claims, sum(total_billed) AS billed, sum(total_paid) AS paid, "
                 "sum(member_resp) AS member_resp FROM claim_header GROUP BY status ORDER BY status",
    "denials_by_category": "SELECT denial_category, carc, count(*) AS denials FROM claim_denial "
                           "GROUP BY denial_category, carc ORDER BY denials DESC",
    "by_month": "SELECT EXTRACT(YEAR FROM service_from_dt) AS yr, EXTRACT(MONTH FROM service_from_dt) AS mon, "
                "count(*) AS claims, sum(total_paid) AS paid FROM claim_header GROUP BY yr, mon ORDER BY yr, mon",
    "appeals_by_outcome": "SELECT status, outcome, count(*) AS appeals FROM appeal_record GROUP BY status, outcome "
                          "ORDER BY status, outcome",
}


def _json(body: dict[str, Any]) -> JSONResponse:
    """Response for rows with dates and decimals (stringified)."""
    return JSONResponse(json.loads(json.dumps(body, default=str)))


@router.get("/factory/runs")
def list_runs(registry: Registry = Depends(get_registry)) -> dict[str, Any]:
    return {"active": registry.active, "runs": [r.status() for r in registry.runs.values()]}


@router.get("/factory/runs/{tenant}")
def get_run(tenant: str, registry: Registry = Depends(get_registry)) -> Any:
    run = registry.get(tenant)
    return {**run.status(), "run_spec": run.spec.model_dump(mode="json"),
            "results": {s: r.model_dump(mode="json") for s, r in run.completed.items()},
            "s8": run.api.result.model_dump(mode="json") if run.api else None}


@router.get("/factory/runs/{tenant}/manifest")
def get_manifest(tenant: str, registry: Registry = Depends(get_registry)) -> Any:
    run = registry.get(tenant)
    if run.data.manifest is None:
        raise JobRejected(RejectCode.NO_MANIFEST, "Run s7 first.")
    return run.data.manifest.model_dump(mode="json")


@router.get("/factory/runs/{tenant}/rule-report")
def get_rule_report(tenant: str, registry: Registry = Depends(get_registry)) -> Any:
    try:
        data = registry.get(tenant).stores.blobs.get("reports/rule-report.json")
    except (JobRejected, KeyError):
        raise JobRejected(RejectCode.NO_REPORT, "Run s7 first.") from None
    return json.loads(data)


@router.get("/factory/runs/{tenant}/tables/{entity}", summary="Peek at generated rows (find IDs for testing)")
def peek(tenant: str, entity: str, limit: int = Query(20, ge=1, le=500),
         scenario_id: str | None = Query(None, description="e.g. PCS-02"), registry: Registry = Depends(get_registry)) -> Any:
    run = registry.get(tenant)
    if entity not in catalog.PRIMARY_KEY or not run.stores.tables.exists(entity):
        raise JobRejected(RejectCode.NO_TABLE, f"No generated table {entity!r} for tenant {tenant!r}.")
    where, params = ("WHERE scenario_id = ?", [scenario_id]) if scenario_id else ("", [])
    rows = run.stores.tables.query(f'SELECT * FROM "{entity}" {where} ORDER BY 1 LIMIT ?', [*params, limit]).to_pylist()
    return _json({"entity": entity, "rows": rows})


@router.get("/factory/runs/{tenant}/analytics/claims-summary",
            summary="Claims analytics (BigQuery in the gcp profile, the in-memory tables locally)")
def claims_summary(tenant: str, registry: Registry = Depends(get_registry)) -> Any:
    run = registry.get(tenant)
    out: dict[str, Any] = {"tenant": tenant, "source": run.stores.analytics.uri}
    for name, sql in CLAIMS_SUMMARY_SQL.items():
        try:
            out[name] = run.stores.analytics.query(sql).to_pylist()
        except Exception as exc:  # e.g. the table was not generated for this scenario
            out[name] = {"unavailable": str(exc).splitlines()[0][:200]}
    return _json(out)


@router.get("/factory/runs/{tenant}/credentials", summary="Local test credentials for the tenant API (memory profiles)")
def get_credentials(tenant: str, registry: Registry = Depends(get_registry)) -> Any:
    run = registry.get(tenant)
    if run.api is None:
        raise JobRejected(RejectCode.NOT_DEPLOYED, "Run s8 first.")
    if not run.stores.profile.startswith("memory"):
        raise JobRejected(RejectCode.USE_SECRET_MANAGER, "Read the secret from Secret Manager in cloud profiles.")
    creds = json.loads(run.stores.blobs.get("secrets/api-client.json"))
    return {**creds, "token_url": "/oauth/token", "grant_type": "client_credentials"}


@router.delete("/factory/runs/{tenant}", summary="Drop a tenant's in-memory run")
def delete_run(tenant: str, registry: Registry = Depends(get_registry), tenant_api: TenantApiMount = Depends(get_tenant_api)) -> Any:
    with registry.lock:
        run = registry.runs.get(tenant)
        if run and run.api is not None:
            tenant_api.unmount()
        return {"tenant": tenant, "deleted": registry.reset(tenant)}
