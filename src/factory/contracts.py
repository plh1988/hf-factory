"""Contracts: the stage job payloads (as in the architecture spec) and what the engines return.

The engines take everything from their job payloads; there is no blueprint. A run's settings
(RunSpec) are assembled from the payloads received so far, starting with s3. Payload models allow
extra fields so the spec payloads are accepted unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def canonical_hash(obj: Any) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- job payloads
class ScenarioShare(BaseModel):
    scenario_id: str
    share_pct: float


class DataJob(_Open):
    """Payload for data engine stages s3-s7 (spec section 5, scenario S1)."""

    job: str
    stage: str
    root_seed: int | None = None
    stage_seed: int | None = None
    as_of_date: date | None = None
    catalog_version: str | None = None
    id_strategy: str | None = None
    shards: int | None = Field(None, ge=1, le=64)
    consumes: dict[str, Any] = Field(default_factory=dict)
    entities: list[str] | None = None
    volumes: dict[str, int] = Field(default_factory=dict)
    distributions: dict[str, Any] = Field(default_factory=dict)
    personas: list[str] | None = None
    scenario_mix: list[ScenarioShare] | None = None
    denial_rate: float | None = Field(None, ge=0, le=1)
    rules: list[str] | None = None
    llm: dict[str, Any] | None = None
    rule_sets: list[str] | None = None
    privacy_scan: dict[str, bool] | None = None
    # optional extensions (not in the spec payloads)
    tenant_id: str | None = None
    history_months: int | None = Field(None, ge=1, le=60)
    use_cases: list[str] | None = None

    def tenant_hint(self) -> str | None:
        alloy = self.consumes.get("alloydb")
        if self.tenant_id:
            return self.tenant_id
        if isinstance(alloy, str) and alloy.startswith("alloy-"):
            return alloy[len("alloy-"):]
        return None


class Endpoint(BaseModel):
    method: str
    path: str
    use_case: str


class FaultInjection(BaseModel):
    error_rate_pct: float = Field(0, ge=0, le=100)
    latency_p95_ms: int = Field(0, ge=0, le=60000)


class ApiJob(_Open):
    """Payload for API engine stage s8."""

    job: str
    stage: str
    style: str | None = None
    base_path: str = "/v1"
    use_cases: list[str]
    consumes: dict[str, Any] = Field(default_factory=dict)
    endpoints: list[Endpoint] | None = None
    auth: dict[str, Any] = Field(default_factory=lambda: {"type": "OAuth2 client credentials"})
    rate_limit: dict[str, int] = Field(default_factory=lambda: {"per_client_rpm": 600})
    fault_injection: FaultInjection = FaultInjection()
    agent_tools: bool = True
    tenant_id: str | None = None

    def tenant_hint(self) -> str | None:
        alloy = self.consumes.get("alloydb")
        if self.tenant_id:
            return self.tenant_id
        if isinstance(alloy, str) and alloy.startswith("alloy-"):
            return alloy[len("alloy-"):]
        return None


# ---------------------------------------------------------------- run settings built from payloads
class RunSpec(BaseModel):
    """Everything the data engine knows about a run, taken only from the payloads received."""

    tenant_id: str
    dataset_id: str
    root_seed: int
    as_of_date: date
    catalog_version: str
    id_strategy: str
    entities: set[str] = Field(default_factory=set)
    volumes: dict[str, int] = Field(default_factory=dict)
    distributions: dict[str, Any] = Field(default_factory=dict)
    scenario_mix: list[ScenarioShare] = Field(default_factory=list)
    history_months: int | None = None
    use_cases: list[str] = Field(default_factory=list)
    payload_hashes: dict[str, str] = Field(default_factory=dict)

    def data_spec_hash(self) -> str:
        """Hash of the data-affecting payloads s3-s6 (shards excluded: they never change the data)."""
        return canonical_hash({k: v for k, v in sorted(self.payload_hashes.items()) if k in ("s3", "s4", "s5", "s6")})


def payload_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({k: v for k, v in payload.items() if k not in ("shards", "tenant_id")})


class S2Output(_Open):
    """Outputs of infra stage s2: where the data stores live (used by the gcp profile)."""

    project_id: str | None = None
    region: str
    alloydb: str
    bigquery_dataset: str
    docs_bucket: str
    vector_index: str
    pubsub_topic: str

    @classmethod
    def load(cls, path: str | Path) -> "S2Output":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- results
class EntityResult(BaseModel):
    entity: str
    rows: int
    checksum: str


class StageResult(_Open):
    stage: str
    status: Literal["COMPLETED", "FAILED", "DEPLOYED"]
    duration_s: float
    entities: list[EntityResult] = Field(default_factory=list)
    rule_checks: dict[str, str] = Field(default_factory=dict)
    loaded_to: str | None = None
    error: str | None = None


class Manifest(_Open):
    tenant_id: str
    dataset_id: str
    data_spec_hash: str
    payload_hashes: dict[str, str]
    root_seed: int
    as_of_date: date
    catalog_version: str
    generator_version: str
    entity_counts: dict[str, int]
    entity_checksums: dict[str, str]
    rule_report: dict[str, Any]
    privacy_scan: str
