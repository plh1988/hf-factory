"""Runs per tenant: the Registry of this control-plane process and each tenant's Run.

Payload validation lives in control/validation.py.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from factory.adapters import build_stores
from factory.adapters.ports import Stores
from factory.api_engine.stage import ApiEngineRun
from factory.config import Profile
from factory.contracts import DataJob, RunSpec, S2Output, StageResult
from factory.control.errors import JobRejected, RejectCode
from factory.data_engine.context import GenContext
from factory.data_engine.runner import STAGES, DataEngineRun


@dataclass
class Run:
    """One tenant's run: its spec (seed, as-of date, ...), its own stores and generation context, the stage
    results so far, and the API engine result after s8.
    """
    spec: RunSpec
    stores: Stores
    ctx: GenContext
    data: DataEngineRun = field(default_factory=DataEngineRun)
    completed: dict[str, StageResult] = field(default_factory=dict)
    api: ApiEngineRun | None = None
    updated_at: float = field(default_factory=time.time)
    limits: dict[str, int] = field(default_factory=dict)

    def status(self) -> dict[str, Any]:
        """Per-stage status (NOT_RUN until done), the next stage to call and whether the tenant API is live."""
        stages = {s: (self.completed[s].status if s in self.completed else "NOT_RUN") for s in STAGES}
        stages["s8"] = self.api.result.status if self.api else "NOT_RUN"
        return {"tenant": self.spec.tenant_id, "dataset_id": self.spec.dataset_id, "root_seed": self.spec.root_seed,
                "as_of_date": self.spec.as_of_date.isoformat(), "profile": self.stores.profile, "stages": stages,
                "next_stage": next((s for s, v in stages.items() if v == "NOT_RUN"), None),
                "api_live": self.api is not None and self.api.result.status == "DEPLOYED"}


class Registry:
    """All runs of this control-plane process, keyed by tenant. State lives in memory: a restart loses the runs
    (the data itself persists in the gcp profile). `active` is the tenant used when a payload names none.
    """
    def __init__(self, profile: Profile, s2: S2Output | None = None) -> None:
        self.profile, self.s2 = profile, s2
        self.runs: dict[str, Run] = {}
        self.active: str | None = None
        self.lock = threading.Lock()  # one job at a time: stages share in-memory state

    def resolve_tenant(self, hint: str | None, explicit: str | None, stage: str) -> str:
        """Tenant for a call: ?tenant= wins, then the payload (consumes.alloydb 'alloy-<tenant>'), then the active run.

        s3 must name its tenant because it starts the run.
        """
        tenant = explicit or hint
        if tenant:
            return tenant
        if stage == "s3":
            raise JobRejected(RejectCode.TENANT_REQUIRED, "s3 starts a run and must name its tenant: consumes.alloydb "
                              "('alloy-<tenant>'), tenant_id, or ?tenant=<id>.")
        if self.active:
            return self.active
        raise JobRejected(RejectCode.STAGE_ORDER, "No run in progress. Start with POST /factory/data-engine/s3.")

    def get(self, tenant: str) -> Run:
        """The tenant's run, or a 404 NO_RUN rejection."""
        if tenant not in self.runs:
            raise JobRejected(RejectCode.NO_RUN, f"No run for tenant {tenant!r}. Start with POST /factory/data-engine/s3.")
        return self.runs[tenant]

    def run_for(self, tenant: str, stage: str, job: DataJob | None = None) -> Run:
        """The tenant's run. s3 creates it (with namespaced stores) or, when it already exists, requires the same seed,
        as-of date, catalog version and id strategy; later stages need an existing run.
        """
        run = self.runs.get(tenant)
        if stage != "s3":
            if run is None:
                raise JobRejected(RejectCode.STAGE_ORDER, f"No run for tenant {tenant!r}. Start with POST /factory/data-engine/s3.",
                                  {"runs": sorted(self.runs)})
            return run
        settings = {"root_seed": job.root_seed, "as_of_date": job.as_of_date, "catalog_version": job.catalog_version,
                    "id_strategy": job.id_strategy}
        missing = [k for k, v in settings.items() if v is None]
        if missing:
            raise JobRejected(RejectCode.MISSING_RUN_SETTINGS, "s3 starts the run and must carry these fields.", missing)
        if run is not None:
            held = {k: getattr(run.spec, k) for k in settings}
            if held != settings:
                raise JobRejected(RejectCode.RUN_SETTINGS_DIFFER, f"Tenant {tenant!r} already has a run with other settings. DELETE /factory/runs/{tenant} to start over.",
                                  {"run": {k: str(v) for k, v in held.items()}, "payload": {k: str(v) for k, v in settings.items()}})
            return run
        spec = RunSpec(tenant_id=tenant, dataset_id=tenant, **settings)
        stores = build_stores(self.profile, namespace=tenant)
        run = Run(spec, stores, GenContext(spec, stores), limits=dict(self.profile.limits))
        self.runs[tenant] = run
        return run

    def reset(self, tenant: str) -> bool:
        """Forget the tenant's run (DELETE /factory/runs/{tenant}). True if there was one."""
        if self.active == tenant:
            self.active = None
        return self.runs.pop(tenant, None) is not None
