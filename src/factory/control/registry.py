"""Runs per tenant, tenant resolution, and job-payload validation against the stub blueprint.

The orchestrator's job payloads (spec section "Scenario walkthroughs", S1) carry no tenant or dataset id,
so the tenant is read from consumes.alloydb ("alloy-<tenant>"), an explicit ?tenant= / tenant_id, or the
run in progress. Fields that decide the data (seed, dates, versions, volumes, mixes) must equal the
blueprint's; a difference is a 409 with a diff, never a silent override.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.adapters import build_stores
from factory.adapters.ports import Stores
from factory.api_engine.routes import ENDPOINT_CATALOG
from factory.api_engine.stage import ApiEngineRun
from factory.config import Profile
from factory.contracts import Blueprint, S2Output, StageResult
from factory.data_engine import catalog
from factory.data_engine.context import GenContext
from factory.data_engine.runner import STAGES, DataEngineRun, stage_seed
from factory.data_engine.stages.s3_reference import N_PLANS


class JobRejected(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


@dataclass
class Run:
    bp: Blueprint
    stores: Stores
    ctx: GenContext
    data: DataEngineRun = field(default_factory=DataEngineRun)
    completed: dict[str, StageResult] = field(default_factory=dict)
    api: ApiEngineRun | None = None
    updated_at: float = field(default_factory=time.time)

    def status(self) -> dict[str, Any]:
        stages = {s: (self.completed[s].status if s in self.completed else "NOT_RUN") for s in [*STAGES, "s8"]}
        if self.api is not None:
            stages["s8"] = self.api.result.status
        return {"tenant": self.bp.tenant.id, "blueprint_id": self.bp.blueprint_id, "blueprint_hash": self.bp.blueprint_hash,
                "data_spec_hash": self.bp.data_spec_hash, "profile": self.stores.profile, "stages": stages,
                "next_stage": next((s for s, v in stages.items() if v == "NOT_RUN"), None),
                "api_live": self.api is not None and self.api.result.status == "DEPLOYED"}


class Registry:
    def __init__(self, profile: Profile, s2: S2Output | None = None) -> None:
        self.profile, self.s2 = profile, s2
        self.blueprints: dict[str, Blueprint] = {}
        self.runs: dict[str, Run] = {}
        self.active: str | None = None
        self.lock = threading.Lock()  # one job at a time: stages share in-memory state

    # ------------------------------------------------------------ blueprints and runs
    def load_dir(self, folder: Path) -> list[str]:
        loaded = []
        for path in sorted(folder.glob("*.json")):
            try:
                bp = Blueprint.load(path)
            except Exception:  # s2 outputs and other JSON files live here too
                continue
            self.blueprints[bp.tenant.id] = bp
            loaded.append(bp.tenant.id)
        return loaded

    def register(self, bp: Blueprint) -> None:
        old = self.blueprints.get(bp.tenant.id)
        if old and old.blueprint_hash != bp.blueprint_hash and bp.tenant.id in self.runs:
            raise JobRejected(409, "TENANT_HAS_RUN", f"Tenant {bp.tenant.id} already has a run with another blueprint. "
                              f"DELETE /factory/runs/{bp.tenant.id} first.")
        self.blueprints[bp.tenant.id] = bp

    def run_for(self, tenant: str) -> Run:
        if tenant not in self.runs:
            bp = self.blueprints[tenant]
            stores = build_stores(self.profile)
            ctx = GenContext(bp, stores)
            missing = ctx.unimplemented()
            if missing:
                raise JobRejected(422, "ENTITIES_NOT_IMPLEMENTED", "Blueprint scopes entities without a v1 generator.", missing)
            self.runs[tenant] = Run(bp, stores, ctx)
        return self.runs[tenant]

    def reset(self, tenant: str) -> bool:
        if self.active == tenant:
            self.active = None
        return self.runs.pop(tenant, None) is not None

    # ------------------------------------------------------------ tenant resolution
    def resolve_tenant(self, payload: dict[str, Any], explicit: str | None) -> str:
        candidates = [explicit, payload.get("tenant_id")]
        alloy = (payload.get("consumes") or {}).get("alloydb")
        if isinstance(alloy, str) and alloy.startswith("alloy-"):
            candidates.append(alloy[len("alloy-"):])
        for c in candidates:
            if c:
                if c not in self.blueprints:
                    raise JobRejected(404, "UNKNOWN_TENANT", f"No blueprint registered for tenant {c!r}.",
                                      {"registered": sorted(self.blueprints)})
                return c
        if self.active:
            return self.active
        if len(self.blueprints) == 1:
            return next(iter(self.blueprints))
        raise JobRejected(422, "TENANT_REQUIRED", "Cannot tell which tenant this job is for. Add consumes.alloydb "
                          "('alloy-<tenant>'), a tenant_id field, or ?tenant=<id>.", {"registered": sorted(self.blueprints)})


# ================================================================ payload validation
def _mismatch(diffs: list[dict], field_: str, sent: Any, expected: Any) -> None:
    if sent is not None and sent != expected:
        diffs.append({"field": field_, "sent": sent, "blueprint": expected})


def validate_data_job(stage: str, p: dict[str, Any], run: Run) -> list[str]:
    """Raise JobRejected on a bad payload; return notes (accepted differences, informational)."""
    bp, notes, diffs = run.bp, [], []
    job_name, _, needs = STAGES[stage]
    if p.get("stage") not in (None, stage):
        raise JobRejected(422, "WRONG_STAGE", f"This endpoint runs {stage}; payload says stage={p.get('stage')!r}.")
    if p.get("job") not in (None, job_name):
        raise JobRejected(422, "WRONG_JOB", f"Stage {stage} runs job {job_name!r}; payload says {p.get('job')!r}.")

    missing = [s for s in needs if s not in run.completed]
    if missing:
        msg = f"{stage} needs {', '.join(missing)} to be completed first for tenant {bp.tenant.id}."
        if stage == "s6" and missing == ["s5"]:
            msg += " (v1: care records and denial-code links need claims; design doc gap 1.)"
        raise JobRejected(409, "STAGE_ORDER", msg, run.status()["stages"])

    r = bp.repeatability
    if stage != "s7":
        _mismatch(diffs, "root_seed", p.get("root_seed"), r.root_seed)
        _mismatch(diffs, "as_of_date", p.get("as_of_date"), r.as_of_date.isoformat())
        _mismatch(diffs, "catalog_version", p.get("catalog_version"), r.catalog_version)
        _mismatch(diffs, "id_strategy", p.get("id_strategy"), r.id_strategy)
        _mismatch(diffs, "stage_seed", p.get("stage_seed"), stage_seed(r.root_seed, stage))
        shards = p.get("shards")
        if shards is not None and not (isinstance(shards, int) and 1 <= shards <= 64):
            raise JobRejected(422, "BAD_SHARDS", "shards must be an integer from 1 to 64.")

    vol, ds = p.get("volumes") or {}, bp.data_spec
    known = {"members": ds.volumes.members, "providers": ds.volumes.providers, "claims": ds.volumes.claims,
             "authorizations": ds.volumes.authorizations}
    for k, v in vol.items():
        if k in known:
            _mismatch(diffs, f"volumes.{k}", v, known[k])
        elif k == "plans":
            _mismatch(diffs, "volumes.plans", v, N_PLANS)
        elif k == "access_standards" and "access_standard" not in run.ctx.in_scope:
            notes.append(f"volumes.access_standards={v} ignored: access_standard is not in this blueprint's scope "
                         "(Provider is a supporting domain).")
        else:
            notes.append(f"volumes.{k}={v} is a target; actual counts are in entities[].")
    for k, v in (p.get("distributions") or {}).items():
        if k in ds.distributions:
            _mismatch(diffs, f"distributions.{k}", v, ds.distributions[k])
        else:
            notes.append(f"distributions.{k} is not used by the v1 generator.")
    _mismatch(diffs, "denial_rate", p.get("denial_rate"), ds.distributions.get("denial_rate"))
    if p.get("scenario_mix") is not None:
        sent = {s["scenario_id"]: s["share_pct"] for s in p["scenario_mix"]}
        _mismatch(diffs, "scenario_mix", sent, {s.scenario_id: s.share_pct for s in ds.scenario_mix})
    if p.get("personas") is not None and set(p["personas"]) != set(bp.scope.personas):
        notes.append(f"personas {p['personas']} differ from the blueprint {bp.scope.personas}; v1 derives persona types from LOB.")
    if diffs:
        raise JobRejected(409, "PAYLOAD_DIFFERS_FROM_BLUEPRINT",
                          f"The job differs from blueprint {bp.blueprint_id}. Register a new blueprint version to change these.", diffs)

    if stage != "s7":
        sent_ents = p.get("entities")
        produced = [e for e in catalog.STAGE_ENTITIES[stage] if run.ctx.wants(e)]
        if sent_ents is not None:
            unknown = [e for e in sent_ents if e not in catalog.PRIMARY_KEY and not any(e in v for v in catalog.ENTITIES.values())]
            if unknown:
                raise JobRejected(422, "UNKNOWN_ENTITIES", "Entities not in catalog 2.4.0.", unknown)
            out_of_scope = [e for e in sent_ents if not run.ctx.wants(e)]
            if out_of_scope:
                raise JobRejected(409, "ENTITIES_OUT_OF_SCOPE", "Entities not in this blueprint's scope.", out_of_scope)
            for e in sent_ents:
                if e not in produced:
                    other = next(s for s, ents in catalog.STAGE_ENTITIES.items() if e in ents)
                    why = " (derived from paid claims, rule F-03; design doc gap 2)" if e == "member_accumulator" else ""
                    notes.append(f"{e} is generated in {other}, not {stage}{why}.")
            for e in produced:
                if e not in sent_ents:
                    notes.append(f"{e} is also generated in {stage} (v1 stage layout).")
    else:
        sent_from = (p.get("consumes") or {}).get("from") or []
        not_done = [s for s in sent_from if s not in run.completed]
        if not_done:
            raise JobRejected(409, "STAGE_ORDER", f"s7 consumes {not_done}, which have not completed.")
        bad = [f for f in p.get("rule_sets") or [] if f not in "RTFCPA"]
        if bad:
            raise JobRejected(422, "UNKNOWN_RULE_SETS", "rule_sets must be from R, T, F, C, P, A.", bad)
    if stage == "s6" and p.get("llm"):
        notes.append(f"llm.model={p['llm'].get('model')!r}: this run uses {run.stores.llm.model!r} "
                     f"(profile {run.stores.profile}).")
    return notes


def validate_api_job(p: dict[str, Any], run: Run) -> list[str]:
    bp, notes, diffs = run.bp, [], []
    if p.get("stage") not in (None, "s8"):
        raise JobRejected(422, "WRONG_STAGE", f"This endpoint runs s8; payload says stage={p.get('stage')!r}.")
    if p.get("job") not in (None, "api_generate_and_deploy"):
        raise JobRejected(422, "WRONG_JOB", f"Stage s8 runs job 'api_generate_and_deploy'; payload says {p.get('job')!r}.")
    s7 = run.completed.get("s7")
    if s7 is None:
        raise JobRejected(409, "STAGE_ORDER", "s8 needs s7 (validate_and_scan) to be completed first.", run.status()["stages"])
    if not run.data.passed:
        raise JobRejected(409, "DATA_QUALITY_GATE_FAILED", "The data quality gate after s7 is red; the API engine does not deploy over failing data.",
                          {"validation": s7.model_extra.get("validation"), "privacy_scan": s7.model_extra.get("privacy_scan")})
    _mismatch(diffs, "use_cases", p.get("use_cases"), bp.scope.use_cases)
    _mismatch(diffs, "base_path", p.get("base_path"), bp.api_spec.base_path)
    if p.get("endpoints") is not None:
        base = bp.api_spec.base_path
        expected = {(m, base + path, uc) for uc in [*bp.scope.use_cases, "COMMON"] for m, path in ENDPOINT_CATALOG.get(uc, [])}
        sent = {(e["method"].upper(), e["path"], e.get("use_case")) for e in p["endpoints"]}
        if sent != expected:
            diffs.append({"field": "endpoints", "missing_from_payload": sorted(expected - sent), "not_in_catalog": sorted(sent - expected)})
    auth = p.get("auth") or {}
    if auth.get("type") not in (None, "OAuth2 client credentials"):
        diffs.append({"field": "auth.type", "sent": auth.get("type"), "supported": "OAuth2 client credentials"})
    if diffs:
        raise JobRejected(409, "PAYLOAD_DIFFERS_FROM_BLUEPRINT", f"The job differs from blueprint {bp.blueprint_id}.", diffs)
    if auth.get("secret_ref"):
        notes.append(f"auth.secret_ref={auth['secret_ref']!r} recorded; profile {run.stores.profile} issues a local client secret "
                     "(see credentials_secret_ref).")
    manifest = (p.get("consumes") or {}).get("manifest")
    if manifest and run.data.manifest and not manifest.endswith(f"/{run.data.manifest.root_seed}.json"):
        notes.append(f"consumes.manifest {manifest!r} does not name seed {run.data.manifest.root_seed}.")
    rpm = (p.get("rate_limit") or {}).get("per_client_rpm")
    default_rpm = int(bp.infra_spec.sizing.get("concurrent_sessions", 30)) * 20
    if rpm and rpm != default_rpm:
        notes.append(f"rate_limit.per_client_rpm={rpm} overrides the blueprint default of {default_rpm}.")
    return notes
