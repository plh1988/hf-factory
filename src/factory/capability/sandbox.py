"""Small stage payloads for gate runs: core entities plus the ones under test, about 200 members, offline LLM.

The gates run these through an in-process control plane with the memory-offline profile, so a gate never touches
a tenant's stores or the network.
"""

from __future__ import annotations

from typing import Any

from factory.data_engine import catalog
from factory.data_engine.runner import STAGES, stage_seed

SEED = 20261012
AS_OF = "2026-10-12"
LOB_MIX = {"MEDICARE_ADV": 1, "COMMERCIAL": 1, "MEDICAID_MCO": 1}


def _core(stage: str) -> list[str]:
    return [e for e in catalog.STAGE_ENTITIES[stage] if e in catalog.CORE_ENTITIES]


def payloads(tenant: str, extra_entities: dict[str, list[str]] | None = None, use_cases: list[str] | None = None,
             members: int = 200, shards: int = 2) -> dict[str, dict[str, Any]]:
    """s3..s8 payloads for a sandbox run. extra_entities: stage -> entities to generate besides the core ones."""
    extra = extra_entities or {}
    ucs = list(use_cases or [])

    def job(stage: str, **fields: Any) -> dict[str, Any]:
        return {"job": STAGES[stage].job, "stage": stage, "root_seed": SEED, "stage_seed": stage_seed(SEED, stage),
                "as_of_date": AS_OF, "catalog_version": catalog.CATALOG_VERSION,
                "id_strategy": "UUIDv5(dataset_id, entity, seed, row_index)", "consumes": {"alloydb": f"alloy-{tenant}"},
                **fields}

    out = {
        "s3": job("s3", entities=_core("s3") + extra.get("s3", []), volumes={"providers": 30, "plans": 4},
                  distributions={"lob_mix": LOB_MIX}),
        "s4": job("s4", entities=_core("s4") + extra.get("s4", []), volumes={"members": members},
                  distributions={"lob_mix": LOB_MIX}, shards=shards),
        "s5": job("s5", entities=_core("s5") + extra.get("s5", []), volumes={"claims": members, "authorizations": members // 20},
                  shards=shards),
        "s6": job("s6", entities=_core("s6") + extra.get("s6", []), use_cases=ucs or ["PCS"]),
        "s7": {"job": STAGES["s7"].job, "stage": "s7", "consumes": {"from": ["s3", "s4", "s5", "s6"], "alloydb": f"alloy-{tenant}"},
               "rule_sets": list("RTFCPA")},
    }
    if ucs:
        from factory.api_engine.endpoints import ENDPOINT_CATALOG

        endpoints = [{"method": m, "path": "/v1" + p, "use_case": uc} for uc in [*ucs, "COMMON"] for m, p in ENDPOINT_CATALOG[uc]]
        out["s8"] = {"job": "api_generate_and_deploy", "stage": "s8", "base_path": "/v1", "use_cases": ucs,
                     "consumes": {"from": ["s7"], "alloydb": f"alloy-{tenant}"}, "endpoints": endpoints,
                     "fault_injection": {"error_rate_pct": 0, "latency_p95_ms": 0}}
    return out
