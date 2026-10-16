"""In-memory adapters: DuckDB tables, dict blobs, NumPy vector search, log events."""

from __future__ import annotations

import logging
import threading
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

log = logging.getLogger("factory.events")


class DuckDBTableStore:
    """Serving + analytics tables in one in-memory DuckDB database."""

    uri = "memory://tables"

    def __init__(self) -> None:
        self._con = duckdb.connect(":memory:")
        self._lock = threading.Lock()

    def _cur(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def exists(self, entity: str) -> bool:
        with self._lock:
            rows = self._cur().execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [entity]
            ).fetchall()
        return bool(rows)

    def upsert(self, entity: str, rows: pa.Table, key: str) -> None:
        with self._lock:
            cur = self._cur()
            cur.register("incoming", rows)
            try:
                existing = cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [entity]
                ).fetchall()
                if not existing:
                    cur.execute(f'CREATE TABLE "{entity}" AS SELECT * FROM incoming')
                else:
                    cur.execute(f'DELETE FROM "{entity}" WHERE "{key}" IN (SELECT "{key}" FROM incoming)')
                    cur.execute(f'INSERT INTO "{entity}" SELECT * FROM incoming')
            finally:
                cur.unregister("incoming")

    def read(self, entity: str, columns: list[str] | None = None) -> pa.Table:
        cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        with self._lock:
            return self._cur().execute(f'SELECT {cols} FROM "{entity}"').to_arrow_table()

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        with self._lock:
            return self._cur().execute(sql, params or []).to_arrow_table()

    def health(self) -> str:
        self.query("SELECT 1")
        return "ok (in-memory DuckDB)"


class MemoryBlobStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._data[key] = data
        return f"memory://blobs/{key}"

    def get(self, key: str) -> bytes:
        return self._data[key]

    def list(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))

    def health(self) -> str:
        return f"ok (in-memory, {len(self._data)} objects)"


class NumpyVectorIndex:
    """Exact cosine search. Fine for tens of thousands of chunks."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._meta: list[dict[str, Any]] = []
        self._vecs = np.zeros((0, 0), dtype=np.float32)

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict[str, Any]]) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)
        index = {i: n for n, i in enumerate(self._ids)}
        new_ids, new_vecs, new_meta = [], [], []
        for i, v, m in zip(ids, vectors, metadata):
            if i in index:
                self._vecs[index[i]] = v
                self._meta[index[i]] = m
            else:
                new_ids.append(i)
                new_vecs.append(v)
                new_meta.append(m)
        if new_ids:
            stacked = np.vstack(new_vecs)
            self._vecs = stacked if self._vecs.size == 0 else np.vstack([self._vecs, stacked])
            self._ids += new_ids
            self._meta += new_meta

    def search(self, vector: np.ndarray, k: int = 5, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not self._ids:
            return []
        v = np.asarray(vector, dtype=np.float32)
        v = v / (np.linalg.norm(v) or 1)
        scores = self._vecs @ v
        if where:
            mask = np.array([all(m.get(f) == val for f, val in where.items()) for m in self._meta])
            scores = np.where(mask, scores, -np.inf)
        order = np.argsort(-scores, kind="stable")[:k]
        return [
            {"id": self._ids[i], "score": float(scores[i]), **self._meta[i]}
            for i in order
            if np.isfinite(scores[i])
        ]

    def health(self) -> str:
        return f"ok (in-memory NumPy, {len(self._ids)} vectors)"


class LogEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        self.events.append({"topic": topic, **event})
        log.info("%s %s", topic, event)

    def health(self) -> str:
        return "ok (log)"


class MemoryTextCache:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        self._data[key] = text

    def health(self) -> str:
        return f"ok (in-memory, {len(self._data)} entries)"


class TablesAnalyticsStore:
    """Analytics over the in-memory TableStore: nothing is mirrored (the data is already in DuckDB);
    analytics queries run against the same tables."""

    def __init__(self, tables: DuckDBTableStore) -> None:
        self.tables = tables
        self.uri = tables.uri

    def wants(self, entity: str) -> bool:
        return False

    def load(self, entity: str, rows: pa.Table, key: str) -> None:
        return None

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        return self.tables.query(sql, params)

    def health(self) -> str:
        return "ok (analytics on the in-memory tables)"
