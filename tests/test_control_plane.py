"""REST control plane: the spec's S1 job payloads (tests/fixtures/s1_jobs) run stage by stage."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory.config import load_profile
from factory.control.registry import Registry
from factory.control.server import create_control_app

FIX = Path(__file__).parent / "fixtures" / "s1_jobs"
BLUEPRINTS = Path(__file__).resolve().parents[1] / "blueprints"


def payload(stage: str) -> dict:
    return json.loads((FIX / f"{stage}.json").read_text(encoding="utf-8"))


def new_client() -> TestClient:
    reg = Registry(load_profile("memory-offline"))
    reg.load_dir(BLUEPRINTS)
    return TestClient(create_control_app(reg))


@pytest.fixture(scope="module")
def full_run():
    """s3..s8 with the spec payloads, unchanged, for tenant ma-demo-01 (50,000 members)."""
    c = new_client()
    responses = {st: c.post(f"/factory/data-engine/{st}", json=payload(st)) for st in ["s3", "s4", "s5", "s6", "s7"]}
    responses["s8"] = c.post("/factory/api-engine/s8", json=payload("s8"))
    return c, responses


def test_spec_payloads_run_end_to_end(full_run):
    _, r = full_run
    for st in ["s3", "s4", "s5", "s6", "s7"]:
        assert r[st].status_code == 200, (st, r[st].json())
        assert r[st].json()["status"] == "COMPLETED"
        assert r[st].json()["tenant"] == "ma-demo-01"
    assert r["s7"].json()["gate"] == "data_quality_gate PASSED"
    assert r["s7"].json()["entity_count"] == payload("s7")["consumes"]["entity_count"]
    s8 = r["s8"].json()
    assert r["s8"].status_code == 200 and s8["status"] == "DEPLOYED"
    assert s8["contract_tests"] == {"total": 24, "passed": 24}
    assert s8["rate_limit"]["per_client_rpm"] == 600


def test_stage_rule_checks_match_payload(full_run):
    _, r = full_run
    assert set(r["s5"].json()["rule_checks"]) == set(payload("s5")["rules"])
    assert all(v in ("PASS", "PASS_PARTIAL", "DEFERRED_TO_S7") for st in ["s3", "s4", "s5", "s6"]
               for v in r[st].json()["rule_checks"].values())


def test_tenant_api_is_live_after_s8(full_run):
    c, _ = full_run
    creds = c.get("/factory/runs/ma-demo-01/credentials").json()
    tok = c.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                       "client_secret": creds["client_secret"]}).json()["access_token"]
    resp = c.get("/v1/knowledge/search?q=prior authorization", headers={"Authorization": f"Bearer {tok}", "x-fault-injection": "off"})
    assert resp.status_code == 200 and resp.json()["results"]


def test_rerun_is_idempotent(full_run):
    c, r = full_run
    again = c.post("/factory/data-engine/s3", json=payload("s3")).json()
    assert {e["entity"]: e["checksum"] for e in again["entities"]} == {e["entity"]: e["checksum"] for e in r["s3"].json()["entities"]}
    status = c.get("/factory/runs/ma-demo-01").json()["stages"]
    assert status["s7"] == "NOT_RUN" and status["s8"] == "NOT_RUN"  # downstream must be re-run


def test_out_of_order_is_rejected():
    c = new_client()
    r = c.post("/factory/data-engine/s4", json=payload("s4"))
    assert r.status_code == 409 and r.json()["error"] == "STAGE_ORDER"
    r = c.post("/factory/api-engine/s8", json=payload("s8"))
    assert r.status_code == 409 and r.json()["error"] == "STAGE_ORDER"


def test_payload_must_match_blueprint():
    c = new_client()
    r = c.post("/factory/data-engine/s3", json={**payload("s3"), "root_seed": 1})
    assert r.status_code == 409 and r.json()["error"] == "PAYLOAD_DIFFERS_FROM_BLUEPRINT"
    assert {d["field"] for d in r.json()["details"]} == {"root_seed"}
    r = c.post("/factory/data-engine/s3", json={**payload("s3"), "volumes": {"providers": 999}})
    assert r.status_code == 409 and r.json()["details"][0]["field"] == "volumes.providers"


def test_bad_requests():
    c = new_client()
    assert c.post("/factory/data-engine/s3", json={**payload("s3"), "stage": "s4"}).json()["error"] == "WRONG_STAGE"
    r = c.post("/factory/data-engine/s3", json={**payload("s3"), "consumes": {"alloydb": "alloy-nobody"}})
    assert r.status_code == 404 and r.json()["error"] == "UNKNOWN_TENANT"
    r = c.post("/factory/data-engine/s3", json={**payload("s3"), "entities": ["plan", "spaceship"]})
    assert r.status_code == 422 and r.json()["error"] == "UNKNOWN_ENTITIES"


def test_s8_endpoint_list_must_match_catalog(full_run):
    c, _ = full_run
    for st in ["s3", "s4", "s5", "s6", "s7"]:  # re-establish s7 after the idempotency test
        c.post(f"/factory/data-engine/{st}", json=payload(st))
    p = payload("s8")
    p["endpoints"] = p["endpoints"][:-1]
    r = c.post("/factory/api-engine/s8", json=p)
    assert r.status_code == 409 and r.json()["details"][0]["missing_from_payload"]
