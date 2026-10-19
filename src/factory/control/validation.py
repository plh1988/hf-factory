"""Job-payload validation for s3-s8. No blueprint: every setting comes from the payloads.

A run starts with the s3 payload (seed, as-of date, catalog, id strategy, tenant from consumes.alloydb).
Later payloads must agree with what the run already holds; they add volumes, distributions, scenario mix
and entities. Gaps in the spec payloads are filled by documented derivations and reported as notes:

  s3  no LOB mix          -> plans cycle MA / Commercial / Medicaid (optional distributions.lob_mix)
  s4  no scenario_mix     -> member scenarios (PEV-xx) are not injected (optional scenario_mix)
  s5  no history window   -> history_months = claims x 12 / (members x 6), D-05 median 6 claims/member/year
  s6  no use cases        -> taken from the run's scenario_mix ids (optional use_cases)

Structure: validate_data_job checks the payload names and stage order, then calls the stage's validator from
DATA_VALIDATORS. Generation stages (s3-s6) share the checks in _generation_stage (run settings, upstream
tables, entities, profile limits) and add their own rules; s7 has its own. Every validator raises JobRejected
or returns notes for the response, and records accepted settings on the run spec.
"""

from __future__ import annotations

from typing import Any, Callable

from factory.api_engine.endpoints import ENDPOINT_CATALOG, ENDPOINTS
from factory.contracts import ApiJob, DataJob, RunSpec, payload_hash
from factory.control.errors import JobRejected, RejectCode
from factory.control.registry import Run
from factory.data_engine import catalog
from factory.data_engine.runner import STAGES, stage_seed

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
    "MBN": ["member_demographics", "plan_enrollment", "plan"],
    "MCI": ["member_demographics", "claim_header", "claim_line"],
    "MPS": ["provider", "network_participation", "practice_location"],
    "MPC": ["member_demographics", "plan_enrollment", "pcp_assignment", "provider", "network_participation", "practice_location"],
    "MAS": ["member_demographics", "plan_enrollment", "provider", "network_participation", "availability_schedule"],
    "PAP": ["claim_header", "claim_denial", "appeal_record", "provider"],
    "PPI": ["claim_header", "claim_payment", "provider"],
    "COMMON": ["provider", "member_demographics", "asset_chunk"],
}

StageValidator = Callable[[str, DataJob, Run], list[str]]  # (stage, job, run) -> notes
SettingsRule = Callable[[DataJob, Run, list], list[str]]  # (job, run, diffs) -> notes


# ================================================================ entry point (s3-s7)
def validate_data_job(stage: str, job: DataJob, run: Run) -> list[str]:
    """Check the payload, record its settings on the run spec, return notes. Raises JobRejected."""
    _check_names(stage, job)
    _check_order(stage, run)
    return DATA_VALIDATORS[stage](stage, job, run)


def _check_names(stage: str, job: DataJob) -> None:
    """Reject a payload sent to the wrong endpoint (its stage or job name does not match)."""
    job_name = STAGES[stage].job
    if job.stage != stage:
        raise JobRejected(RejectCode.WRONG_STAGE, f"This endpoint runs {stage}; payload says stage={job.stage!r}.")
    if job.job != job_name:
        raise JobRejected(RejectCode.WRONG_JOB, f"Stage {stage} runs job {job_name!r}; payload says {job.job!r}.")


def _check_order(stage: str, run: Run) -> None:
    """Reject a stage whose prerequisite stages have not completed."""
    needs = [s for s in STAGES[stage].needs if s not in run.completed]
    if needs:
        msg = f"{stage} needs {', '.join(needs)} to be completed first for tenant {run.spec.tenant_id}."
        if stage == "s6" and needs == ["s5"]:
            msg += " (v1: care records and denial-code links need claims; design doc gap 1.)"
        raise JobRejected(RejectCode.STAGE_ORDER, msg, run.status()["stages"])


# ================================================================ generation stages (s3-s6): shared checks
def _generation_stage(settings_rule: SettingsRule) -> StageValidator:
    """Validator for a generation stage: the shared checks, then the stage's own settings rule."""

    def validate(stage: str, job: DataJob, run: Run) -> list[str]:
        spec = run.spec
        _check_run_settings(stage, job, spec)
        _check_upstream_tables(stage, run)
        notes = _check_entities(stage, job)
        _check_limits(job, run)
        diffs: list = []
        notes += settings_rule(job, run, diffs)
        if diffs:
            raise JobRejected(RejectCode.PAYLOAD_DIFFERS_FROM_RUN, "A setting differs from an earlier payload in this run.", diffs)
        spec.volumes.update({k: v for k, v in job.volumes.items() if k in ("providers", "plans", "members", "claims", "authorizations")})
        spec.entities |= set(job.entities)
        if job.shards:
            run.ctx.shards = job.shards
        spec.payload_hashes[stage] = payload_hash(job.model_dump(mode="json", exclude_none=True))
        return notes

    return validate


