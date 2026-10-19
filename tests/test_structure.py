"""Structural guards: the stage table, its validators, and SQL staying in the repository."""

import re
import types
from pathlib import Path

import pytest

from factory.control.validation import DATA_VALIDATORS
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
