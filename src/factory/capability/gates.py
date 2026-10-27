"""Gates a pack must pass before it stays active (plan §8). Each gate is a real sandbox run through an in-process
control plane (memory-offline profile, ~200 members), so a gate never touches a tenant's data or the network.

  entity pack    G2 sandbox s3-s7: stages succeed, the entity has rows, s7 quality gate passes (pack rules included)
                 G3 determinism: identical checksums for a different shard count
                 G4 regression: every other entity's checksum equals the run without the pack
  use-case pack  G2 sandbox s3-s8: deployed, every contract test passes (the pack's generated ones included)
                 G4 regression: every non-knowledge entity's checksum equals the run without the use case
G1 (static checks) is done by the registry before any sandbox run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from factory.capability import sandbox
from factory.data_engine import catalog
from factory.data_engine.packs.model import EntityPack, UseCasePack


# One tenant name for every sandbox run: tenant_id is stamped on every row and the dataset id feeds every row id,
# so runs are only comparable with the same name. Each run has its own in-process control plane, so they never mix.
TENANT = "capability-sandbox"


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


@dataclass
class SandboxRun:
    status: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    s7: dict[str, Any] = field(default_factory=dict)
    s8: dict[str, Any] = field(default_factory=dict)


def run_sandbox(payloads: dict[str, dict[str, Any]]) -> SandboxRun:
    """Run the payloads through a fresh in-process control plane (memory-offline). Imports are local: the control
    plane imports this package."""
    from fastapi.testclient import TestClient

    from factory.config import load_profile
    from factory.control.registry import Registry
    from factory.control.server import create_control_app

    reg = Registry(load_profile("memory-offline"))
    client = TestClient(create_control_app(reg, capability_layer=False, load_packs=False))
    out = SandboxRun()
    for st in ["s3", "s4", "s5", "s6", "s7"]:
        r = client.post(f"/factory/data-engine/{st}", json=payloads[st])
        out.status[st] = r.status_code
        if r.status_code != 200:
            out.s7 = {"error": r.json()}
            return out
        if st == "s7":
            out.s7 = r.json()
    tenant = reg.active
    manifest = reg.runs[tenant].data.manifest
    out.counts, out.checksums = dict(manifest.entity_counts), dict(manifest.entity_checksums)
    if "s8" in payloads:
        r = client.post("/factory/api-engine/s8", json=payloads["s8"])
        out.status["s8"], out.s8 = r.status_code, r.json()
    return out


def _stages_ok(run: SandboxRun) -> GateResult:
    bad = {s: c for s, c in run.status.items() if c != 200}
    if bad:
        return GateResult("G2 sandbox run", False, f"stages failed: {bad}; {str(run.s7.get('error') or run.s8)[:400]}")
    if run.s7.get("gate") != "data_quality_gate PASSED":
        failed = [f"{r['rule']} ({r['violations']})" for r in run.s7.get("failed_rules", [])]
        return GateResult("G2 sandbox run", False, f"s7 quality gate failed: {failed} privacy={run.s7.get('privacy_scan')}")
    return GateResult("G2 sandbox run", True)


def _regression(before: SandboxRun, after: SandboxRun, skip: set[str]) -> GateResult:
    changed = sorted(e for e in before.checksums if e not in skip and before.checksums[e] != after.checksums.get(e))
    if changed:
        return GateResult("G4 regression", False, f"other entities changed: {changed}")
    return GateResult("G4 regression", True, f"{len([e for e in before.checksums if e not in skip])} entities unchanged")


def baseline(pack: EntityPack | UseCasePack) -> SandboxRun:
    """The sandbox run without the pack's contribution (taken before the pack is activated)."""
    if isinstance(pack, EntityPack):
        return run_sandbox(sandbox.payloads(TENANT))
    stages = _needs_by_stage(pack)
    return run_sandbox(sandbox.payloads(TENANT, stages))


def _needs_by_stage(pack: UseCasePack) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in pack.use_case.needs:
        if e in catalog.CORE_ENTITIES:
            continue
        stage = next(s for s, ents in catalog.STAGE_ENTITIES.items() if e in ents)
        out.setdefault(stage, []).append(e)
    return out


def run_gates(pack: EntityPack | UseCasePack, before: SandboxRun) -> list[GateResult]:
    """G2-G4 for an *activated* pack, against the baseline taken before activation."""
    t0 = time.perf_counter()
    if isinstance(pack, EntityPack):
        name, stage = pack.entity.name, pack.entity.stage
        run = run_sandbox(sandbox.payloads(TENANT, {stage: [name]}))
        results = [_stages_ok(run)]
        if results[0].passed and not run.counts.get(name):
            results[0] = GateResult("G2 sandbox run", False, f"{name} generated no rows for 200 members")
        if results[0].passed:
            other = run_sandbox(sandbox.payloads(TENANT, {stage: [name]}, shards=1))
            same = other.checksums.get(name) == run.checksums.get(name)
            results.append(GateResult("G3 determinism", same, "" if same else "checksum differs between 1 and 2 shards"))
            results.append(_regression(before, run, {name}))
    else:
        code = pack.use_case.code
        run = run_sandbox(sandbox.payloads(TENANT, _needs_by_stage(pack), [code]))
        results = [_stages_ok(run)]
        if results[0].passed:
            ct = run.s8.get("contract_tests", {})
            failed = [f"{t['endpoint']}: {t['test']} ({t['status']})" for t in run.s8.get("failed_contract_tests", [])]
            ok = run.s8.get("status") == "DEPLOYED"
            results.append(GateResult("G5 contract tests", ok, f"{ct.get('passed')}/{ct.get('total')} passed" + (f"; failed: {failed}" if failed else "")))
            results.append(_regression(before, run, set(catalog.ENTITIES["K"])))
    results.append(GateResult("timing", True, f"{time.perf_counter() - t0:.1f}s"))
    return results