def _check_run_settings(stage: str, job: DataJob, spec: RunSpec) -> None:
    """Reject a later payload whose seed, as-of date, catalog version or id strategy differs from the run's."""
    diffs = []
    for k in ("root_seed", "as_of_date", "catalog_version", "id_strategy"):
        sent = getattr(job, k)
        if sent is not None and sent != getattr(spec, k):
            diffs.append({"field": k, "sent": str(sent), "run": str(getattr(spec, k))})
    if diffs:
        raise JobRejected(RejectCode.RUN_SETTINGS_DIFFER, f"Payload differs from the run started at s3 for tenant {spec.tenant_id}.", diffs)
    if spec.catalog_version != catalog.CATALOG_VERSION:
        raise JobRejected(RejectCode.UNSUPPORTED_CATALOG, f"This engine implements catalog {catalog.CATALOG_VERSION}.")
    if spec.id_strategy != ID_STRATEGY:
        raise JobRejected(RejectCode.UNSUPPORTED_ID_STRATEGY, f"Supported id_strategy: {ID_STRATEGY!r}.")
    if job.stage_seed is not None and job.stage_seed != stage_seed(spec.root_seed, stage):
        raise JobRejected(RejectCode.BAD_STAGE_SEED, f"stage_seed must be FNV-1a('{spec.root_seed}:{stage}') = "
                          f"{stage_seed(spec.root_seed, stage)}.")


def _check_upstream_tables(stage: str, run: Run) -> None:
    missing_tables = [t for t in NEEDS_TABLES.get(stage, []) if not run.stores.tables.exists(t)]
    if missing_tables:
        raise JobRejected(RejectCode.MISSING_UPSTREAM_DATA, f"{stage} reads tables that earlier payloads did not generate.", missing_tables)


def _check_entities(stage: str, job: DataJob) -> list[str]:
    """The payload decides what is generated: entities must be known, implemented, and not belong to a stage that ran."""
    notes: list[str] = []
    if not job.entities:
        raise JobRejected(RejectCode.ENTITIES_REQUIRED, f"{stage} payload must list the entities to generate.")
    known = set(catalog.PRIMARY_KEY) | {e for v in catalog.ENTITIES.values() for e in v}
    unknown = [e for e in job.entities if e not in known]
    if unknown:
        raise JobRejected(RejectCode.UNKNOWN_ENTITIES, f"Entities not in catalog {catalog.CATALOG_VERSION}.", unknown)
    not_built = [e for e in job.entities if e not in catalog.IMPLEMENTED]
    if not_built:
        raise JobRejected(RejectCode.ENTITIES_NOT_IMPLEMENTED, "No v1 generator for these entities.", not_built)
    for e in job.entities:
        home = next(s for s, ents in catalog.STAGE_ENTITIES.items() if e in ents)
        if home != stage:
            why = " (derived from paid claims, rule F-03; design doc gap 2)" if e == "member_accumulator" else ""
            if catalog.STAGE_ENTITIES and list(STAGES).index(home) < list(STAGES).index(stage):
                raise JobRejected(RejectCode.ENTITY_STAGE, f"{e} is generated in {home}, which has already run; list it in the {home} payload.")
            notes.append(f"{e} is generated in {home}, not {stage}{why}.")
    return notes


def _check_limits(job: DataJob, run: Run) -> None:
    """Memory profiles cap volumes so a run fits in RAM."""
    vol, limits = job.volumes, run.limits
    for key, cap in (("members", limits.get("max_members")), ("claims", limits.get("max_claims"))):
        if cap and vol.get(key, 0) > cap:
            raise JobRejected(RejectCode.PAYLOAD_TOO_LARGE_FOR_PROFILE, f"volumes.{key}={vol[key]:,} exceeds this profile's limit of {cap:,}. Scale the payload down "
                              f"(keep claims per member the same) or use the gcp profile.", {"limit": cap, "sent": vol[key]})


def _merge(spec: RunSpec, key: str, value: Any, stage: str, diffs: list) -> None:
    """Record a setting; a later payload may repeat it but not change it."""
    held = spec.distributions.get(key)
    if held is not None and value is not None and held != value:
        diffs.append({"field": key, "sent": value, "run": held, "stage": stage})
    elif value is not None:
        spec.distributions[key] = value


