"""GCP adapters. v1: final signatures, not yet implemented.

Each class mirrors its memory counterpart so switching FACTORY_PROFILE needs no code change.
Implementation notes per adapter say which client library to use.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from factory.config import AdapterConfig


class _NotYet:
    impl_note = ""

    def __init__(self, cfg: AdapterConfig) -> None:
        self.cfg = cfg

    def _todo(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError(f"{type(self).__name__} is not implemented in v1: {self.impl_note}")

    def health(self) -> str:
        return f"not implemented in v1 ({self.impl_note})"


class AlloyDBTableStore(_NotYet):
    impl_note = "psycopg 3 + AlloyDB connector; COPY into a staging table, then INSERT ... ON CONFLICT"

    @property
    def uri(self) -> str:
        return f"alloydb://{self.cfg.get('instance')}"

    def upsert(self, entity: str, rows: pa.Table, key: str) -> None:
        self._todo()

    def read(self, entity: str, columns: list[str] | None = None) -> pa.Table:
        return self._todo()

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        return self._todo()

    def exists(self, entity: str) -> bool:
        return self._todo()


class GCSBlobStore(_NotYet):
    impl_note = "google-cloud-storage; key = object name under the tenant docs bucket"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        return self._todo()

    def get(self, key: str) -> bytes:
        return self._todo()

    def list(self, prefix: str = "") -> list[str]:
        return self._todo()


class VertexVectorIndex(_NotYet):
    impl_note = "google-cloud-aiplatform MatchingEngineIndex upsert_datapoints / find_neighbors with restricts"

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict[str, Any]]) -> None:
        self._todo()

    def search(self, vector: np.ndarray, k: int = 5, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._todo()


class PubSubEventBus(_NotYet):
    impl_note = "google-cloud-pubsub PublisherClient; JSON message body"

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        self._todo()


class GCSTextCache(_NotYet):
    impl_note = "google-cloud-storage; object = <prefix>/<sha256 key>.txt"

    def get(self, key: str) -> str | None:
        return self._todo()

    def put(self, key: str, text: str) -> None:
        self._todo()
