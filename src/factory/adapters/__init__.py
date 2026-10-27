"""Builds adapters from the active profile. The ONLY place that maps a profile to backends.

namespace (the tenant) isolates runs: an AlloyDB schema, a Cloud Storage prefix, a Vector Search restrict,
a Pub/Sub attribute. The memory adapters are per-run objects already and ignore it.
"""

from __future__ import annotations

from typing import Any, Callable

from factory.adapters.ports import Stores
from factory.config import AdapterConfig, Profile


def _registry() -> dict[str, dict[str, Callable[[AdapterConfig, str | None], Any]]]:
    """port -> kind -> constructor. GCP classes are imported lazily so memory profiles need no Google libraries."""
    from factory.adapters import llm, memory
    from factory.adapters.deploy import CloudRunDeployer, LocalDeployer
    from factory.adapters.jobs import CloudRunJobExecutor, LocalExecutor

    def gcp(name: str) -> Callable[[AdapterConfig, str | None], Any]:
        def make(cfg: AdapterConfig, ns: str | None) -> Any:
            from factory.adapters import gcp as g  # Google Cloud libraries load only for the gcp profile

            return getattr(g, name)(cfg, ns)
        return make

    return {
        "tables": {"duckdb": lambda c, ns: memory.DuckDBTableStore(), "alloydb": gcp("AlloyDBTableStore")},
        "blobs": {"memory": lambda c, ns: memory.MemoryBlobStore(), "gcs": gcp("GCSBlobStore")},
        "vectors": {"numpy": lambda c, ns: memory.NumpyVectorIndex(), "vertex_vector_search": gcp("VertexVectorIndex")},
        "events": {"log": lambda c, ns: memory.LogEventBus(), "pubsub": gcp("PubSubEventBus")},
        "text_cache": {"memory": lambda c, ns: memory.MemoryTextCache(), "gcs": gcp("GCSTextCache")},
        "llm": {"offline": lambda c, ns: llm.OfflineLLM(c), "vertex": lambda c, ns: llm.VertexLLM(c),
                "openai_compatible": lambda c, ns: llm.OpenAICompatibleLLM(c)},
        "deployer": {"local": lambda c, ns: LocalDeployer(c), "cloud_run": CloudRunDeployer},
        "executor": {"local": LocalExecutor, "cloud_run_job": CloudRunJobExecutor},
        "analytics": {"bigquery": gcp("BigQueryAnalyticsStore")},  # "tables" is built below: it needs the TableStore
    }


def build_stores(profile: Profile, namespace: str | None = None) -> Stores:
    """Construct every adapter of a profile for one tenant namespace.

    analytics kind 'tables' reuses the TableStore (memory profiles); 'bigquery' mirrors claims entities to BigQuery.
    """
    reg = _registry()

    def make(port: str) -> Any:
        cfg: AdapterConfig = getattr(profile, port)
        try:
            factory = reg[port][cfg.kind]
        except KeyError:
            raise ValueError(f"profile {profile.name!r}: unknown {port} kind {cfg.kind!r}") from None
        return factory(cfg, namespace)

    tables = make("tables")
    if profile.analytics.kind == "tables":
        from factory.adapters.memory import TablesAnalyticsStore

        analytics = TablesAnalyticsStore(tables)
    else:
        analytics = make("analytics")
    return Stores(
        profile=profile.name,
        tables=tables,
        blobs=make("blobs"),
        vectors=make("vectors"),
        events=make("events"),
        text_cache=make("text_cache"),
        llm=make("llm"),
        deployer=make("deployer"),
        executor=make("executor"),
        analytics=analytics,
        namespace=namespace,
    )
