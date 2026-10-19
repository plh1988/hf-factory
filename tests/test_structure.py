"""Structural guards: the stage table, its validators, and SQL staying in the repository."""

import re
import types
from pathlib import Path

import pytest

from factory.control.validation import DATA_VALIDATORS
from factory.data_engine import scenarios as recipes
from factory.data_engine.runner import STAGES
from factory.data_engine.stage import ShardedGenerator, StageOutput, StageSpec

SRC = Path(__file__).parent.parent / "src" / "factory"


def test_every_stage_has_a_validator_and_known_needs():
    assert set(DATA_VALIDATORS) == set(STAGES)
    for spec in STAGES.values():
        assert all(n in STAGES and list(STAGES).index(n) < list(STAGES).index(spec.name) for n in spec.needs), spec.name


def test_sharded_stages_implement_the_protocol():
    assert {s for s, spec in STAGES.items() if spec.sharded} == {"s4", "s5"}
    for spec in STAGES.values():
        if spec.sharded:
            assert isinstance(spec.sharded, ShardedGenerator)


def test_stage_spec_rejects_an_incomplete_sharded_module():
    half = types.SimpleNamespace(prepare=lambda ctx: None, run_task=lambda ctx, i, prep: None)  # no task_count
    with pytest.raises(TypeError, match="ShardedGenerator"):
        StageSpec("sx", "job", [], [], lambda ctx, opts: StageOutput([]), half)


@pytest.mark.parametrize("module", ["routes.py", "member_routes.py", "provider_routes.py"])
def test_api_handlers_contain_no_sql(module):
    text = (SRC / "api_engine" / module).read_text(encoding="utf-8")
    assert not re.search(r"\bSELECT\b|\bINSERT\b|\bUPDATE\b.*\bSET\b", text), f"{module}: move SQL into Repository"


# ---------------------------------------------------------------- scenario recipes
ID = re.compile(r'"(PCS|PAS|PEV|MBN|MCI|MPS|MPC|MAS|PAP|PPI)-0\d"')


def test_recipes_are_consistent():
    for s in recipes.SCENARIOS.values():
        assert s.anchor in ("member", "claim", "auth") and s.kind in ("happy", "edge", "compliance", "data"), s.id
        if s.anchor == "claim":
            assert s.claim_status is not None, s.id
        if s.forced and s.anchor == "auth":
            assert s.auth_status is not None, s.id
        if s.appeal is not None:
            assert s.claim_status == "DENIED" and s.forced, s.id


def test_happy_and_forced_claim_sets():
    assert recipes.HAPPY_CLAIM_SCENARIOS == {"PCS-01", "MCI-01", "PPI-01", "PPI-02", "PAP-01"}
    assert recipes.FORCED_CLAIM_SCENARIOS == {s for s, r in recipes.SCENARIOS.items() if r.anchor == "claim"} - recipes.HAPPY_CLAIM_SCENARIOS


def test_having_counts_false_for_tri_state_fields():
    assert {s.id for s in recipes.having("auth_urgent")} == {"PAS-01", "PAS-02", "PAS-03"}


@pytest.mark.parametrize("path", sorted(str(p.relative_to(SRC)) for p in (SRC / "data_engine" / "stages").rglob("*.py")))
def test_generators_do_not_branch_on_scenario_ids(path):
    assert not ID.search((SRC / path).read_text(encoding="utf-8")), f"{path}: declare the fact on the recipe in scenarios.py"
