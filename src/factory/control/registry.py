"""Runs per tenant and job-payload validation. No blueprint: every setting comes from the payloads.

A run starts with the s3 payload (seed, as-of date, catalog, id strategy, tenant from consumes.alloydb).
Later payloads must agree with what the run already holds; they add volumes, distributions, scenario mix
and entities. Gaps in the spec payloads are filled by documented derivations and reported as notes:

  s3  no LOB mix          -> plans cycle MA / Commercial / Medicaid (optional distributions.lob_mix)
  s4  no scenario_mix     -> member scenarios (PEV-xx) are not injected (optional scenario_mix)
  s5  no history window   -> history_months = claims x 12 / (members x 6), D-05 median 6 claims/member/year
  s6  no use cases        -> taken from the run's scenario_mix ids (optional use_cases)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from factory.adapters import build_stores
from factory.adapters.ports import Stores
from factory.api_engine.routes import ENDPOINT_CATALOG, ENDPOINTS
from factory.api_engine.stage import ApiEngineRun
from factory.config import Profile
from factory.contracts import ApiJob, DataJob, RunSpec, S2Output, StageResult, payload_hash
from factory.data_engine import catalog
from factory.data_engine.context import GenContext
from factory.data_engine.runner import STAGES, DataEngineRun, stage_seed

ID_STRATEGY = "UUIDv5(dataset_id, entity, seed, row_index)"
NEEDS_TABLES = {  # stage -> tables it reads from earlier stages
    "s4": ["plan", "provider", "network_participation"],
    "s5": ["member_demographics", "plan_enrollment", "plan", "provider", "network_participation"],
    "s6": ["plan"],
}
USE_CASE_TABLES = {  # s8: data a use case's endpoints read
    "PCS": ["claim_header", "claim_line", "claim_denial", "claim_pend", "claim_payment", "provider"],
    "PAS": ["auth_request", "auth_line", "auth_determination", "clinical_review_queue", "documentation_requirement", "provider"],
    "PEV": ["member_demographics", "plan_enrollment", "plan", "provider"],
    "COMMON": ["provider", "member_demographics", "asset_chunk"],
}


class JobRejected(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


@dataclass
class Run:
    spec: RunSpec
    stores: Stores
    ctx: GenContext
    data: DataEngineRun = field(default_factory=DataEngineRun)
    completed: dict[str, StageResult] = field(default_factory=dict)
    api: ApiEngineRun | None = None
    updated_at: float = field(default_factory=time.time)

    def status(self) -> dict[str, Any]:
        stages = {s: (self.completed[s].status if s in self.completed else "NOT_RUN") for s in STAGES}
        stages["s8"] = self.api.result.status if self.api else "NOT_RUN"
        return {"tenant": self.spec.tenant_id, "dataset_id": self.spec.dataset_id, "root_seed": self.spec.root_seed,
                "as_of_date": self.spec.as_of_date.isoformat(), "profile": self.stores.profile, "stages": stages,
                "next_stage": next((s for s, v in stages.items() if v == "NOT_RUN"), None),
                "api_live": self.api is not None and self.api.result.status == "DEPLOYED"}


class Registry:
    def __init__(self, profile: Profile, s2: S2Output | None = None) -> None:
        self.profile, self.s2 = profile, s2
        self.runs: dict[str, Run] = {}
        self.active: str | None = None
        self.lock = threading.Lock()  # one job at a time: stages share in-memory state

    def resolve_tenant(self, hint: str | None, explicit: str | None, stage: str) -> str:
        tenant = explicit or hint
        if tenant:
            return tenant
        if stage == "s3":
            raise JobRejected(422, "TENANT_REQUIRED", "s3 starts a run and must name its tenant: consumes.alloydb "
                              "('alloy-<tenant>'), tenant_id, or ?tenant=<id>.")
        if self.active:
            return self.active
        raise JobRejected(409, "STAGE_ORDER", "No run in progress. Start with POST /factory/data-engine/s3.")

    def run_for(self, tenant: str, stage: str, job: DataJob | None = None) -> Run:
        run = self.runs.get(tenant)
        if stage != "s3":
            if run is None:
                raise JobRejected(409, "STAGE_ORDER", f"No run for tenant {tenant!r}. Start with POST /factory/data-engine/s3.",
                                  {"runs": sorted(self.runs)})
            return run
        settings = {"root_seed": job.root_seed, "as_of_date": job.as_of_date, "catalog_version": job.catalog_version,
                    "id_strategy": job.id_strategy}
        missing = [k for k, v in settings.items() if v is None]
        if missing:
            raise JobRejected(422, "MISSING_RUN_SETTINGS", "s3 starts the run and must carry these fields.", missing)
        if run is not None:
            held = {k: getattr(run.spec, k) for k in settings}
            if held != settings:
                raise JobRejected(409, "RUN_SETTINGS_DIFFER",
                                  f"Tenant {tenant!r} already has a run with other settings. DELETE /factory/runs/{tenant} to start over.",
                                  {"run": {k: str(v) for k, v in held.items()}, "payload": {k: str(v) for k, v in settings.items()}})
            return run
        spec = RunSpec(tenant_id=tenant, dataset_id=tenant, **settings)
        stores = build_stores(self.profile)
        run = Run(spec, stores, GenContext(spec, stores))
        self.runs[tenant] = run
        return run

    def reset(self, tenant: str) -> bool:
        if self.active == tenant:
            self.active = None
        return self.runs.pop(tenant, None) is not None


# ================================================================ data engine payloads (s3-s7)
def _check_names(stage: str, job: DataJob) -> None:
    job_name = STAGES[stage][0]
    if job.stage != stage:
        raise JobRejected(422, "WRONG_STAGE", f"This endpoint runs {stage}; payload says stage={job.stage!r}.")
    if job.job != job_name:
        raise JobRejected(422, "WRONG_JOB", f"Stage {stage} runs job {job_name!r}; payload says {job.job!r}.")


def _check_run_settings(stage: str, job: DataJob, spec: RunSpec) -> None:
    diffs = []
    for k in ("root_seed", "as_of_date", "catalog_version", "id_strategy"):
        sent = getattr(job, k)
        if sent is not None and sent != getattr(spec, k):
            diffs.append({"field": k, "sent": str(sent), "run": str(getattr(spec, k))})
    if diffs:
        raise JobRejected(409, "RUN_SETTINGS_DIFFER", f"Payload differs from the run started at s3 for tenant {spec.tenant_id}.", diffs)
    if spec.catalog_version != catalog.CATALOG_VERSION:
        raise JobRejected(422, "UNSUPPORTED_CATALOG", f"This engine implements catalog {catalog.CATALOG_VERSION}.")
    if spec.id_strategy != ID_STRATEGY:
        raise JobRejected(422, "UNSUPPORTED_ID_STRATEGY", f"Supported id_strategy: {ID_STRATEGY!r}.")
    if job.stage_seed is not None and job.stage_seed != stage_seed(spec.root_seed, stage):
        raise JobRejected(422, "BAD_STAGE_SEED", f"stage_seed must be FNV-1a('{spec.root_seed}:{stage}') = "
                          f"{stage_seed(spec.root_seed, stage)}.")


def _merge(spec: RunSpec, key: str, value: Any, stage: str, diffs: list) -> None:
    """Record a setting; a later payload may repeat it but not change it."""
    held = spec.distributions.get(key)
    if held is not None and value is not None and held != value:
        diffs.append({"field": key, "sent": value, "run": held, "stage": stage})
    elif value is not None:
        spec.distributions[key] = value


def validate_data_job(stage: str, job: DataJob, run: Run) -> list[str]:
    """Check the payload, record its settings on the run spec, return notes. Raises JobRejected."""
    spec, notes, diffs = run.spec, [], []
    _check_names(stage, job)
    needs = [s for s in STAGES[stage][2] if s not in run.completed]
    if needs:
        msg = f"{stage} needs {', '.join(needs)} to be completed first for tenant {spec.tenant_id}."
        if stage == "s6" and needs == ["s5"]:
            msg += " (v1: care records and denial-code links need claims; design doc gap 1.)"
        raise JobRejected(409, "STAGE_ORDER", msg, run.status()["stages"])
    if stage == "s7":
        return _validate_s7(job, run)
    _check_run_settings(stage, job, spec)
    tables = run.stores.tables
    missing_tables = [t for t in NEEDS_TABLES.get(stage, []) if not tables.exists(t)]
    if missing_tables:
        raise JobRejected(409, "MISSING_UPSTREAM_DATA", f"{stage} reads tables that earlier payloads did not generate.", missing_tables)

    # entities: the payload decides what is generated
    if not job.entities:
        raise JobRejected(422, "ENTITIES_REQUIRED", f"{stage} payload must list the entities to generate.")
    known = set(catalog.PRIMARY_KEY) | {e for v in catalog.ENTITIES.values() for e in v}
    unknown = [e for e in job.entities if e not in known]
    if unknown:
        raise JobRejected(422, "UNKNOWN_ENTITIES", f"Entities not in catalog {catalog.CATALOG_VERSION}.", unknown)
    not_built = [e for e in job.entities if e not in catalog.IMPLEMENTED]
    if not_built:
        raise JobRejected(422, "ENTITIES_NOT_IMPLEMENTED", "No v1 generator for these entities.", not_built)
    for e in job.entities:
        home = next(s for s, ents in catalog.STAGE_ENTITIES.items() if e in ents)
        if home != stage:
            why = " (derived from paid claims, rule F-03; design doc gap 2)" if e == "member_accumulator" else ""
            if catalog.STAGE_ENTITIES and list(STAGES).index(home) < list(STAGES).index(stage):
                raise JobRejected(409, "ENTITY_STAGE", f"{e} is generated in {home}, which has already run; list it in the {home} payload.")
            notes.append(f"{e} is generated in {home}, not {stage}{why}.")

    vol = job.volumes
    if stage == "s3":
        if "provider" in job.entities and not vol.get("providers"):
            raise JobRejected(422, "VOLUME_REQUIRED", "s3 needs volumes.providers.")
        if vol.get("plans") is not None and not 1 <= vol["plans"] <= 50:
            raise JobRejected(422, "BAD_VOLUME", "volumes.plans must be between 1 and 50.")
        if "access_standards" in vol and "access_standard" not in job.entities:
            notes.append(f"volumes.access_standards={vol['access_standards']} ignored: access_standard is not in the entity list.")
        _merge(spec, "lob_mix", job.distributions.get("lob_mix"), stage, diffs)
        if not job.distributions.get("lob_mix"):
            notes.append("No lob_mix in the s3 payload: plans cycle Medicare Advantage, Commercial, Medicaid. "
                         "s4's lob_mix chooses which plans members join.")
            spec.distributions.pop("lob_mix", None)
    elif stage == "s4":
        if not vol.get("members"):
            raise JobRejected(422, "VOLUME_REQUIRED", "s4 needs volumes.members.")
        mix = job.distributions.get("lob_mix")
        if not mix:
            raise JobRejected(422, "LOB_MIX_REQUIRED", "s4 needs distributions.lob_mix.")
        plan_lobs = set(tables.query("SELECT DISTINCT lob FROM plan").column(0).to_pylist())
        no_plan = [lob for lob in mix if lob not in plan_lobs]
        if no_plan:
            raise JobRejected(409, "NO_PLAN_FOR_LOB", f"s3 created plans for {sorted(plan_lobs)} only.", no_plan)
        spec.distributions["lob_mix"] = mix
        for k, v in job.distributions.items():
            if k != "lob_mix":
                _merge(spec, k, v, stage, diffs)
        if job.scenario_mix:
            spec.scenario_mix = list(job.scenario_mix)
        else:
            notes.append("No scenario_mix in the s4 payload: member scenarios (PEV-xx) are not injected; members are baseline rows. "
                         "Add scenario_mix to the s4 payload to inject them.")
    elif stage == "s5":
        if not vol.get("claims"):
            raise JobRejected(422, "VOLUME_REQUIRED", "s5 needs volumes.claims.")
        _merge(spec, "denial_rate", job.denial_rate if job.denial_rate is not None else job.distributions.get("denial_rate"), stage, diffs)
        if job.scenario_mix:
            sent = [s.model_dump() for s in job.scenario_mix]
            if spec.scenario_mix and [s.model_dump() for s in spec.scenario_mix] != sent:
                diffs.append({"field": "scenario_mix", "sent": sent, "run": [s.model_dump() for s in spec.scenario_mix], "stage": "s4"})
            spec.scenario_mix = list(job.scenario_mix)
        members = tables.query("SELECT count(*) FROM member_demographics").column(0)[0].as_py()
        if job.history_months:
            spec.history_months = job.history_months
        else:
            inferred = max(1, min(12, round(vol["claims"] * 12 / (max(members, 1) * 6))))
            spec.history_months = inferred
            notes.append(f"history_months inferred = {inferred}: claims {vol['claims']:,} x 12 / (members {members:,} x 6 claims/year, D-05).")
    elif stage == "s6":
        if job.use_cases:
            spec.use_cases = list(job.use_cases)
        else:
            spec.use_cases = sorted({s.scenario_id.split("-")[0] for s in spec.scenario_mix})
            notes.append(f"use_cases taken from the run's scenario_mix: {spec.use_cases}.")
        skipped = [u for u in spec.use_cases if u not in catalog.USE_CASES]
        if skipped:
            notes.append(f"No FAQ/SOP catalog for {skipped} in v1; skipped.")
        for k, v in vol.items():
            notes.append(f"volumes.{k}={v} is a target; actual counts are in entities[].")
        if job.llm:
            notes.append(f"llm.model={job.llm.get('model')!r}: this run uses {run.stores.llm.model!r} (profile {run.stores.profile}).")
    if diffs:
        raise JobRejected(409, "PAYLOAD_DIFFERS_FROM_RUN", "A setting differs from an earlier payload in this run.", diffs)

    spec.volumes.update({k: v for k, v in vol.items() if k in ("providers", "plans", "members", "claims", "authorizations")})
    spec.entities |= set(job.entities)
    if job.shards:
        run.ctx.shards = job.shards
    spec.payload_hashes[stage] = payload_hash(job.model_dump(mode="json", exclude_none=True))
    return notes


def _validate_s7(job: DataJob, run: Run) -> list[str]:
    _check_names("s7", job)
    not_done = [s for s in (job.consumes.get("from") or []) if s not in run.completed]
    if not_done:
        raise JobRejected(409, "STAGE_ORDER", f"s7 consumes {not_done}, which have not completed.")
    bad = [f for f in job.rule_sets or [] if f not in "RTFCPA"]
    if bad:
        raise JobRejected(422, "UNKNOWN_RULE_SETS", "rule_sets must be from R, T, F, C, P, A.", bad)
    run.spec.payload_hashes["s7"] = payload_hash(job.model_dump(mode="json", exclude_none=True))
    return []


# ================================================================ api engine payload (s8)
def validate_api_job(job: ApiJob, run: Run) -> list[str]:
    notes, diffs = [], []
    if job.stage != "s8":
        raise JobRejected(422, "WRONG_STAGE", f"This endpoint runs s8; payload says stage={job.stage!r}.")
    if job.job != "api_generate_and_deploy":
        raise JobRejected(422, "WRONG_JOB", f"Stage s8 runs job 'api_generate_and_deploy'; payload says {job.job!r}.")
    if "s7" not in run.completed or run.data.manifest is None:
        raise JobRejected(409, "STAGE_ORDER", "s8 needs s7 (validate_and_scan) to be completed first.", run.status()["stages"])
    if not run.data.passed:
        s7 = run.completed["s7"].model_extra
        raise JobRejected(409, "DATA_QUALITY_GATE_FAILED", "The data quality gate after s7 is red; the API engine does not deploy over failing data.",
                          {"validation": s7.get("validation"), "privacy_scan": s7.get("privacy_scan")})
    unknown = [u for u in job.use_cases if u not in ENDPOINTS]
    if unknown:
        raise JobRejected(422, "USE_CASES_NOT_IMPLEMENTED", f"v1 has endpoints for {sorted(ENDPOINTS)}.", unknown)
    if not job.base_path.startswith("/") or job.base_path.endswith("/"):
        raise JobRejected(422, "BAD_BASE_PATH", "base_path must start with '/' and not end with '/', e.g. '/v1'.")
    if job.auth.get("type", "OAuth2 client credentials") != "OAuth2 client credentials":
        raise JobRejected(422, "UNSUPPORTED_AUTH", "Supported auth.type: 'OAuth2 client credentials'.")
    missing = {uc: [t for t in USE_CASE_TABLES[uc] if not run.stores.tables.exists(t)] for uc in [*job.use_cases, "COMMON"]}
    missing = {uc: ts for uc, ts in missing.items() if ts}
    if missing:
        raise JobRejected(409, "DATA_MISSING_FOR_USE_CASE", "The generated data lacks tables these endpoints read.", missing)
    if job.endpoints is not None:
        expected = {(m, job.base_path + path, uc) for uc in [*job.use_cases, "COMMON"] for m, path in ENDPOINT_CATALOG.get(uc, [])}
        sent = {(e.method.upper(), e.path, e.use_case) for e in job.endpoints}
        if sent != expected:
            diffs.append({"field": "endpoints", "missing_from_payload": sorted(expected - sent), "not_in_catalog": sorted(sent - expected)})
    if diffs:
        raise JobRejected(409, "ENDPOINTS_DIFFER", "The endpoint list does not match the endpoint catalog for these use cases.", diffs)
    if job.auth.get("secret_ref"):
        notes.append(f"auth.secret_ref={job.auth['secret_ref']!r} recorded; profile {run.stores.profile} issues a local client "
                     "secret (see credentials_secret_ref).")
    manifest = job.consumes.get("manifest")
    if manifest and not str(manifest).endswith(f"/{run.data.manifest.root_seed}.json"):
        notes.append(f"consumes.manifest {manifest!r} does not name seed {run.data.manifest.root_seed}; the run's s7 manifest is used.")
    return notes
