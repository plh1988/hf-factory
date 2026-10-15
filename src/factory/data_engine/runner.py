"""Runs Data Engine stages s3-s7. Stands in for the Orchestrator: builds each stage job spec from the
blueprint, runs the stage, publishes progress events and returns the stage results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from factory.adapters.ports import Stores
from factory.contracts import Blueprint, EntityResult, Manifest, StageJobSpec, StageResult
from factory.data_engine import catalog
from factory.data_engine.context import GenContext
from factory.data_engine.rng import fmt_checksum, stable_hash64
from factory.data_engine.stages import s3_reference, s4_members, s5_claims_auth, s6_care_knowledge, s7_validate

STAGES = [
    ("s3", "generate_reference_data", ["R-04", "F-04", "P-02"]),
    ("s4", "generate_members", ["R-02", "R-05", "R-06", "T-01"]),
    ("s5", "generate_claims_auth", ["R-03", "R-07", "F-01", "F-02", "F-03", "F-05", "T-02", "T-03", "T-04"]),
    ("s6", "generate_care_knowledge", ["R-10", "R-11", "T-06", "T-08"]),
    ("s7", "validate_and_scan", ["R", "T", "F", "C", "P", "A"]),
]


class DataEngineError(RuntimeError):
    pass


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


def job_spec(bp: Blueprint, stage: str, job: str, rules: list[str], shards: int, stores: Stores) -> StageJobSpec:
    r = bp.repeatability
    return StageJobSpec(
        job=job, stage=stage, root_seed=r.root_seed, stage_seed=stable_hash64(f"{r.root_seed}:{stage}") % (1 << 32),
        as_of_date=r.as_of_date, catalog_version=r.catalog_version, id_strategy=r.id_strategy, shards=shards,
        entities=catalog.STAGE_ENTITIES.get(stage, []), rules=rules,
        consumes={"tables": stores.tables.uri, "llm": stores.llm.model},
    )


def run_data_engine(bp: Blueprint, stores: Stores, shards: int = 4) -> DataEngineRun:
    ctx = GenContext(bp, stores, shards=shards)
    missing = ctx.unimplemented()
    if missing:
        raise DataEngineError(f"blueprint asks for entities without a v1 generator: {missing}")
    run = DataEngineRun()
    topic = "scenario-events"
    for stage, job, rules in STAGES:
        spec = job_spec(bp, stage, job, rules, shards, stores)
        stores.blobs.put(f"jobs/{stage}.json", spec.model_dump_json(indent=2).encode(), "application/json")
        stores.events.publish(topic, {"stage": stage, "job": job, "state": "STARTED"})
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
                out = s7_validate.run(ctx)
                run.report, run.privacy, run.manifest = out["report"], out["privacy"], out["manifest"]
                run.manifest_uri = out["manifest_uri"]
                ents = []
                extra = {"validation": {k: v for k, v in out["report"].items() if k != "results"},
                         "privacy_scan": out["privacy"]["status"], "manifest_uri": out["manifest_uri"],
                         "report_uri": out["report_uri"]}
        except Exception as exc:  # report the failure the way the orchestrator expects, then stop
            res = StageResult(stage=stage, status="FAILED", duration_s=round(time.perf_counter() - t0, 2), error=repr(exc))
            run.results.append(res)
            stores.events.publish(topic, {"stage": stage, "state": "FAILED", "error": repr(exc)})
            raise DataEngineError(f"stage {stage} failed: {exc!r}") from exc
        res = StageResult(
            stage=stage, status="COMPLETED", duration_s=round(time.perf_counter() - t0, 2),
            entities=[EntityResult(entity=e, rows=ctx.counts.get(e, 0), checksum=fmt_checksum(ctx.checksums.get(e, 0))) for e in ents],
            loaded_to=stores.tables.uri, **extra,
        )
        run.results.append(res)
        stores.events.publish(topic, {"stage": stage, "state": "COMPLETED", "duration_s": res.duration_s,
                                      "rows": sum(e.rows for e in res.entities)})
    return run
