"""Stage s8: generate the API contract, publish OpenAPI + tool manifest, run contract tests, deploy."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory.adapters.ports import Stores
from factory.api_engine.app import create_app, tool_manifest
from factory.contracts import Blueprint, StageResult

NO_FAULTS = {"x-fault-injection": "off"}


@dataclass
class ApiEngineRun:
    app: FastAPI
    result: StageResult
    contract_tests: list[dict[str, Any]] = field(default_factory=list)


def _sample(stores: Stores, sql: str) -> dict | None:
    rows = stores.tables.query(sql).to_pylist()
    return rows[0] if rows else None


def contract_tests(app: FastAPI, bp: Blueprint, stores: Stores) -> list[dict[str, Any]]:
    """Four tests per enabled endpoint: happy path, not found / negative, no token, scenario or validation case."""
    client = TestClient(app)
    creds = json.loads(stores.blobs.get("secrets/api-client.json"))
    tok = client.post("/oauth/token", data={"grant_type": "client_credentials", **creds}).json()["access_token"]
    auth = {"Authorization": f"Bearer {tok}", **NO_FAULTS}
    base = bp.api_spec.base_path
    tests: list[tuple[str, str, Callable[[], Any], Callable[[Any], bool]]] = []

    def add(endpoint: str, name: str, call: Callable[[], Any], check: Callable[[Any], bool]) -> None:
        tests.append((endpoint, name, call, check))

    prov_of = "SELECT npi, tin FROM provider WHERE npi = '{npi}'"
    if "PCS" in bp.scope.use_cases:
        c = _sample(stores, "SELECT claim_number, billing_npi FROM claim_header WHERE status = 'PAID' ORDER BY claim_number")
        p = _sample(stores, prov_of.format(npi=c["billing_npi"]))
        pend = _sample(stores, "SELECT claim_number, billing_npi FROM claim_header WHERE scenario_id = 'PCS-02' ORDER BY claim_number")
        pp = _sample(stores, prov_of.format(npi=pend["billing_npi"])) if pend else p
        ep = f"GET {base}/claims/{{claim_id}}/status"
        q_pcs = f"?npi={p['npi']}&tin={p['tin']}"
        add(ep, "paid claim returns status and payment", lambda: client.get(f"{base}/claims/{c['claim_number']}/status{q_pcs}", headers=auth),
            lambda r: r.status_code == 200 and r.json()["status"] == "PAID" and r.json()["payment"] is not None)
        add(ep, "unknown claim is 404", lambda: client.get(f"{base}/claims/CLM-9999999-99/status{q_pcs}", headers=auth),
            lambda r: r.status_code == 404)
        add(ep, "no token is 401", lambda: client.get(f"{base}/claims/{c['claim_number']}/status{q_pcs}", headers=NO_FAULTS),
            lambda r: r.status_code == 401)
        add(ep, "PCS-04 wrong TIN is refused (403)", lambda: client.get(
            f"{base}/claims/{c['claim_number']}/status?npi={p['npi']}&tin=900000000", headers=auth),
            lambda r: r.status_code == 403 and r.json()["error"] == "VERIFICATION_FAILED")
        if pend:
            tests[-1] = (ep, "PCS-02 pended claim lists what is needed", lambda: client.get(
                f"{base}/claims/{pend['claim_number']}/status?npi={pp['npi']}&tin={pp['tin']}", headers=auth),
                lambda r: r.status_code == 200 and r.json()["pend_reason"] == "RECORDS_REQUEST" and r.json()["info_needed_from"] == "PROVIDER")

    if "PAS" in bp.scope.use_cases:
        a = _sample(stores, "SELECT auth_number, requesting_npi FROM auth_request WHERE scenario_id = 'PAS-01' ORDER BY auth_number") or \
            _sample(stores, "SELECT auth_number, requesting_npi FROM auth_request ORDER BY auth_number")
        ap = _sample(stores, prov_of.format(npi=a["requesting_npi"]))
        a2 = _sample(stores, "SELECT auth_number, requesting_npi FROM auth_request WHERE scenario_id = 'PAS-02' ORDER BY auth_number")
        a2p = _sample(stores, prov_of.format(npi=a2["requesting_npi"])) if a2 else None
        ep = f"GET {base}/authorizations/{{auth_id}}/status"
        q_pas = f"?npi={ap['npi']}&tin={ap['tin']}"
        add(ep, "status returns due time and review", lambda: client.get(f"{base}/authorizations/{a['auth_number']}/status{q_pas}", headers=auth),
            lambda r: r.status_code == 200 and "decision_due_dt" in r.json())
        add(ep, "unknown auth is 404", lambda: client.get(f"{base}/authorizations/AUTH-9999999-99/status{q_pas}", headers=auth),
            lambda r: r.status_code == 404)
        add(ep, "no token is 401", lambda: client.get(f"{base}/authorizations/{a['auth_number']}/status{q_pas}", headers=NO_FAULTS),
            lambda r: r.status_code == 401)
        if a2:
            add(ep, "PAS-02 urgent SNF shows discharge summary outstanding", lambda: client.get(
                f"{base}/authorizations/{a2['auth_number']}/status?npi={a2p['npi']}&tin={a2p['tin']}", headers=auth),
                lambda r: r.status_code == 200 and r.json()["urgency"] == "URGENT" and r.json()["clock_paused"]
                and r.json()["documents_required"][0]["doc_type"] == "DISCHARGE_SUMMARY")
        else:
            add(ep, "wrong TIN is refused (403)", lambda: client.get(f"{base}/authorizations/{a['auth_number']}/status?npi={ap['npi']}&tin=900000000", headers=auth),
                lambda r: r.status_code == 403)

        ep = f"POST {base}/authorizations/{{auth_id}}/documents"
        body = {"doc_type": "IMAGING", "document_ref": "doc-test", "npi": ap["npi"], "tin": ap["tin"]}
        add(ep, "document not requested is 409", lambda: client.post(f"{base}/authorizations/{a['auth_number']}/documents", json=body, headers=auth),
            lambda r: r.status_code == 409)
        add(ep, "unknown auth is 404", lambda: client.post(f"{base}/authorizations/AUTH-9999999-99/documents", json=body, headers=auth),
            lambda r: r.status_code == 404)
        add(ep, "no token is 401", lambda: client.post(f"{base}/authorizations/{a['auth_number']}/documents", json=body, headers=NO_FAULTS),
            lambda r: r.status_code == 401)
        add(ep, "invalid doc type is 422", lambda: client.post(f"{base}/authorizations/{a['auth_number']}/documents",
                                                               json={**body, "doc_type": "SELFIE"}, headers=auth),
            lambda r: r.status_code == 422)

    if "PEV" in bp.scope.use_cases:
        m = _sample(stores, "SELECT m.member_number FROM member_demographics m JOIN plan_enrollment e ON e.member_id = m.member_id "
                            "WHERE e.status = 'ACTIVE' AND m.scenario_id = 'PEV-01' ORDER BY m.member_number")
        m2 = _sample(stores, "SELECT member_number FROM member_demographics WHERE scenario_id = 'PEV-02' ORDER BY member_number")
        op = _sample(stores, "SELECT npi, tin FROM provider WHERE status = 'ACTIVE' ORDER BY npi")
        dos = str(bp.repeatability.as_of_date)
        ep = f"POST {base}/eligibility/verify"
        req = {"npi": op["npi"], "tin": op["tin"], "date_of_service": dos, "member_id": m["member_number"]}
        add(ep, "active member is eligible with cost share", lambda: client.post(f"{base}/eligibility/verify", json=req, headers=auth),
            lambda r: r.status_code == 200 and r.json()["eligible"] and r.json()["cost_share"] is not None)
        add(ep, "unknown member is 404", lambda: client.post(f"{base}/eligibility/verify", json={**req, "member_id": "MBR-9999999"}, headers=auth),
            lambda r: r.status_code == 404)
        add(ep, "no token is 401", lambda: client.post(f"{base}/eligibility/verify", json=req, headers=NO_FAULTS),
            lambda r: r.status_code == 401)
        if m2:
            add(ep, "PEV-02 retro termination warns, no coverage today", lambda: client.post(
                f"{base}/eligibility/verify", json={**req, "member_id": m2["member_number"]}, headers=auth),
                lambda r: r.status_code == 200 and not r.json()["eligible"])
        else:
            add(ep, "wrong TIN is 403", lambda: client.post(f"{base}/eligibility/verify", json={**req, "tin": "900000000"}, headers=auth),
                lambda r: r.status_code == 403)

    op = _sample(stores, "SELECT npi, tin FROM provider WHERE status = 'ACTIVE' ORDER BY npi")
    ep = f"POST {base}/identity/verify"
    add(ep, "provider NPI + TIN verifies", lambda: client.post(f"{base}/identity/verify", json={"caller_type": "PROVIDER", **op}, headers=auth),
        lambda r: r.status_code == 200 and r.json()["verified"])
    add(ep, "wrong TIN does not verify", lambda: client.post(f"{base}/identity/verify", json={"caller_type": "PROVIDER", "npi": op["npi"], "tin": "900000000"}, headers=auth),
        lambda r: r.status_code == 200 and not r.json()["verified"])
    add(ep, "no token is 401", lambda: client.post(f"{base}/identity/verify", json={"caller_type": "PROVIDER", **op}, headers=NO_FAULTS),
        lambda r: r.status_code == 401)
    add(ep, "bad caller type is 422", lambda: client.post(f"{base}/identity/verify", json={"caller_type": "ROBOT"}, headers=auth),
        lambda r: r.status_code == 422)

    ep = f"GET {base}/knowledge/search"
    add(ep, "search returns cited chunks", lambda: client.get(f"{base}/knowledge/search?q=prior authorization urgent decision", headers=auth),
        lambda r: r.status_code == 200 and len(r.json()["results"]) > 0)
    add(ep, "audience filter is applied", lambda: client.get(f"{base}/knowledge/search?q=deductible&audience=MEMBER", headers=auth),
        lambda r: r.status_code == 200 and all(h["audience"] == "MEMBER" for h in r.json()["results"]))
    add(ep, "no token is 401", lambda: client.get(f"{base}/knowledge/search?q=deductible", headers=NO_FAULTS),
        lambda r: r.status_code == 401)
    add(ep, "query too short is 422", lambda: client.get(f"{base}/knowledge/search?q=a", headers=auth),
        lambda r: r.status_code == 422)

    out = []
    for endpoint, name, call, check in tests:
        resp = call()
        ok = False
        try:
            ok = bool(check(resp))
        except Exception:  # a malformed body counts as a failure
            ok = False
        out.append({"endpoint": endpoint, "test": name, "status": resp.status_code, "passed": ok})
    return out


def run_api_engine(bp: Blueprint, stores: Stores, manifest_uri: str | None, job: dict[str, Any] | None = None) -> ApiEngineRun:
    """job = the s8 payload (rate_limit, fault_injection override the blueprint); None = blueprint defaults."""
    job = job or {}
    t0 = time.perf_counter()
    stores.events.publish("scenario-events", {"stage": "s8", "job": "api_generate_and_deploy", "state": "STARTED"})
    app = create_app(bp, stores, rate_limit_rpm=(job.get("rate_limit") or {}).get("per_client_rpm"),
                     fault_settings=job.get("fault_injection"))
    spec = app.openapi()
    openapi_uri = stores.blobs.put("api/openapi.json", json.dumps(spec, indent=2).encode(), "application/json")
    tools = tool_manifest(spec, bp.api_spec.base_path)
    tools_uri = stores.blobs.put("api/tools.json", json.dumps(tools, indent=2).encode(), "application/json")
    tests = contract_tests(app, bp, stores)
    passed = sum(t["passed"] for t in tests)
    deployment = stores.deployer.deploy(bp.tenant.id, bp.api_spec.base_path)
    result = StageResult(
        stage="s8", status="DEPLOYED" if passed == len(tests) else "FAILED", duration_s=round(time.perf_counter() - t0, 2),
        gateway_url=deployment.gateway_url, revision=deployment.revision, target=deployment.target,
        openapi_uri=openapi_uri, tool_manifest_uri=tools_uri, endpoint_count=len(tools), tools_published=len(tools),
        contract_tests={"total": len(tests), "passed": passed}, credentials_secret_ref=app.state.clients.secret_ref,
        rate_limit={"per_client_rpm": app.state.limiter.per_minute},
        fault_injection={**bp.api_spec.fault_injection.model_dump(), **(job.get("fault_injection") or {})},
        agent_tools=True,
        consumes={"tables": stores.tables.uri, "manifest": manifest_uri},
        error=None if passed == len(tests) else "contract tests failed",
    )
    stores.events.publish("scenario-events", {"stage": "s8", "state": result.status, "contract_tests": result.contract_tests})
    return ApiEngineRun(app=app, result=result, contract_tests=tests)
