"""Runs Data Engine stages s3-s7, one REST job at a time.

STAGES is the stage table (see stage.StageSpec). run_stage() is the single entry point: it resets that stage's
counters (so re-runs are idempotent), runs the stage, checks the rules the job names, and publishes progress
events. Sharded stages run their tasks through the profile's job executor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from factory.adapters.jobs import TaskResult
from factory.contracts import EntityResult, Manifest, StageResult
from factory.data_engine import catalog
from factory.data_engine.context import GenContext
from factory.data_engine.rng import fmt_checksum
from factory.data_engine.stage import ShardedGenerator, StageOptions, StageOutput, StageSpec
from factory.data_engine.stages import s3_reference, s4_members, s5_claims_auth, s6_care_knowledge, s7_validate

TOPIC = "scenario-events"


class DataEngineError(RuntimeError):
    pass


def stage_seed(root_seed: int, stage: str) -> int:
    """32-bit FNV-1a of "<root_seed>:<stage>", as in the architecture spec."""
    h = 0x811C9DC5
    for ch in f"{root_seed}:{stage}":
        h = ((h ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
    return h


@dataclass
class DataEngineRun:
    """What s7 produces for a run: stage results, rule report, privacy scan and manifest; `passed` is the quality gate."""
    results: list[StageResult] = field(default_factory=list)
    manifest: Manifest | None = None
    report: dict[str, Any] = field(default_factory=dict)
    privacy: dict[str, Any] = field(default_factory=dict)
    manifest_uri: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.manifest) and self.report.get("failed", 1) == 0 and self.privacy.get("status") == "PASS"


def _loaded_to(stores, ents: list[str]) -> str:
    """Human-readable target for a stage result, e.g. 'memory://tables' or 'alloydb://... + bigquery://... (9 entities)'."""
    mirrored = [e for e in ents if stores.analytics is not None and stores.analytics.wants(e)]
    return stores.tables.uri + (f" + {stores.analytics.uri} ({len(mirrored)} entities)" if mirrored else "")


def run_sharded(ctx: GenContext, stage: str, mod: ShardedGenerator) -> list[str]:
    """Run a member-sharded stage through the profile's job executor (in-process tasks, or a Cloud Run Job)."""
    stores = ctx.stores
    # what a remote task needs: the run spec and the shard count
    stores.blobs.put("runs/spec.json", ctx.spec.model_dump_json(indent=2).encode(), "application/json")
    stores.blobs.put(f"runs/{stage}/job.json", json.dumps({"shards": ctx.shards}).encode(), "application/json")
    prep = mod.prepare(ctx)
    n = mod.task_count(ctx, prep)

    def local_task(i: int) -> TaskResult:
        """Run task i in-process with its own counters, so a retried task's partial counts are discarded
        instead of double-counted (its rows are upserts, so they are simply rewritten).
        """
        saved = ctx.counts, ctx.checksums
        ctx.counts, ctx.checksums = {}, {}
        try:
            mod.run_task(ctx, i, prep)
            return TaskResult(index=i, counts=dict(ctx.counts), checksums=dict(ctx.checksums))
        finally:
            ctx.counts, ctx.checksums = saved

    args = ["task", "--stage", stage, "--tenant", ctx.tenant_id]
    for res in stores.executor.run(stage, n, local_task, stores.blobs, args):
        for e, c in res.counts.items():
            ctx.counts[e] = ctx.counts.get(e, 0) + c
        for e, h in res.checksums.items():
            ctx.checksums[e] = (ctx.checksums.get(e, 0) + h) % (1 << 64)
    stores.events.publish(TOPIC, {"stage": stage, "state": "TASKS_DONE", "tasks": n})
    return [e for e in catalog.STAGE_ENTITIES[stage] if ctx.wants(e)]


def run_task_remote(ctx: GenContext, stage: str, index: int) -> dict:
    """Body of one Cloud Run Job task (`factory task`): generate shard `index`, return its partial counts/checksums."""
    mod = STAGES[stage].sharded
    if mod is None:
        raise ValueError(f"{stage} is not a sharded stage")
    prep = mod.prepare(ctx)
    ctx.counts, ctx.checksums = {}, {}
    mod.run_task(ctx, index, prep)
    return {"index": index, "counts": ctx.counts, "checksums": {k: fmt_checksum(v) for k, v in ctx.checksums.items()}}


# ---------------------------------------------------------------- stage run callables
def _sharded(stage: str, mod: ShardedGenerator):
    return lambda ctx, opts: StageOutput(run_sharded(ctx, stage, mod))


