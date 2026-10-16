"""Stage s7 gates and scenario recipes."""

import pyarrow as pa
from conftest import new_client, run_all, small_payloads

from factory.data_engine.stages.s7_validate import run_rules


def test_all_rules_pass(generated):
    _, run, resp = generated
    s7 = resp["s7"].json()
    assert s7["failed_rules"] == []
    assert s7["validation"]["referential_integrity"] == "100%"
    assert s7["privacy_scan"] == "PASS"
    assert run.data.passed


def test_every_row_is_stamped(generated):
    _, run, _ = generated
    for entity, n in run.ctx.counts.items():
        if n == 0:
            continue
        bad = run.stores.tables.query(
            f"SELECT count(*) FROM {entity} WHERE is_synthetic IS NOT TRUE OR tenant_id <> ? OR scenario_id IS NULL",
            ["test-small"]).column(0)[0].as_py()
        assert bad == 0, entity


def test_scenario_recipes_hold(generated):
    _, run, _ = generated
    q = run.stores.tables.query
    assert q("SELECT count(*) FROM claim_header h JOIN claim_pend p ON p.claim_id = h.claim_id "
             "WHERE h.scenario_id = 'PCS-02' AND (h.status <> 'PENDED' OR p.pend_reason <> 'RECORDS_REQUEST')").column(0)[0].as_py() == 0
    assert q("SELECT count(*) FROM claim_header h JOIN claim_denial d ON d.claim_id = h.claim_id "
             "WHERE h.scenario_id = 'PCS-03' AND d.carc <> 'CO-16'").column(0)[0].as_py() == 0
    assert q("SELECT count(*) FROM plan_enrollment e JOIN member_demographics m ON m.member_id = e.member_id "
             "WHERE m.scenario_id = 'PEV-02' AND e.status = 'TERMED' AND e.retro_change_flag").column(0)[0].as_py() > 0


def test_member_scenarios_need_scenario_mix_in_s4():
    """The spec's s4 payload has no scenario_mix: members stay baseline and a note says so."""
    client, reg = new_client()
    resp = run_all(client, small_payloads(300, member_scenarios=False), api=False)
    assert any("PEV-xx" in n for n in resp["s4"].json()["notes"])
    q = reg.runs["test-small"].stores.tables.query
    assert q("SELECT count(*) FROM member_demographics WHERE scenario_id <> 'BASE'").column(0)[0].as_py() == 0
    assert resp["s7"].json()["gate"] == "data_quality_gate PASSED"


def test_rule_engine_detects_a_broken_row():
    """The gate is real: corrupt one claim line and F-01 must fail."""
    client, reg = new_client()
    run_all(client, small_payloads(300), api=False)
    run = reg.runs["test-small"]
    row = run.stores.tables.query("SELECT * FROM claim_line WHERE line_status = 'PAID' LIMIT 1")
    i = row.schema.get_field_index("paid")
    broken = row.set_column(i, "paid", pa.array([row["billed"][0].as_py() * 2], row.schema.field("paid").type))
    run.stores.tables.upsert("claim_line", broken, "line_id")
    failed = {r["rule"] for r in run_rules(run.ctx)["results"] if r["status"] == "FAIL"}
    assert "F-01a" in failed
