"""Spec scenarios S2-S4, scaled down: s3..s8 run from the spec payloads (s4 gets the s5 scenario_mix)."""

import copy
import json
from pathlib import Path

import pytest
from conftest import new_client

FIX = Path(__file__).parent / "fixtures"
SIZES = {"s2": (3000, 18000, 900, 60), "s3": (2000, 13000, 650, 40), "s4": (2000, 12000, 600, 40)}  # members, claims, auths, providers


def scenario_payloads(name: str) -> dict:
    members, claims, auths, providers = SIZES[name]
    p = {st: json.loads((FIX / f"{name}_jobs" / f"{st}.json").read_text(encoding="utf-8")) for st in ["s3", "s4", "s5", "s6", "s7", "s8"]}
    p["s3"]["volumes"]["providers"] = providers
    p["s4"]["volumes"]["members"] = members
    p["s5"]["volumes"].update(claims=claims, authorizations=auths)
    p["s4"]["scenario_mix"] = copy.deepcopy(p["s5"]["scenario_mix"])
    return p


@pytest.fixture(scope="module", params=["s2", "s3", "s4"])
def scenario(request):
    client, reg = new_client()
    p = scenario_payloads(request.param)
    resp = {st: client.post(f"/factory/data-engine/{st}", json=p[st]) for st in ["s3", "s4", "s5", "s6", "s7"]}
    resp["s8"] = client.post("/factory/api-engine/s8", json=p["s8"])
    tenant = p["s3"]["consumes"]["alloydb"][len("alloy-"):]
    return request.param, client, reg.runs[tenant], resp


def test_scenario_runs_end_to_end(scenario):
    name, _, run, resp = scenario
    for st in ["s3", "s4", "s5", "s6", "s7"]:
        assert resp[st].status_code == 200, (name, st, resp[st].json())
    s7 = resp["s7"].json()
    assert s7["gate"] == "data_quality_gate PASSED", (name, s7["failed_rules"])
    s8 = resp["s8"].json()
    assert s8["status"] == "DEPLOYED", (name, s8.get("failed_contract_tests"))
    assert s8["contract_tests"]["passed"] == s8["contract_tests"]["total"] == 4 * s8["endpoint_count"]


def _token(client, tenant):
    creds = client.get(f"/factory/runs/{tenant}/credentials").json()
    tok = client.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                            "client_secret": creds["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}", "x-fault-injection": "off"}


def test_s2_caregiver_scope_and_rural_search(scenario):
    name, client, run, _ = scenario
    if name != "s2":
        pytest.skip("S2 only")
    h = _token(client, "mdcd-pilot-02")
    q = run.stores.tables.query
    cg = q("SELECT m.member_number, f.relationship_id FROM family_relationship f JOIN member_demographics m ON m.member_id = f.member_id "
           "WHERE f.scenario_id = 'MBN-04' AND f.caregiver_flag LIMIT 1").to_pylist()[0]
    ok = client.get(f"/v1/members/{cg['member_number']}/benefits?on_behalf_of={cg['relationship_id']}", headers=h)
    assert ok.status_code == 200  # BENEFITS is in scope
    lang = q("SELECT preferred_language, geo_type FROM member_demographics WHERE scenario_id = 'MPS-02'").to_pylist()
    assert lang and all(x["preferred_language"] == "ES" and x["geo_type"] == "RURAL" for x in lang)


def test_s3_reprocessing_is_consistent(scenario):
    name, _, run, _ = scenario
    if name != "s3":
        pytest.skip("S3 only")
    q = run.stores.tables.query
    assert q("SELECT count(*) FROM reprocessing_history").column(0)[0].as_py() > 0
    assert q("SELECT count(*) FROM appeal_record WHERE scenario_id = 'PAP-03' AND outcome = 'OVERTURNED'").column(0)[0].as_py() > 0
    assert q("SELECT count(*) FROM claim_payment WHERE recoup_flag").column(0)[0].as_py() > 0


def test_s4_claim_versions_are_linked(scenario):
    name, client, run, _ = scenario
    if name != "s4":
        pytest.skip("S4 only")
    h = _token(client, "sandbox-04")
    v = run.stores.tables.query("SELECT h.claim_number, m.member_number FROM claim_header h JOIN member_demographics m ON m.member_id = h.member_id "
                                "WHERE h.scenario_id = 'MCI-04' AND h.status = 'ADJUSTED' LIMIT 1").to_pylist()[0]
    r = client.get(f"/v1/claims/{v['claim_number']}?member_id={v['member_number']}", headers=h).json()
    replacement = r["versions"]["replaced_by"]
    r2 = client.get(f"/v1/claims/{replacement}?member_id={v['member_number']}", headers=h).json()
    assert r2["versions"]["is_replacement"] and r2["versions"]["original_claim_id"] == v["claim_number"]


def test_profile_row_limit():
    client, _ = new_client()
    p = scenario_payloads("s2")
    p["s4"]["volumes"]["members"] = 100000  # the spec's S2 size
    assert client.post("/factory/data-engine/s3", json=p["s3"]).status_code == 200
    r = client.post("/factory/data-engine/s4", json=p["s4"])
    assert r.status_code == 413 and r.json()["error"] == "PAYLOAD_TOO_LARGE_FOR_PROFILE"
