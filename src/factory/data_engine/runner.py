"""Runs Data Engine stages s3-s7, one REST job at a time.

run_stage() is the single entry point for a stage: it resets that stage's counters (so re-runs are
idempotent), runs the generator, checks the rules the job names, and publishes progress events."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from factory.contracts import EntityResult, Manifest, StageResult
from factory.data_engine import catalog
from factory.data_engine.context import GenContext
from factory.data_engine.rng import fmt_checksum
from factory.data_engine.stages import s3_reference, s4_members, s5_claims_auth, s6_care_knowledge, s7_validate

# stage -> (job name, default rules, stages it needs). v1: s6 needs s5 (design doc gap 1).
STAGES: dict[str, tuple[str, list[str], list[str]]] = {
    "s3": ("generate_reference_data", ["R-04", "F-04", "P-02"], []),
    "s4": ("generate_members", ["R-02", "R-05", "R-06", "T-01"], ["s3"]),
    "s5": ("generate_claims_auth", ["R-03", "R-07", "F-01", "F-02", "F-05", "T-02", "T-03", "T-04"], ["s4"]),
    "s6": ("generate_care_knowledge", ["R-09", "R-10", "R-11", "T-08"], ["s5"]),
    "s7": ("validate_and_scan", [], ["s3", "s4", "s5", "s6"]),
}
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
    results: list[StageResult] = field(default_factory=list)
    manifest: Manifest | None = None
    report: dict[str, Any] = field(default_factory=dict)
    privacy: dict[str, Any] = field(default_factory=dict)
    manifest_uri: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.manifest) and self.report.get("failed", 1) == 0 and self.privacy.get("status") == "PASS"


def run_stage(ctx: GenContext, stage: str, *, rules: list[str] | None = None, rule_sets: list[str] | None = None,
              privacy_options: dict | None = None, run: DataEngineRun | None = None) -> StageResult:
    """Run one stage against the stores in ctx. Raises DataEngineError on failure (after publishing it)."""
    job, default_rules, _ = STAGES[stage]
    rules = default_rules if rules is None else rules
    stores = ctx.stores
    for e in catalog.STAGE_ENTITIES.get(stage, []):  # idempotent re-run: recount this stage from zero
        ctx.counts.pop(e, None)
        ctx.checksums.pop(e, None)
    stores.events.publish(TOPIC, {"stage": stage, "job": job, "state": "STARTED"})
    t0 = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        if stage == "s3":
            ents = s3_reference.run(ctx)
        elif stage == "s4":
            ents = s4_members.run(ctx)
        elif stage == "s5":
            ents = s5_claims_auth.run(ctx)
        elif stage == "s6":
            ents, extra = s6_care_knowledge.run(ctx)
        else:
            out = s7_validate.run(ctx, rule_sets, privacy_options)
            if run is not None:
                run.report, run.privacy, run.manifest, run.manifest_uri = out["report"], out["privacy"], out["manifest"], out["manifest_uri"]
            ents = []
            extra = {"total_rows": sum(ctx.counts.values()), "entity_count": len(ctx.counts),
                     "validation": {k: v for k, v in out["report"].items() if k != "results"},
                     "failed_rules": [r for r in out["report"]["results"] if r["status"] == "FAIL"],
                     "privacy_scan": out["privacy"]["status"], "privacy_detail": out["privacy"],
                     "manifest_uri": out["manifest_uri"], "report_uri": out["report_uri"],
                     "manifest": out["manifest"].model_dump(mode="json"),
                     "gate": "data_quality_gate PASSED" if out["report"]["failed"] == 0 and out["privacy"]["status"] == "PASS"
                     else "data_quality_gate FAILED"}
        rule_checks, rule_detail = s7_validate.check_rules(ctx, rules) if rules else ({}, [])
    except Exception as exc:
        stores.events.publish(TOPIC, {"stage": stage, "state": "FAILED", "error": repr(exc)})
        raise DataEngineError(f"stage {stage} failed: {exc!r}") from exc
    res = StageResult(
        stage=stage, status="COMPLETED", duration_s=round(time.perf_counter() - t0, 2),
        entities=[EntityResult(entity=e, rows=ctx.counts.get(e, 0), checksum=fmt_checksum(ctx.checksums.get(e, 0))) for e in ents],
        rule_checks=rule_checks, loaded_to=stores.tables.uri, **extra,
    )
    if rule_detail:
        res.rule_detail = rule_detail
    if run is not None:
        run.results.append(res)
    stores.events.publish(TOPIC, {"stage": stage, "state": "COMPLETED", "duration_s": res.duration_s,
                                  "rows": sum(e.rows for e in res.entities)})
    return res
