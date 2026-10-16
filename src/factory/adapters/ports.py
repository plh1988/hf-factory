"""Ports: the only storage/LLM interfaces engine code may use.

Each port has a memory implementation (adapters/memory.py, adapters/llm.py) and a GCP
implementation (adapters/gcp.py). Engine code never imports duckdb, google.cloud or
similar directly; tests/test_portability.py enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pyarrow as pa


class TableStore(Protocol):
    """Serving tables (AlloyDB in production). Also used for analytics in memory profiles."""

    uri: str

    def upsert(self, entity: str, rows: pa.Table, key: str) -> None: ...
    def read(self, entity: str, columns: list[str] | None = None) -> pa.Table: ...
    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table: ...
    def exists(self, entity: str) -> bool: ...
    def health(self) -> str: ...


class AnalyticsStore(Protocol):
    """Claims analytics (BigQuery in production). load() mirrors an entity's rows; query() runs analytics SQL."""

    uri: str

    def wants(self, entity: str) -> bool: ...
    def load(self, entity: str, rows: pa.Table, key: str) -> None: ...
    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table: ...
    def health(self) -> str: ...


class BlobStore(Protocol):
    """Documents, reports, manifests (Cloud Storage in production)."""

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str = "") -> list[str]: ...
    def health(self) -> str: ...


class VectorIndex(Protocol):
    """Knowledge chunk index (Vertex AI Vector Search in production)."""

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict[str, Any]]) -> None: ...
    def search(self, vector: np.ndarray, k: int = 5, where: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def health(self) -> str: ...


class EventBus(Protocol):
    """Progress events (Pub/Sub in production)."""

    def publish(self, topic: str, event: dict[str, Any]) -> None: ...
    def health(self) -> str: ...


class TextCache(Protocol):
    """Content-addressed LLM text cache (Cloud Storage in production)."""

    def get(self, key: str) -> str | None: ...
    def put(self, key: str, text: str) -> None: ...
    def health(self) -> str: ...


class LLMClient(Protocol):
    """Text generation and embeddings (Vertex AI in production and in the memory profile)."""

    model: str
    embedding_model: str
    embedding_dim: int

    def generate(self, prompt: str, system: str = "") -> str: ...
    def embed(self, texts: list[str], task: str = "document") -> np.ndarray: ...  # task: document | query
    def health(self) -> str: ...


@dataclass
class Stores:
    """Everything an engine needs, built once from the active profile."""

    profile: str
    tables: TableStore
    blobs: BlobStore
    vectors: VectorIndex
    events: EventBus
    text_cache: TextCache
    llm: LLMClient
    deployer: Any
    executor: Any = None
    analytics: AnalyticsStore | None = None
    namespace: str | None = None