# ================================================================ generation stages: per-stage rules
def _s3_settings(job: DataJob, run: Run, diffs: list) -> list[str]:
    """Reference data: provider and plan volumes, optional LOB mix for plans."""
    spec, vol, notes = run.spec, job.volumes, []
    if "provider" in job.entities and not vol.get("providers"):
        raise JobRejected(RejectCode.VOLUME_REQUIRED, "s3 needs volumes.providers.")
    if vol.get("plans") is not None and not 1 <= vol["plans"] <= 50:
        raise JobRejected(RejectCode.BAD_VOLUME, "volumes.plans must be between 1 and 50.")
    if "access_standards" in vol and "access_standard" not in job.entities:
        notes.append(f"volumes.access_standards={vol['access_standards']} ignored: access_standard is not in the entity list.")
    _merge(spec, "lob_mix", job.distributions.get("lob_mix"), "s3", diffs)
    if not job.distributions.get("lob_mix"):
        notes.append("No lob_mix in the s3 payload: plans cycle Medicare Advantage, Commercial, Medicaid. "
                     "s4's lob_mix chooses which plans members join.")
        spec.distributions.pop("lob_mix", None)
    return notes


def _s4_settings(job: DataJob, run: Run, diffs: list) -> list[str]:
    """Members: member volume and an LOB mix whose LOBs have plans; optional member scenario mix."""
    spec, vol, notes = run.spec, job.volumes, []
    if not vol.get("members"):
        raise JobRejected(RejectCode.VOLUME_REQUIRED, "s4 needs volumes.members.")
    mix = job.distributions.get("lob_mix")
    if not mix:
        raise JobRejected(RejectCode.LOB_MIX_REQUIRED, "s4 needs distributions.lob_mix.")
    plan_lobs = set(run.stores.tables.query("SELECT DISTINCT lob FROM plan").column(0).to_pylist())
    no_plan = [lob for lob in mix if lob not in plan_lobs]
    if no_plan:
        raise JobRejected(RejectCode.NO_PLAN_FOR_LOB, f"s3 created plans for {sorted(plan_lobs)} only.", no_plan)
    spec.distributions["lob_mix"] = mix
    for k, v in job.distributions.items():
        if k != "lob_mix":
            _merge(spec, k, v, "s4", diffs)
    if job.scenario_mix:
        spec.scenario_mix = list(job.scenario_mix)
    else:
        notes.append("No scenario_mix in the s4 payload: member scenarios (PEV-xx) are not injected; members are baseline rows. "
                     "Add scenario_mix to the s4 payload to inject them.")
    return notes


def _s5_settings(job: DataJob, run: Run, diffs: list) -> list[str]:
    """Claims and auths: claim volume, denial rate, the scenario mix (must match s4's), history window."""
    spec, vol, notes = run.spec, job.volumes, []
    if not vol.get("claims"):
        raise JobRejected(RejectCode.VOLUME_REQUIRED, "s5 needs volumes.claims.")
    _merge(spec, "denial_rate", job.denial_rate if job.denial_rate is not None else job.distributions.get("denial_rate"), "s5", diffs)
    if job.scenario_mix:
        sent = [s.model_dump() for s in job.scenario_mix]
        if spec.scenario_mix and [s.model_dump() for s in spec.scenario_mix] != sent:
            diffs.append({"field": "scenario_mix", "sent": sent, "run": [s.model_dump() for s in spec.scenario_mix], "stage": "s4"})
        spec.scenario_mix = list(job.scenario_mix)
    members = run.stores.tables.query("SELECT count(*) FROM member_demographics").column(0)[0].as_py()
    if job.history_months:
        spec.history_months = job.history_months
    else:
        inferred = max(1, min(12, round(vol["claims"] * 12 / (max(members, 1) * 6))))
        spec.history_months = inferred
        notes.append(f"history_months inferred = {inferred}: claims {vol['claims']:,} x 12 / (members {members:,} x 6 claims/year, D-05).")
    return notes


def _s6_settings(job: DataJob, run: Run, diffs: list) -> list[str]:
    """Care and knowledge: use cases (default: from the scenario mix); volumes and llm model are informational."""
    spec, notes = run.spec, []
    if job.use_cases:
        spec.use_cases = list(job.use_cases)
    else:
        spec.use_cases = sorted({s.scenario_id.split("-")[0] for s in spec.scenario_mix})
        notes.append(f"use_cases taken from the run's scenario_mix: {spec.use_cases}.")
    skipped = [u for u in spec.use_cases if u not in catalog.USE_CASES]
    if skipped:
        notes.append(f"No FAQ/SOP catalog for {skipped} in v1; skipped.")
    for k, v in job.volumes.items():
        notes.append(f"volumes.{k}={v} is a target; actual counts are in entities[].")
    if job.llm:
        notes.append(f"llm.model={job.llm.get('model')!r}: this run uses {run.stores.llm.model!r} (profile {run.stores.profile}).")
    return notes


