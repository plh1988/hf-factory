"""Builds adapters from the active profile. The ONLY place that maps a profile to backends."""

from __future__ import annotations

from typing import Any, Callable

from factory.adapters.ports import Stores
from factory.config import AdapterConfig, Profile


def _memory_tables(cfg: AdapterConfig) -> Any:
    from factory.adapters.memory import DuckDBTableStore

    return DuckDBTableStore()


def _registry() -> dict[str, dict[str, Callable[[AdapterConfig], Any]]]:
    from factory.adapters import gcp, llm, memory
    from factory.adapters.deploy import CloudRunDeployer, LocalDeployer

    return {
        "tables": {"duckdb": _memory_tables, "alloydb": gcp.AlloyDBTableStore},
        "blobs": {"memory": lambda c: memory.MemoryBlobStore(), "gcs": gcp.GCSBlobStore},
        "vectors": {"numpy": lambda c: memory.NumpyVectorIndex(), "vertex_vector_search": gcp.VertexVectorIndex},
        "events": {"log": lambda c: memory.LogEventBus(), "pubsub": gcp.PubSubEventBus},
        "text_cache": {"memory": lambda c: memory.MemoryTextCache(), "gcs": gcp.GCSTextCache},
        "llm": {"offline": llm.OfflineLLM, "vertex": llm.VertexLLM},
        "deployer": {"local": LocalDeployer, "cloud_run": CloudRunDeployer},
    }


def build_stores(profile: Profile) -> Stores:
    reg = _registry()

    def make(port: str) -> Any:
        cfg: AdapterConfig = getattr(profile, port)
        try:
            factory = reg[port][cfg.kind]
        except KeyError:
            raise ValueError(f"profile {profile.name!r}: unknown {port} kind {cfg.kind!r}") from None
        return factory(cfg)

    return Stores(
        profile=profile.name,
        tables=make("tables"),
        blobs=make("blobs"),
        vectors=make("vectors"),
        events=make("events"),
        text_cache=make("text_cache"),
        llm=make("llm"),
        deployer=make("deployer"),
    )
