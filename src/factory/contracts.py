"""Contracts between factory layers, as shown in the architecture spec.

Only the fields the Data and API engines read are typed; everything else is kept
(extra="allow") so the blueprint round-trips unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Domain names as they appear in the blueprint -> short codes used by the catalog.
DOMAIN_CODES = {
    "Member": "M",
    "Provider": "P",
    "Claims": "C",
    "Authorization": "A",
    "Care Management": "CM",
    "Knowledge Assets": "K",
}


def canonical_hash(obj: Any) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Repeatability(_Open):
    root_seed: int
    as_of_date: date
    catalog_version: str
    rule_set_version: str
    generator_version: str
    id_strategy: str


class Tenant(_Open):
    id: str
    env: str
    ttl_hours: int


class DomainScope(_Open):
    domain: str
    role: Literal["primary", "supporting"]
    entities: list[str] | None = None

    @property
    def code(self) -> str:
        return DOMAIN_CODES[self.domain]


class Scope(_Open):
    use_cases: list[str]
    personas: list[str]
    channels: list[str]
    domains: list[DomainScope]


class ScenarioShare(BaseModel):
    scenario_id: str
    share_pct: float


class Volumes(_Open):
    members: int
    providers: int
    claims: int
    authorizations: int


class DataSpec(_Open):
    dataset_id: str
    volumes: Volumes
    history_months: int = 12
    distributions: dict[str, Any]
    scenario_mix: list[ScenarioShare]
    edge_case_pct: float
    rule_sets: list[str]


class FaultInjection(BaseModel):
    error_rate_pct: float = 0
    latency_p95_ms: int = 0


class ApiSpec(_Open):
    base_path: str = "/v1"
    auth: str
    fault_injection: FaultInjection = FaultInjection()


class InfraSpec(_Open):
    cloud: Literal["GCP"]
    region: str
    tier: str
    sizing: dict[str, Any]


class Blueprint(_Open):
    blueprint_id: str
    blueprint_version: str
    repeatability: Repeatability
    tenant: Tenant
    scope: Scope
    data_spec: DataSpec
    infra_spec: InfraSpec
    api_spec: ApiSpec

    @property
    def blueprint_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def data_spec_hash(self) -> str:
        """Hash of everything that affects generated data (not infra, not API)."""
        r = self.repeatability
        core = {
            "dataset_id": self.data_spec.dataset_id,
            "root_seed": r.root_seed,
            "as_of_date": r.as_of_date,
            "catalog_version": r.catalog_version,
            "rule_set_version": r.rule_set_version,
            "generator_version": r.generator_version,
            "use_cases": self.scope.use_cases,
            "domains": [d.model_dump(mode="json") for d in self.scope.domains],
            "data_spec": self.data_spec.model_dump(mode="json"),
        }
        return canonical_hash(core)

    @classmethod
    def load(cls, path: str | Path) -> "Blueprint":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class S2Output(_Open):
    """Outputs of infra stage s2: where the data stores live."""

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


class StageJobSpec(_Open):
    """What the orchestrator sends to an engine for one stage."""

    job: str
    stage: str
    root_seed: int
    stage_seed: int
    as_of_date: date
    catalog_version: str
    id_strategy: str
    shards: int
    entities: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    consumes: dict[str, Any] = Field(default_factory=dict)


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
    blueprint_hash: str
    data_spec_hash: str
    root_seed: int
    as_of_date: date
    catalog_version: str
    generator_version: str
    entity_counts: dict[str, int]
    entity_checksums: dict[str, str]
    rule_report: dict[str, Any]
    privacy_scan: str
