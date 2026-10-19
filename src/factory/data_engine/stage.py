"""The contract every Data Engine stage implements, so the runner can treat them alike.

A stage is a StageSpec: its job name, default rules, the stages it needs, and a `run(ctx, options)` callable
that generates (or validates) and returns which entities it produced plus stage-specific result fields.

Member-sharded stages (s4, s5) also provide a ShardedGenerator module: prepare once, then task_count tasks that
each generate one shard. The runner hands those tasks to the profile's job executor (in-process or Cloud Run Job).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from factory.data_engine.context import GenContext

if TYPE_CHECKING:
    from factory.data_engine.runner import DataEngineRun


@dataclass(frozen=True)
class StageOptions:
    """Per-call options from the job payload."""

    rules: list[str] = field(default_factory=list)  # rules to check right after the stage
    rule_sets: list[str] | None = None  # s7: rule families to run
    privacy_options: dict | None = None  # s7: privacy scan options
    run: DataEngineRun | None = None  # s7: where the rule report, privacy scan and manifest are stored


@dataclass
class StageOutput:
    entities: list[str]  # entities this stage wrote, in result order
    extra: dict[str, Any] = field(default_factory=dict)  # stage-specific fields of the StageResult


@runtime_checkable
class ShardedGenerator(Protocol):
    """A stage module whose rows are generated per member shard (any task, any order, same rows)."""

    def prepare(self, ctx: GenContext) -> Any: ...  # read inputs once for all tasks

    def task_count(self, ctx: GenContext, prep: Any) -> int: ...

    def run_task(self, ctx: GenContext, index: int, prep: Any) -> None: ...  # generate and write shard `index`


@dataclass(frozen=True)
class StageSpec:
    name: str  # s3 .. s7
    job: str  # job name the payload must carry
    default_rules: list[str]  # checked after the stage when the payload names none
    needs: list[str]  # stages that must be completed first (v1: s6 needs s5, design doc gap 1)
    run: Callable[[GenContext, StageOptions], StageOutput]
    sharded: ShardedGenerator | None = None

    def __post_init__(self) -> None:
        if self.sharded is not None and not isinstance(self.sharded, ShardedGenerator):
            raise TypeError(f"{self.name}: {self.sharded!r} does not implement ShardedGenerator (prepare, task_count, run_task)")
