"""API engine stage s8 and the tenant API it deploys."""

import json

import pytest
from conftest import new_client, run_all, small_payloads

NO_FAULTS = {"x-fault-injection": "off"}


@pytest.fixture(scope="module")
def env(generated):
    client, run, resp = generated
    creds = client.get("/factory/runs/test-small/credentials").json()
    tok = client.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                            "client_secret": creds["client_secret"]}).json()["access_token"]
    return client, run, resp["s8"].json(), {"Authorization": f"Bearer {tok}", **NO_FAULTS}


def test_contract_tests_pass(env):
    _, _, s8, _ = env
    assert s8["status"] == "DEPLOYED"
    assert s8["contract_tests"] == {"total": 24, "passed": 24}
    assert s8["rate_limit"]["per_client_rpm"] == 600  # from the s8 payload


def test_only_payload_use_cases_are_exposed(env):
    _, run, _, _ = env
    spec = json.loads(run.stores.blobs.get("api/openapi.json"))
    assert "/v1/claims/{claim_id}/status" in spec["paths"]
    assert not any("appeals" in p or "benefits" in p for p in spec["paths"])
    tools = json.loads(run.stores.blobs.get("api/tools.json"))
    assert {t["name"] for t in tools} == {"get_claim_status", "get_authorization_status", "submit_authorization_document",
                                          "verify_eligibility", "verify_identity", "search_knowledge"}


def test_responses_are_marked_synthetic(env):
    client, run, _, auth = env
    c = run.stores.tables.query("SELECT h.claim_number, p.npi, p.tin FROM claim_header h JOIN provider p ON p.npi = h.billing_npi "
                                "WHERE h.status = 'DENIED' LIMIT 1").to_pylist()[0]
    r = client.get(f"/v1/claims/{c['claim_number']}/status?npi={c['npi']}&tin={c['tin']}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["is_synthetic"] is True and body["scenario_id"]
    assert body["denial"]["liable_party"] == "PROVIDER" and body["member_resp"] == "0.00"


def test_document_upload_resumes_clock(env):
    client, run, _, auth = env
    row = run.stores.tables.query(
        "SELECT a.auth_number, d.doc_type, p.npi, p.tin FROM documentation_requirement d JOIN auth_request a ON a.auth_id = d.auth_id "
        "JOIN provider p ON p.npi = a.requesting_npi WHERE d.state = 'REQUESTED' LIMIT 1").to_pylist()
    if not row:
        pytest.skip("no pended auth in this small dataset")
    x = row[0]
    r = client.post(f"/v1/authorizations/{x['auth_number']}/documents",
                    json={"doc_type": x["doc_type"], "document_ref": "doc-1", "npi": x["npi"], "tin": x["tin"]}, headers=auth)
    assert r.status_code == 200 and r.json()["clock_resumed"]
    status = client.get(f"/v1/authorizations/{x['auth_number']}/status?npi={x['npi']}&tin={x['tin']}", headers=auth).json()
    assert all(d["doc_type"] != x["doc_type"] for d in status["documents_required"])


def test_minimum_necessary_blocks_bulk_lookups(env):
    client, run, _, auth = env
    prov = run.stores.tables.query("SELECT npi, tin FROM provider WHERE status = 'ACTIVE' LIMIT 1").to_pylist()[0]
    members = run.stores.tables.query("SELECT member_number FROM member_demographics ORDER BY member_number LIMIT 25").column(0).to_pylist()
    codes = [client.post("/v1/eligibility/verify", json={**prov, "date_of_service": "2026-10-01", "member_id": m}, headers=auth).status_code
             for m in members]
    assert 429 in codes  # cap is 20 distinct members per 5 minutes per client
    assert codes.count(200) <= 20 and codes[0] == 200


def test_fault_injection_from_payload():
    """error_rate_pct=100 in the s8 payload: contract tests (fault injection off) pass, normal calls get 503."""
    client, _ = new_client()
    p = small_payloads(200, tenant="faulty")
    p["s8"]["fault_injection"] = {"error_rate_pct": 100, "latency_p95_ms": 0}
    resp = run_all(client, p)
    assert resp["s8"].json()["status"] == "DEPLOYED"
    assert client.get("/v1/knowledge/search?q=deductible").status_code == 503


def test_unimplemented_use_case_is_rejected():
    client, _ = new_client()
    p = small_payloads(100, tenant="mbn")
    run_all(client, p, api=False)
    r = client.post("/factory/api-engine/s8", json={**p["s8"], "use_cases": ["PCS", "MBN"], "endpoints": None})
    assert r.status_code == 422 and r.json()["error"] == "USE_CASES_NOT_IMPLEMENTED"
