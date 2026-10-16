"""REST control plane with the spec's S1 payloads, unchanged, plus rejection cases."""

import pytest
from conftest import new_client, run_all, small_payloads, spec_payload

from factory.data_engine.runner import stage_seed


@pytest.fixture(scope="module")
def full_run():
    """s3..s8 with the spec payloads exactly as in the spec (tenant ma-demo-01, 50,000 members)."""
    client, reg = new_client()
    resp = run_all(client, {st: spec_payload(st) for st in ["s3", "s4", "s5", "s6", "s7", "s8"]})
    return client, reg, resp


def test_spec_payloads_run_end_to_end(full_run):
    _, _, r = full_run
    for st in ["s3", "s4", "s5", "s6", "s7"]:
        assert r[st].status_code == 200, (st, r[st].json())
        assert r[st].json()["status"] == "COMPLETED" and r[st].json()["tenant"] == "ma-demo-01"
    assert r["s7"].json()["gate"] == "data_quality_gate PASSED"
    assert r["s7"].json()["entity_count"] == spec_payload("s7")["consumes"]["entity_count"]
    s8 = r["s8"].json()
    assert r["s8"].status_code == 200 and s8["status"] == "DEPLOYED"
    assert s8["contract_tests"] == {"total": 24, "passed": 24}


def test_gaps_in_spec_payloads_are_reported(full_run):
    _, _, r = full_run
    notes = {st: " ".join(r[st].json()["notes"]) for st in ["s3", "s4", "s5", "s6"]}
    assert "lob_mix" in notes["s3"]
    assert "PEV-xx" in notes["s4"] and "member_accumulator" in notes["s4"]
    assert "history_months inferred = 2" in notes["s5"]
    assert "use_cases taken from the run's scenario_mix" in notes["s6"]


def test_stage_rule_checks_match_payload(full_run):
    _, _, r = full_run
    assert set(r["s5"].json()["rule_checks"]) == set(spec_payload("s5")["rules"])
    assert all(v in ("PASS", "PASS_PARTIAL", "DEFERRED_TO_S7") for st in ["s3", "s4", "s5", "s6"]
               for v in r[st].json()["rule_checks"].values())


def test_tenant_api_is_live_after_s8(full_run):
    c, _, _ = full_run
    creds = c.get("/factory/runs/ma-demo-01/credentials").json()
    tok = c.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                       "client_secret": creds["client_secret"]}).json()["access_token"]
    resp = c.get("/v1/knowledge/search?q=prior authorization", headers={"Authorization": f"Bearer {tok}", "x-fault-injection": "off"})
    assert resp.status_code == 200 and resp.json()["results"]


def test_peek_finds_scenario_rows(full_run):
    c, _, _ = full_run
    rows = c.get("/factory/runs/ma-demo-01/tables/claim_header?scenario_id=PCS-02&limit=2").json()["rows"]
    assert len(rows) == 2 and all(x["status"] == "PENDED" for x in rows)


def test_rerun_is_idempotent_and_resets_downstream(full_run):
    c, _, r = full_run
    again = c.post("/factory/data-engine/s3", json=spec_payload("s3")).json()
    assert {e["entity"]: e["checksum"] for e in again["entities"]} == {e["entity"]: e["checksum"] for e in r["s3"].json()["entities"]}
    stages = c.get("/factory/runs/ma-demo-01").json()["stages"]
    assert stages["s7"] == "NOT_RUN" and stages["s8"] == "NOT_RUN"


def test_out_of_order_is_rejected():
    c, _ = new_client()
    r = c.post("/factory/data-engine/s4", json=spec_payload("s4"))
    assert r.status_code == 409 and r.json()["error"] == "STAGE_ORDER"
    c.post("/factory/data-engine/s3", json=spec_payload("s3"))
    r = c.post("/factory/data-engine/s5", json=spec_payload("s5"))
    assert r.status_code == 409 and r.json()["error"] == "STAGE_ORDER"
    r = c.post("/factory/api-engine/s8", json=spec_payload("s8"))
    assert r.status_code == 409 and r.json()["error"] == "STAGE_ORDER"


def test_later_payloads_must_agree_with_the_run():
    c, _ = new_client()
    c.post("/factory/data-engine/s3", json=spec_payload("s3"))
    p = {**spec_payload("s4"), "root_seed": 1}
    r = c.post("/factory/data-engine/s4", json=p)
    assert r.status_code == 409 and r.json()["error"] == "RUN_SETTINGS_DIFFER"
    r = c.post("/factory/data-engine/s4", json={**spec_payload("s4"), "stage_seed": 123})
    assert r.status_code == 422 and r.json()["error"] == "BAD_STAGE_SEED"
    r = c.post("/factory/data-engine/s3", json={**spec_payload("s3"), "as_of_date": "2026-11-01", "stage_seed": stage_seed(20261012, "s3")})
    assert r.status_code == 409 and r.json()["error"] == "RUN_SETTINGS_DIFFER"


def test_bad_requests():
    c, _ = new_client()
    assert c.post("/factory/data-engine/s3", json={**spec_payload("s3"), "stage": "s4"}).json()["error"] == "WRONG_STAGE"
    r = c.post("/factory/data-engine/s3", json={**spec_payload("s3"), "consumes": {}})
    assert r.status_code == 422 and r.json()["error"] == "TENANT_REQUIRED"
    r = c.post("/factory/data-engine/s3", json={**spec_payload("s3"), "entities": ["plan", "spaceship"]})
    assert r.status_code == 422 and r.json()["error"] == "UNKNOWN_ENTITIES"
    assert c.get("/factory/runs").json()["runs"] == []  # rejected s3 calls leave no run behind


def test_s4_lob_needs_a_plan_from_s3():
    c, _ = new_client()
    p = small_payloads(100, tenant="lob")
    p["s3"]["distributions"] = {"lob_mix": {"COMMERCIAL": 1}}
    assert c.post("/factory/data-engine/s3", json=p["s3"]).status_code == 200
    r = c.post("/factory/data-engine/s4", json=p["s4"])  # s4 asks for MEDICARE_ADV members
    assert r.status_code == 409 and r.json()["error"] == "NO_PLAN_FOR_LOB"


def test_s8_endpoint_list_must_match_catalog():
    c, _ = new_client()
    p = small_payloads(150, tenant="ep")
    run_all(c, p, api=False)
    p["s8"]["endpoints"] = p["s8"]["endpoints"][:-1]
    r = c.post("/factory/api-engine/s8", json=p["s8"])
    assert r.status_code == 409 and r.json()["details"][0]["missing_from_payload"]
