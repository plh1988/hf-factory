"""Agentic capability layer, phase 1: publish hand-written packs, preflight, 202 fallback, and the MBP acceptance run.

Acceptance (plan §10, phase 1): an s4 payload listing premium_invoice and an s8 payload with use_cases [MBP] run end
to end with no code change, once the packs are published.
"""

import copy
from pathlib import Path

import pytest
import yaml
from conftest import small_payloads
from fastapi.testclient import TestClient

from factory.capability import CapabilityRegistry, PackRejected, build_pack_store, reset_packs
from factory.config import AdapterConfig, load_profile
from factory.control.registry import Registry
from factory.control.server import create_control_app
from factory.data_engine import catalog, packs
from factory.data_engine.packs.model import parse_pack

PACKS = Path(__file__).parent.parent / "examples" / "capability-packs" / "mbp"
ENTITY = yaml.safe_load((PACKS / "premium_invoice.yaml").read_text(encoding="utf-8"))
USE_CASE = yaml.safe_load((PACKS / "uc_mbp.yaml").read_text(encoding="utf-8"))
MBP_ENDPOINTS = [{"method": "GET", "path": "/v1/members/{member_id}/premiums", "use_case": "MBP"},
                 {"method": "GET", "path": "/v1/members/{member_id}/premiums/{invoice_number}", "use_case": "MBP"}]


@pytest.fixture(autouse=True)
def clean_packs():
    reset_packs()
    yield
    reset_packs()


def _client(layer: bool = False) -> tuple[TestClient, Registry]:
    reg = Registry(load_profile("memory-offline"))
    return TestClient(create_control_app(reg, capability_layer=layer)), reg


def _mbp_payloads(tenant: str) -> dict:
    p = small_payloads(members=400, tenant=tenant)
    p["s4"]["entities"] = p["s4"]["entities"] + ["premium_invoice"]
    p["s4"]["distributions"]["lob_mix"] = {"MEDICARE_ADV": 1, "COMMERCIAL": 1, "MEDICAID_MCO": 1}
    p["s3"]["distributions"] = {"lob_mix": {"MEDICARE_ADV": 1, "COMMERCIAL": 1, "MEDICAID_MCO": 1}}
    mix = p["s5"]["scenario_mix"] + [{"scenario_id": "MBN-04", "share_pct": 10}]  # caregivers with BENEFITS scope only
    p["s4"]["scenario_mix"] = p["s5"]["scenario_mix"] = mix
    p["s6"]["use_cases"] = ["PCS", "MBP"]
    p["s8"]["use_cases"] = p["s8"]["use_cases"] + ["MBP"]
    p["s8"]["endpoints"] = p["s8"]["endpoints"] + MBP_ENDPOINTS
    return p


def _publish(client: TestClient) -> list[dict]:
    out = []
    for doc in (ENTITY, USE_CASE):
        r = client.post("/factory/capabilities/packs", json=doc)
        assert r.status_code == 201, r.json()
        out.append(r.json())
    return out