def _run_s3(ctx: GenContext, opts: StageOptions) -> StageOutput:
    return StageOutput(s3_reference.run(ctx))


def _run_s6(ctx: GenContext, opts: StageOptions) -> StageOutput:
    ents, extra = s6_care_knowledge.run(ctx)
    return StageOutput(ents, extra)


def _run_s7(ctx: GenContext, opts: StageOptions) -> StageOutput:
    """Validation: rules, privacy scan and manifest. Stores them on opts.run; the gate is red if anything fails."""
    out = s7_validate.run(ctx, opts.rule_sets, opts.privacy_options)
    if opts.run is not None:
        opts.run.report, opts.run.privacy, opts.run.manifest, opts.run.manifest_uri = (
            out["report"], out["privacy"], out["manifest"], out["manifest_uri"])
    passed = out["report"]["failed"] == 0 and out["privacy"]["status"] == "PASS"
    return StageOutput([], {
        "total_rows": sum(ctx.counts.values()), "entity_count": len(ctx.counts),
        "validation": {k: v for k, v in out["report"].items() if k != "results"},
        "failed_rules": [r for r in out["report"]["results"] if r["status"] == "FAIL"],
        "privacy_scan": out["privacy"]["status"], "privacy_detail": out["privacy"],
        "manifest_uri": out["manifest_uri"], "report_uri": out["report_uri"],
        "manifest": out["manifest"].model_dump(mode="json"),
        "gate": "data_quality_gate PASSED" if passed else "data_quality_gate FAILED"})


STAGES: dict[str, StageSpec] = {spec.name: spec for spec in [
    StageSpec("s3", "generate_reference_data", ["R-04", "F-04", "P-02"], [], _run_s3),
    StageSpec("s4", "generate_members", ["R-02", "R-05", "R-06", "T-01"], ["s3"], _sharded("s4", s4_members), s4_members),
    StageSpec("s5", "generate_claims_auth", ["R-03", "R-07", "F-01", "F-02", "F-05", "T-02", "T-03", "T-04"], ["s4"],
              _sharded("s5", s5_claims_auth), s5_claims_auth),
    StageSpec("s6", "generate_care_knowledge", ["R-09", "R-10", "R-11", "T-08"], ["s5"], _run_s6),  # v1: needs claims (gap 1)
    StageSpec("s7", "validate_and_scan", [], ["s3", "s4", "s5", "s6"], _run_s7),
]}


def run_stage(ctx: GenContext, stage: str, *, rules: list[str] | None = None, rule_sets: list[str] | None = None,
              privacy_options: dict | None = None, run: DataEngineRun | None = None) -> StageResult:
    """Run one stage against the stores in ctx. Raises DataEngineError on failure (after publishing it)."""
    spec = STAGES[stage]
    rules = spec.default_rules if rules is None else rules
    stores = ctx.stores
    for e in catalog.STAGE_ENTITIES.get(stage, []):  # idempotent re-run: recount this stage from zero
        ctx.counts.pop(e, None)
        ctx.checksums.pop(e, None)
    stores.events.publish(TOPIC, {"stage": stage, "job": spec.job, "state": "STARTED"})
    t0 = time.perf_counter()
    try:
        out = spec.run(ctx, StageOptions(rules=rules, rule_sets=rule_sets, privacy_options=privacy_options, run=run))
        ents, extra = out.entities, out.extra
        rule_checks, rule_detail = s7_validate.check_rules(ctx, rules) if rules else ({}, [])
    except Exception as exc:
        stores.events.publish(TOPIC, {"stage": stage, "state": "FAILED", "error": repr(exc)})
        raise DataEngineError(f"stage {stage} failed: {exc!r}") from exc
    res = StageResult(
        stage=stage, status="COMPLETED", duration_s=round(time.perf_counter() - t0, 2),
        entities=[EntityResult(entity=e, rows=ctx.counts.get(e, 0), checksum=fmt_checksum(ctx.checksums.get(e, 0))) for e in ents],
        rule_checks=rule_checks, loaded_to=_loaded_to(stores, ents), **extra,
    )
    if rule_detail:
        res.rule_detail = rule_detail
    if run is not None:
        run.results.append(res)
    stores.events.publish(TOPIC, {"stage": stage, "state": "COMPLETED", "duration_s": res.duration_s,
                                  "rows": sum(e.rows for e in res.entities)})
    return res
