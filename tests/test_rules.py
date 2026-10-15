"""Stage s7 gates and scenario recipes."""


def test_all_rules_pass(generated):
    _, _, run = generated
    failed = [r for r in run.report["results"] if r["status"] == "FAIL"]
    assert failed == []
    assert run.report["referential_integrity"] == "100%"
    assert run.privacy["status"] == "PASS"


def test_every_row_is_stamped(generated):
    bp, stores, run = generated
    for entity in run.manifest.entity_counts:
        if run.manifest.entity_counts[entity] == 0:
            continue
        bad = stores.tables.query(
            f"SELECT count(*) FROM {entity} WHERE is_synthetic IS NOT TRUE OR tenant_id <> ? OR scenario_id IS NULL",
            [bp.tenant.id]).column(0)[0].as_py()
        assert bad == 0, entity


def test_scenario_recipes_hold(generated):
    _, stores, _ = generated
    q = stores.tables.query
    assert q("SELECT count(*) FROM claim_header h JOIN claim_pend p ON p.claim_id = h.claim_id "
             "WHERE h.scenario_id = 'PCS-02' AND (h.status <> 'PENDED' OR p.pend_reason <> 'RECORDS_REQUEST')").column(0)[0].as_py() == 0
    assert q("SELECT count(*) FROM claim_header h JOIN claim_denial d ON d.claim_id = h.claim_id "
             "WHERE h.scenario_id = 'PCS-03' AND d.carc <> 'CO-16'").column(0)[0].as_py() == 0
    assert q("SELECT count(*) FROM plan_enrollment e JOIN member_demographics m ON m.member_id = e.member_id "
             "WHERE m.scenario_id = 'PEV-02' AND e.term_dt IS NOT NULL AND e.status = 'TERMED' AND e.retro_change_flag").column(0)[0].as_py() > 0


def test_rule_engine_detects_a_broken_row(generated):
    """The gate is real: corrupt one claim line and F-01 must fail."""
    import pyarrow as pa

    from conftest import fresh_stores, small_blueprint
    from factory.data_engine.runner import run_data_engine
    from factory.data_engine.stages.s7_validate import run_rules

    bp = small_blueprint(300)
    stores = fresh_stores()
    run_data_engine(bp, stores, shards=1)
    row = stores.tables.query("SELECT * FROM claim_line WHERE line_status = 'PAID' LIMIT 1")
    i = row.schema.get_field_index("paid")
    broken = row.set_column(i, "paid", pa.array([row["billed"][0].as_py() * 2], row.schema.field("paid").type))
    stores.tables.upsert("claim_line", broken, "line_id")

    from factory.data_engine.context import GenContext
    ctx = GenContext(bp, stores)
    ctx.counts = {e: 1 for e in ["claim_line", "claim_header"]}
    failed = {r["rule"] for r in run_rules(ctx)["results"] if r["status"] == "FAIL"}
    assert "F-01a" in failed