# ------------------------------------------------------------------ acceptance
def test_mbp_runs_end_to_end_after_publishing_the_packs():
    client, reg = _client(layer=True)
    p = _mbp_payloads("mbp")

    # before: preflight reports the gaps; a stage payload gets 202 with the same build
    pre = client.post("/factory/capabilities/preflight", json={"payloads": p}).json()
    assert pre["status"] == "PENDING_PACKS"
    assert {(g["kind"], g["name"]) for g in pre["remaining"]} == {("entity", "premium_invoice"), ("use_case", "MBP")}
    assert client.post("/factory/data-engine/s3", json=p["s3"]).status_code == 200
    r = client.post("/factory/data-engine/s4", json=p["s4"])
    assert r.status_code == 202 and r.json()["error"] == "CAPABILITY_BUILD_STARTED"

    # publish (gated), then the build is READY
    published = _publish(client)
    assert all(g["passed"] for pub in published for g in pub["gates"])
    assert client.get(f"/factory/capabilities/builds/{pre['build_id']}").json()["status"] == "READY"
    assert client.post("/factory/capabilities/preflight", json={"payloads": p}).json()["status"] == "READY"

    # the same payloads now run end to end
    for st in ["s4", "s5", "s6", "s7"]:
        r = client.post(f"/factory/data-engine/{st}", json=p[st])
        assert r.status_code == 200, (st, r.json())
    assert r.json()["gate"] == "data_quality_gate PASSED"
    rules = {x["rule"]: x["status"] for x in client.get("/factory/runs/mbp/rule-report").json()["results"]}
    assert {rules[k] for k in ("F-PREMIUM-01", "T-PREMIUM-01", "R-PREMIUM-01", "R-01:premium_invoice.member_id")} == {"PASS"}
    manifest = client.get("/factory/runs/mbp/manifest").json()
    assert "entity.premium_invoice@1.0.0" in manifest["capability_packs"] and manifest["entity_counts"]["premium_invoice"] > 0

    s8 = client.post("/factory/api-engine/s8", json=p["s8"]).json()
    assert s8["status"] == "DEPLOYED", s8.get("failed_contract_tests")
    tests = [t for t in reg.runs["mbp"].api.contract_tests if "/premiums" in t["endpoint"]]
    assert len(tests) == 8 and all(t["passed"] for t in tests)

    # the API behaves like a built-in use case
    creds = client.get("/factory/runs/mbp/credentials").json()
    tok = client.post("/oauth/token", data={"grant_type": "client_credentials", **{k: creds[k] for k in ("client_id", "client_secret")}}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}", "x-fault-injection": "off"}
    q = reg.runs["mbp"].stores.tables.query
    com = q("SELECT m.member_number FROM member_demographics m JOIN premium_invoice i ON i.member_id = m.member_id "
            "WHERE m.lob = 'COMMERCIAL' ORDER BY 1 LIMIT 1").to_pylist()[0]["member_number"]
    body = client.get(f"/v1/members/{com}/premiums", headers=h).json()
    assert body["count"] == 6 and len({i["amount_due"] for i in body["items"]}) == 1  # one premium per member
    assert [i["billing_month"] for i in body["items"]] == sorted((i["billing_month"] for i in body["items"]), reverse=True)
    assert body["items"][0]["billing_month"] == "2026-10-01"
    one = body["items"][-1]["invoice_number"]
    assert client.get(f"/v1/members/{com}/premiums/{one}", headers=h).json()["item"]["invoice_number"] == one
    assert client.get(f"/v1/members/{com}/premiums?status=REFUNDED", headers=h).json()["error"] == "INVALID_PARAMETER"
    medicaid = q("SELECT member_number FROM member_demographics WHERE lob = 'MEDICAID_MCO' ORDER BY 1 LIMIT 1").to_pylist()[0]["member_number"]
    assert client.get(f"/v1/members/{medicaid}/premiums", headers=h).json()["count"] == 0
    cg = q("SELECT m.member_number, f.relationship_id FROM family_relationship f JOIN member_demographics m ON m.member_id = f.member_id "
           "WHERE f.scenario_id = 'MBN-04' AND f.caregiver_flag LIMIT 1").to_pylist()[0]
    denied = client.get(f"/v1/members/{cg['member_number']}/premiums?on_behalf_of={cg['relationship_id']}", headers=h)
    assert denied.status_code == 403 and denied.json()["error"] == "SCOPE_NOT_AUTHORIZED"  # BENEFITS scope, MBP needs BILLING


def test_without_the_layer_unknown_capabilities_are_rejected_as_before():
    client, _ = _client(layer=False)
    p = _mbp_payloads("old")
    assert client.post("/factory/data-engine/s3", json=p["s3"]).status_code == 200
    r = client.post("/factory/data-engine/s4", json=p["s4"])
    assert r.status_code == 422 and r.json()["error"] == "UNKNOWN_ENTITIES"


# ------------------------------------------------------------------ registry rules
def test_invalid_and_failing_packs_are_rejected_and_nothing_stays_active():
    client, _ = _client()
    bad = copy.deepcopy(ENTITY)
    bad["entity"]["columns"][5]["gen"]["of"] = {"op": "column", "name": "amount_paid"}  # refers to a later column
    r = client.post("/factory/capabilities/packs", json=bad)
    assert r.status_code == 422 and r.json()["error"] == "CAPABILITY_INVALID"

    failing = copy.deepcopy(ENTITY)
    failing["rules"].append({"id": "F-PREMIUM-99", "description": "always fails", "sql": "SELECT count(*) FROM premium_invoice"})
    r = client.post("/factory/capabilities/packs", json=failing)
    assert r.status_code == 409 and r.json()["error"] == "CAPABILITY_GATES_FAILED"
    assert any(g["gate"] == "G2 sandbox run" and not g["passed"] for g in r.json()["details"])
    assert "premium_invoice" not in catalog.PRIMARY_KEY and packs.entity_pack("premium_invoice") is None

    r = client.post("/factory/capabilities/packs", json=USE_CASE)  # needs premium_invoice, not active
    assert r.status_code == 422 and "premium_invoice" in r.json()["message"]


