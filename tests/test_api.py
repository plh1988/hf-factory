"""API engine stage s8 and endpoint behaviour."""

import json

import pytest
from conftest import fresh_stores, small_blueprint
from fastapi.testclient import TestClient

from factory.api_engine.app import ApiEngineError, create_app
from factory.api_engine.stage import run_api_engine
from factory.data_engine.runner import run_data_engine

NO_FAULTS = {"x-fault-injection": "off"}


@pytest.fixture(scope="module")
def env():
    bp = small_blueprint()
    stores = fresh_stores()
    run = run_data_engine(bp, stores, shards=2)
    api = run_api_engine(bp, stores, run.manifest_uri)
    client = TestClient(api.app)
    creds = json.loads(stores.blobs.get("secrets/api-client.json"))
    tok = client.post("/oauth/token", data={"grant_type": "client_credentials", **creds}).json()["access_token"]
    return bp, stores, api, client, {"Authorization": f"Bearer {tok}", **NO_FAULTS}


def test_contract_tests_pass(env):
    _, _, api, _, _ = env
    assert api.result.status == "DEPLOYED"
    assert api.result.contract_tests["passed"] == api.result.contract_tests["total"] == 24


def test_only_blueprint_use_cases_are_exposed(env):
    _, stores, _, _, _ = env
    spec = json.loads(stores.blobs.get("api/openapi.json"))
    paths = set(spec["paths"])
    assert "/v1/claims/{claim_id}/status" in paths
    assert not any("appeals" in p or "benefits" in p for p in paths)
    tools = json.loads(stores.blobs.get("api/tools.json"))
    assert {t["name"] for t in tools} == {"get_claim_status", "get_authorization_status", "submit_authorization_document",
                                          "verify_eligibility", "verify_identity", "search_knowledge"}


def test_responses_are_marked_synthetic(env):
    _, stores, _, client, auth = env
    c = stores.tables.query("SELECT h.claim_number, p.npi, p.tin FROM claim_header h JOIN provider p ON p.npi = h.billing_npi "
                            "WHERE h.status = 'DENIED' LIMIT 1").to_pylist()[0]
    r = client.get(f"/v1/claims/{c['claim_number']}/status?npi={c['npi']}&tin={c['tin']}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["is_synthetic"] is True and body["scenario_id"]
    assert body["denial"]["liable_party"] == "PROVIDER" and body["member_resp"] == "0.00"


def test_document_upload_resumes_clock(env):
    _, stores, _, client, auth = env
    row = stores.tables.query(
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
    _, stores, _, client, auth = env
    prov = stores.tables.query("SELECT npi, tin FROM provider WHERE status = 'ACTIVE' LIMIT 1").to_pylist()[0]
    members = stores.tables.query("SELECT member_number FROM member_demographics ORDER BY member_number LIMIT 25").column(0).to_pylist()
    codes = [client.post("/v1/eligibility/verify", json={**prov, "date_of_service": "2026-10-01", "member_id": m}, headers=auth).status_code
             for m in members]
    assert 429 in codes  # cap is 20 distinct members per 5 minutes per client
    assert codes.count(200) <= 20 and codes[0] == 200


def test_fault_injection_returns_503():
    bp = small_blueprint(200, **{"api_spec.fault_injection.error_rate_pct": 100})
    stores = fresh_stores()
    run_data_engine(bp, stores, shards=1)
    client = TestClient(create_app(bp, stores))
    assert client.get("/v1/knowledge/search?q=deductible").status_code == 503


def test_unimplemented_use_case_fails_fast():
    bp = small_blueprint(100, **{"scope.use_cases": ["PCS", "MBN"]})
    with pytest.raises(ApiEngineError):
        create_app(bp, fresh_stores())
