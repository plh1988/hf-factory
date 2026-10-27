"""Capability packs (agentic capability layer, phase 0): format validation and parity with the coded generator."""

import copy

import numpy as np
import pyarrow as pa
import pytest
import yaml
from conftest import new_client, small_payloads

from factory.codes import Lob
from factory.data_engine import packs
from factory.data_engine import scenarios as recipes
from factory.data_engine.context import GenContext, dates
from factory.data_engine.packs.model import Pack
from factory.data_engine.stages import s4_members
from factory.data_engine.stages.s4_members import MemberBatch

SDOH = packs.CORE_DIR / "sdoh_indicator.yaml"


def reference_sdoh(ctx: GenContext, b: MemberBatch) -> None:
    """The coded sdoh_indicator generator the pack replaced (s4, commit 493a541), kept as the parity reference."""
    rr, idx, scen = b.rr, b.idx, b.scen
    transport = recipes.tagged(scen, lambda s: s.transport_need)
    s = rr.bernoulli("screened", 0.6) | transport
    pos_rate = np.where(b.lob == Lob.MEDICAID_MCO, 0.40, 0.18)
    domain = np.where(transport, "TRANSPORT", rr.choice("sdoh_domain", ["FOOD", "HOUSING", "TRANSPORT", "UTILITIES", "SAFETY", "ISOLATION"]))
    positive = rr.bernoulli("sdoh_pos", pos_rate) | transport
    ctx.write("sdoh_indicator", {
        "indicator_id": pa.array(ctx.ids("sdoh_indicator", idx[s])),
        "member_id": pa.array(b.member_id[s].tolist()),
        "domain": pa.array(domain[s].tolist()),
        "positive": pa.array(positive[s]),
        "screened_dt": dates(ctx.as_of - rr.integers("screened_dt", 10, 300)[s].astype("timedelta64[D]")),
        "source": pa.array(rr.choice("sdoh_src", ["SCREENER", "CLAIMS_Z_CODE", "SELF_REPORT"])[s].tolist()),
        "referral_status": pa.array(rr.choice("sdoh_ref", ["NONE", "SENT", "CONNECTED"], [60, 25, 15])[s].tolist()),
    }, scenario=scen[s])


def _run_s4(seed: int, shards: int, lob_mix: dict) -> tuple[str, list[dict]]:
    client, reg = new_client()
    p = small_payloads(members=900, tenant=f"pk{seed}{shards}", seed=seed, shards=shards)
    p["s4"]["distributions"]["lob_mix"] = lob_mix
    p["s3"]["distributions"] = {"lob_mix": lob_mix}
    p["s4"]["scenario_mix"] = [{"scenario_id": "MBN-03", "share_pct": 10}, {"scenario_id": "MAS-02", "share_pct": 10},
                               {"scenario_id": "PEV-02", "share_pct": 5}]
    for st in ["s3", "s4"]:
        r = client.post(f"/factory/data-engine/s{st[1]}", json=p[st])
        assert r.status_code == 200, r.json()
    run = reg.runs[f"pk{seed}{shards}"]
    rows = run.stores.tables.query("SELECT * FROM sdoh_indicator ORDER BY indicator_id").to_pylist()
    return run.ctx.checksums["sdoh_indicator"], rows


@pytest.mark.parametrize("seed,shards,lob_mix", [(7, 1, {"MEDICAID_MCO": 1}), (20261012, 3, {"MEDICARE_ADV": 1, "MEDICAID_MCO": 1}),
                                                 (99, 5, {"COMMERCIAL": 2, "MEDICAID_MCO": 1})])
def test_sdoh_pack_matches_the_coded_generator(monkeypatch, seed, shards, lob_mix):
    pack_sum, pack_rows = _run_s4(seed, shards, lob_mix)

    def coded(ctx, b):
        if ctx.wants("sdoh_indicator"):
            reference_sdoh(ctx, b)

    monkeypatch.setattr(s4_members, "_write_member_packs", coded)
    ref_sum, ref_rows = _run_s4(seed, shards, lob_mix)
    assert pack_rows == ref_rows and pack_sum == ref_sum
    assert any(r["domain"] == "TRANSPORT" and r["positive"] and r["scenario_id"] in ("MBN-03", "MAS-02") for r in pack_rows)


def test_core_packs_load_and_match_the_catalog():
    from factory.data_engine import catalog

    loaded = packs.core_packs()
    assert [p.id for p in loaded] == ["entity.sdoh_indicator"]
    for p in loaded:
        assert p.entity.name in catalog.STAGE_ENTITIES[p.entity.stage]
        assert catalog.PRIMARY_KEY[p.entity.name] == p.entity.primary_key
        assert p.digest() == Pack.model_validate(yaml.safe_load(SDOH.read_text(encoding="utf-8"))).digest()


def _sdoh_doc() -> dict:
    return yaml.safe_load(SDOH.read_text(encoding="utf-8"))


def _mutate(path: list, value) -> dict:
    doc = copy.deepcopy(_sdoh_doc())
    node = doc
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return doc


@pytest.mark.parametrize("path,value,error", [
    (["entity", "columns", 1, "gen"], {"op": "anchor", "field": "ssn"}, "anchor field 'ssn' unknown"),
    (["entity", "columns", 2, "gen"], {"op": "teleport"}, "does not match any of the expected tags"),
    (["entity", "columns", 4, "gen"], {"op": "days_before_as_of", "draw": "d", "min_days": 30, "max_days": 10}, "max_days must be greater"),
    (["entity", "columns", 6, "gen"], {"op": "choice", "draw": "x", "options": ["A", "B"], "weights": [1]}, "1 weights for 2 options"),
    (["entity", "primary_key"], "member_id", "must be a string column with gen op row_id"),
    (["entity", "rows", "when"], {"op": "recipe", "flag": "teleport_need"}, "not a Scenario field"),
    (["core"], False, "only core packs may use it"),
    (["id"], "entity.other", "must be 'entity.sdoh_indicator'"),
    (["entity", "columns", 1, "name"], "tenant_id", "stamped by the engine"),
])
def test_invalid_packs_are_rejected_with_a_reason(path, value, error):
    with pytest.raises(ValueError, match=error):
        Pack.model_validate(_mutate(path, value))


def test_digest_ignores_formatting_but_not_content():
    a = Pack.model_validate(_sdoh_doc())
    b = Pack.model_validate(yaml.safe_load(yaml.safe_dump(_sdoh_doc(), sort_keys=True)))
    c = Pack.model_validate(_mutate(["entity", "columns", 6, "gen", "weights"], [50, 35, 15]))
    assert a.digest() == b.digest() != c.digest()


def test_skill_references_are_up_to_date():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "build_skill_refs.py"
    result = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_digits_and_numeric_map_keys():
    from factory.data_engine.packs.model import parse_pack

    doc = _sdoh_doc()
    doc["entity"]["columns"].append({"name": "ref_code", "type": "string", "gen": {"op": "digits", "draw": "ref", "length": 11}})
    doc["entity"]["columns"].append({"name": "weight", "type": "float",
                                     "gen": {"op": "map", "of": {"op": "const", "value": 2}, "values": {1: 0.5, 2: 1.5}, "default": 0}})
    pack = parse_pack(doc)
    assert pack.entity.columns[-1].gen.values == {"1": 0.5, "2": 1.5}