def test_rule_sql_must_be_a_read_only_count():
    from factory.data_engine.packs.model import check_rule_sql

    assert check_rule_sql(ENTITY["rules"][2]["sql"]) == {"premium_invoice", "member_demographics"}
    for sql in ["DELETE FROM premium_invoice", "SELECT count(*) FROM premium_invoice; DROP TABLE plan",
                "SELECT count(*) FROM read_csv('/etc/passwd')", "SELECT count(*) FROM glob('*')",
                "SELECT count(*) FROM premium_invoice WHERE getenv('HOME') IS NOT NULL", "SELECT sum(amount_due) FROM premium_invoice",
                "SELECT count(*) FROM main.premium_invoice", "SELECT count(*) FROM"]:
        doc = copy.deepcopy(ENTITY)
        doc["rules"] = [{"id": "F-X-01", "description": "x", "sql": sql}]
        with pytest.raises(ValueError):
            parse_pack(doc)


def test_removal_order_and_core_packs():
    client, _ = _client()
    _publish(client)
    assert client.delete("/factory/capabilities/packs/entity.premium_invoice").json()["error"] == "PACK_IN_USE"
    assert client.delete("/factory/capabilities/packs/entity.sdoh_indicator").json()["error"] == "PACK_IN_USE"
    assert client.delete("/factory/capabilities/packs/uc.MBP").json()["removed"]
    assert client.delete("/factory/capabilities/packs/entity.premium_invoice").json()["removed"]
    assert "MBP" not in catalog.USE_CASES or "MBP" in catalog.CORE_USE_CASES
    assert [p["id"] for p in client.get("/factory/capabilities/packs").json()["packs"]] == ["entity.sdoh_indicator"]


def test_published_packs_survive_a_restart_with_a_local_store(tmp_path):
    store = build_pack_store(AdapterConfig(kind="local", root=str(tmp_path)))
    caps = CapabilityRegistry(store)
    caps.publish(ENTITY, run_gates=False)
    caps.publish(USE_CASE, run_gates=False)
    reset_packs()
    assert packs.use_case_pack("MBP") is None
    assert CapabilityRegistry(store).load() == ["entity.premium_invoice@1.0.0", "uc.MBP@1.0.0"]
    assert packs.use_case_pack("MBP") is not None and "premium_invoice" in catalog.STAGE_ENTITIES["s4"]


def test_use_case_slots_never_move_core_assets():
    assert catalog.USE_CASE_SLOTS["PPI"] == 9 and len(catalog.CORE_USE_CASES) == 10
    pack = parse_pack(USE_CASE)
    assert 10 <= pack.use_case.slot <= 98
    with pytest.raises(PackRejected):
        CapabilityRegistry(build_pack_store(None)).check(parse_pack({**USE_CASE, "id": "uc.PCS",
                                                                     "use_case": {**USE_CASE["use_case"], "code": "PCS"}}))


def test_lookup_picks_existing_rows_deterministically_and_rules_use_as_of():
    from factory.data_engine import rules as R

    doc = copy.deepcopy(ENTITY)
    doc["entity"]["columns"].append({"name": "pharmacy_npi", "type": "string", "references": {"entity": "provider", "column": "npi"},
                                     "gen": {"op": "lookup", "draw": "pharmacy", "per": "anchor", "table": "provider",
                                             "column": "npi", "where": {"facility_type": ["PHARMACY"]}}})
    doc["rules"].append({"id": "T-PREMIUM-02", "description": "no future bills", "sql": "SELECT count(*) FROM premium_invoice WHERE billing_month > AS_OF_DATE"})
    pub = CapabilityRegistry(build_pack_store(None)).publish(doc)  # G2: the FK rule on pharmacy_npi passes; G3: deterministic
    assert all(g["passed"] for g in pub.gates)
    rule = next(r for r in R.pack_rules("2026-10-12") if r.id == "T-PREMIUM-02")
    assert "DATE '2026-10-12'" in rule.sql and "AS_OF_DATE" not in rule.sql

    for gen, error in [({"op": "lookup", "draw": "x", "table": "claim_header", "column": "claim_id"}, "not generated before stage s4")]:
        bad = copy.deepcopy(doc)
        bad["entity"]["columns"][-1]["gen"] = gen
        with pytest.raises(ValueError, match=error):
            parse_pack(bad)
    for sql in ["SELECT count(*) FROM premium_invoice WHERE billing_month > CURRENT_DATE",
                "SELECT count(*) FROM premium_invoice WHERE billing_month > now()"]:
        bad = copy.deepcopy(doc)
        bad["rules"][-1]["sql"] = sql
        with pytest.raises(ValueError):
            parse_pack(bad)