# ================================================================ validation stage (s7)
def _validate_s7(stage: str, job: DataJob, run: Run) -> list[str]:
    """s7 needs the stages it consumes to be completed, and rule_sets limited to the families R, T, F, C, P, A."""
    not_done = [s for s in (job.consumes.get("from") or []) if s not in run.completed]
    if not_done:
        raise JobRejected(RejectCode.STAGE_ORDER, f"s7 consumes {not_done}, which have not completed.")
    bad = [f for f in job.rule_sets or [] if f not in "RTFCPA"]
    if bad:
        raise JobRejected(RejectCode.UNKNOWN_RULE_SETS, "rule_sets must be from R, T, F, C, P, A.", bad)
    run.spec.payload_hashes["s7"] = payload_hash(job.model_dump(mode="json", exclude_none=True))
    return []


DATA_VALIDATORS: dict[str, StageValidator] = {
    "s3": _generation_stage(_s3_settings),
    "s4": _generation_stage(_s4_settings),
    "s5": _generation_stage(_s5_settings),
    "s6": _generation_stage(_s6_settings),
    "s7": _validate_s7,
}
assert set(DATA_VALIDATORS) == set(STAGES), "every stage needs a validator"


# ================================================================ api engine payload (s8)
def validate_api_job(job: ApiJob, run: Run) -> list[str]:
    """Check the s8 payload: s7 completed with a green quality gate, known use cases, and the data each use case
    reads is present. Returns notes for the response.
    """
    notes, diffs = [], []
    if job.stage != "s8":
        raise JobRejected(RejectCode.WRONG_STAGE, f"This endpoint runs s8; payload says stage={job.stage!r}.")
    if job.job != "api_generate_and_deploy":
        raise JobRejected(RejectCode.WRONG_JOB, f"Stage s8 runs job 'api_generate_and_deploy'; payload says {job.job!r}.")
    if "s7" not in run.completed or run.data.manifest is None:
        raise JobRejected(RejectCode.STAGE_ORDER, "s8 needs s7 (validate_and_scan) to be completed first.", run.status()["stages"])
    if not run.data.passed:
        s7 = run.completed["s7"].model_extra
        raise JobRejected(RejectCode.DATA_QUALITY_GATE_FAILED, "The data quality gate after s7 is red; the API engine does not deploy over failing data.",
                          {"validation": s7.get("validation"), "privacy_scan": s7.get("privacy_scan")})
    unknown = [u for u in job.use_cases if u not in ENDPOINTS]
    if unknown:
        raise JobRejected(RejectCode.USE_CASES_NOT_IMPLEMENTED, f"v1 has endpoints for {sorted(ENDPOINTS)}.", unknown)
    if not job.base_path.startswith("/") or job.base_path.endswith("/"):
        raise JobRejected(RejectCode.BAD_BASE_PATH, "base_path must start with '/' and not end with '/', e.g. '/v1'.")
    if job.auth.get("type", "OAuth2 client credentials") != "OAuth2 client credentials":
        raise JobRejected(RejectCode.UNSUPPORTED_AUTH, "Supported auth.type: 'OAuth2 client credentials'.")
    missing = {uc: [t for t in USE_CASE_TABLES[uc] if not run.stores.tables.exists(t)] for uc in [*job.use_cases, "COMMON"]}
    missing = {uc: ts for uc, ts in missing.items() if ts}
    if missing:
        raise JobRejected(RejectCode.DATA_MISSING_FOR_USE_CASE, "The generated data lacks tables these endpoints read.", missing)
    if job.endpoints is not None:
        expected = {(m, job.base_path + path, uc) for uc in [*job.use_cases, "COMMON"] for m, path in ENDPOINT_CATALOG.get(uc, [])}
        sent = {(e.method.upper(), e.path, e.use_case) for e in job.endpoints}
        if sent != expected:
            diffs.append({"field": "endpoints", "missing_from_payload": sorted(expected - sent), "not_in_catalog": sorted(sent - expected)})
    if diffs:
        raise JobRejected(RejectCode.ENDPOINTS_DIFFER, "The endpoint list does not match the endpoint catalog for these use cases.", diffs)
    if job.auth.get("secret_ref"):
        notes.append(f"auth.secret_ref={job.auth['secret_ref']!r} recorded; profile {run.stores.profile} issues a local client "
                     "secret (see credentials_secret_ref).")
    manifest = job.consumes.get("manifest")
    if manifest and not str(manifest).endswith(f"/{run.data.manifest.root_seed}.json"):
        notes.append(f"consumes.manifest {manifest!r} does not name seed {run.data.manifest.root_seed}; the run's s7 manifest is used.")
    return notes
