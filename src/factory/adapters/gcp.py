"""GCP adapters (gcp profile). Implemented against the Google Cloud client libraries; not yet run against a
live project. Each run's stores are namespaced by tenant:

  AlloyDBTableStore  one Postgres schema per tenant (t_<tenant>); bulk COPY into a staging table + INSERT .. ON CONFLICT
  GCSBlobStore       objects under gs://<bucket>/<tenant>/...
  GCSTextCache       gs://<bucket>/<tenant>/<prefix>/<sha256>.txt
  VertexVectorIndex  Vertex AI Vector Search (stream updates); tenant + audience restricts; chunk metadata in GCS
  PubSubEventBus     JSON messages with a tenant attribute

Engine SQL is written in the DuckDB/PostgreSQL common subset; AlloyDBTableStore translates the two differences
it uses: '?' placeholders and list_contains(list, x).
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pyarrow as pa

from factory.config import AdapterConfig

log = logging.getLogger("factory.gcp")


def _ns(namespace: str | None) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (namespace or "default").lower())


# ================================================================ AlloyDB (PostgreSQL)
def _pg_type(t: pa.DataType) -> str:
    if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_null(t):
        return "TEXT"
    if pa.types.is_int64(t):
        return "BIGINT"
    if pa.types.is_integer(t):
        return "INTEGER"
    if pa.types.is_floating(t):
        return "DOUBLE PRECISION"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP"
    if pa.types.is_decimal(t):
        return f"NUMERIC({t.precision},{t.scale})"
    if pa.types.is_list(t):
        return _pg_type(t.value_type) + "[]"
    raise TypeError(f"no PostgreSQL type for {t}")


def _copy_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _copy_value(v: Any) -> str:
    """One value in PostgreSQL COPY text format."""
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (list, tuple)):
        items = []
        for x in v:
            if x is None:
                items.append("NULL")
            else:
                s = str(x).replace("\\", "\\\\").replace('"', '\\"')
                items.append(f'"{s}"')
        return _copy_escape("{" + ",".join(items) + "}")
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    return _copy_escape(str(v))


def _rewrite_list_contains(sql: str) -> str:
    """list_contains(a, b) -> (b = ANY(a)); handles nested parentheses in either argument."""
    out, i, token = [], 0, "list_contains("
    while True:
        j = sql.find(token, i)
        if j < 0:
            return "".join(out) + sql[i:]
        out.append(sql[i:j])
        k, depth, comma = j + len(token), 1, None
        while depth:
            ch = sql[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 1 and comma is None:
                comma = k
            k += 1
        a, b = sql[j + len(token):comma].strip(), sql[comma + 1:k - 1].strip()
        out.append(f"({_rewrite_list_contains(b)} = ANY({_rewrite_list_contains(a)}))")
        i = k


def to_postgres_sql(sql: str, has_params: bool) -> str:
    """Translate the DuckDB-isms engine SQL uses: '?' placeholders and list_contains(list, x).
    pg8000 reads '%' as a placeholder only when parameters are passed, so it is escaped only then."""
    sql = _rewrite_list_contains(sql)
    return sql.replace("%", "%%").replace("?", "%s") if has_params else sql


class AlloyDBTableStore:
    """Serving tables in AlloyDB via the AlloyDB Python Connector (pg8000 driver, IAM database auth).

    Profile keys: instance (projects/.../instances/...), database, user (IAM user, e.g. the service account
    email without '.gserviceaccount.com'), iam_auth (true), ip_type (PRIVATE | PUBLIC | PSC)."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.cfg = cfg
        self.schema = "t_" + _ns(namespace)
        self.uri = f"alloydb://{cfg.get('instance')}/{cfg.get('database')}/{self.schema}"
        self._conn = None
        self._connector = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._conn is None:
            from google.cloud.alloydb.connector import Connector, IPTypes

            self._connector = Connector()
            self._conn = self._connector.connect(
                self.cfg.get("instance"), "pg8000", user=self.cfg.get("user"), db=self.cfg.get("database", "factory"),
                enable_iam_auth=bool(self.cfg.get("iam_auth", True)), ip_type=IPTypes[self.cfg.get("ip_type", "PRIVATE")],
            )
            cur = self._conn.cursor()
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            cur.execute(f'SET search_path TO "{self.schema}"')
            self._conn.commit()
        return self._conn

    def _execute(self, sql: str, params: list[Any] | None = None, stream=None):
        conn = self._connection()
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(params or ()), stream=stream) if stream is not None else cur.execute(sql, tuple(params or ()))
            rows = cur.fetchall() if cur.description else []
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.commit()
            return cols, rows
        except Exception:
            conn.rollback()
            raise

    def exists(self, entity: str) -> bool:
        with self._lock:
            _, rows = self._execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                                    [self.schema, entity])
        return bool(rows)

    def upsert(self, entity: str, rows: pa.Table, key: str) -> None:
        cols = rows.column_names
        col_ddl = ", ".join(f'"{f.name}" {_pg_type(f.type)}' for f in rows.schema)
        stage = f"_stage_{entity}"
        buf = io.BytesIO()
        for rec in rows.to_pylist():
            buf.write(("\t".join(_copy_value(rec[c]) for c in cols) + "\n").encode("utf-8"))
        buf.seek(0)
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != key)
        with self._lock:
            self._execute(f'CREATE TABLE IF NOT EXISTS "{entity}" ({col_ddl}, PRIMARY KEY ("{key}"))')
            self._execute(f'CREATE TEMP TABLE IF NOT EXISTS "{stage}" (LIKE "{entity}" INCLUDING DEFAULTS)')
            self._execute(f'TRUNCATE "{stage}"')
            if rows.num_rows:
                self._execute(f'COPY "{stage}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) FROM STDIN', stream=buf)
                self._execute(f'INSERT INTO "{entity}" SELECT * FROM "{stage}" ON CONFLICT ("{key}") DO UPDATE SET {updates}')

    def read(self, entity: str, columns: list[str] | None = None) -> pa.Table:
        sel = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        return self.query(f'SELECT {sel} FROM "{entity}"')

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        with self._lock:
            cols, rows = self._execute(to_postgres_sql(sql, bool(params)), params)
        return pa.Table.from_pylist([dict(zip(cols, r)) for r in rows]) if rows else pa.table({c: pa.array([], pa.null()) for c in cols})

    def health(self) -> str:
        self.query("SELECT 1 AS ok")
        return f"ok (AlloyDB {self.cfg.get('instance')}, schema {self.schema})"


# ================================================================ Cloud Storage
class GCSBlobStore:
    """Objects under gs://<bucket>/<tenant>/<key>. Profile keys: bucket."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.cfg = cfg
        self.prefix = _ns(namespace) + "/"
        self._b = None

    @property
    def _bucket(self):
        if self._b is None:  # connect on first use: building the adapter needs no credentials
            from google.cloud import storage

            self._b = storage.Client().bucket(self.cfg.get("bucket"))
        return self._b

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._bucket.blob(self.prefix + key).upload_from_string(data, content_type=content_type)
        return f"gs://{self._bucket.name}/{self.prefix}{key}"

    def get(self, key: str) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return self._bucket.blob(self.prefix + key).download_as_bytes()
        except NotFound:
            raise KeyError(key) from None

    def list(self, prefix: str = "") -> list[str]:
        n = len(self.prefix)
        return sorted(b.name[n:] for b in self._bucket.client.list_blobs(self._bucket, prefix=self.prefix + prefix))

    def health(self) -> str:
        return f"ok (gs://{self._bucket.name}/{self.prefix})" if self._bucket.exists() else f"ERROR bucket {self._bucket.name} not found"


class GCSTextCache:
    """LLM text by sha256 key: gs://<bucket>/<tenant>/<prefix>/<key>.txt. Profile keys: bucket, prefix."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.blobs = GCSBlobStore(cfg, namespace)
        self.prefix = cfg.get("prefix", "cache/text").strip("/") + "/"

    def get(self, key: str) -> str | None:
        try:
            return self.blobs.get(f"{self.prefix}{key}.txt").decode("utf-8")
        except KeyError:
            return None

    def put(self, key: str, text: str) -> None:
        self.blobs.put(f"{self.prefix}{key}.txt", text.encode("utf-8"), "text/plain; charset=utf-8")

    def health(self) -> str:
        return self.blobs.health()


# ================================================================ Vertex AI Vector Search
RESTRICT_FIELDS = ("audience", "asset_type", "use_case", "lob")


class VertexVectorIndex:
    """Vertex AI Vector Search with stream updates. Chunk metadata (title, text, ...) is stored next to the
    vectors in GCS, because the index holds only ids, vectors and restricts.

    Profile keys: project, location, index (resource name), index_endpoint (resource name), deployed_index_id,
    metadata_bucket."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.cfg = cfg
        self.tenant = _ns(namespace)
        self._meta = GCSBlobStore(AdapterConfig(kind="gcs", bucket=cfg.get("metadata_bucket")), namespace)
        self._clients = None

    def _vs(self):
        if self._clients is None:
            from google.cloud import aiplatform

            aiplatform.init(project=self.cfg.get("project"), location=self.cfg.get("location", "us-central1"))
            self._clients = (aiplatform.MatchingEngineIndex(index_name=self.cfg.get("index")),
                             aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name=self.cfg.get("index_endpoint")))
        return self._clients

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict[str, Any]]) -> None:
        from google.cloud.aiplatform_v1.types import IndexDatapoint

        points = []
        for i, v, m in zip(ids, np.asarray(vectors, dtype=np.float32), metadata):
            restricts = [IndexDatapoint.Restriction(namespace="tenant", allow_list=[self.tenant])]
            restricts += [IndexDatapoint.Restriction(namespace=f, allow_list=[str(m[f])]) for f in RESTRICT_FIELDS if m.get(f)]
            points.append(IndexDatapoint(datapoint_id=i, feature_vector=v.tolist(), restricts=restricts))
            self._meta.put(f"vectors/{i}.json", json.dumps(m, default=str).encode(), "application/json")
        for start in range(0, len(points), 500):  # stream-update request size
            self._vs()[0].upsert_datapoints(datapoints=points[start:start + 500])

    def search(self, vector: np.ndarray, k: int = 5, where: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import Namespace

        filters = [Namespace(name="tenant", allow_tokens=[self.tenant])]
        filters += [Namespace(name=f, allow_tokens=[str(v)]) for f, v in (where or {}).items()]
        res = self._vs()[1].find_neighbors(deployed_index_id=self.cfg.get("deployed_index_id"),
                                            queries=[np.asarray(vector, dtype=np.float32).tolist()], num_neighbors=k, filter=filters)
        out = []
        for nb in res[0] if res else []:
            try:
                meta = json.loads(self._meta.get(f"vectors/{nb.id}.json"))
            except KeyError:
                meta = {}
            out.append({"id": nb.id, "score": float(nb.distance), **meta})
        return out

    def health(self) -> str:
        return f"ok (Vector Search {self.cfg.get('index_endpoint')}, deployed {self.cfg.get('deployed_index_id')})"


# ================================================================ Pub/Sub
class PubSubEventBus:
    """JSON events on a topic, with tenant and stage attributes. Profile keys: project, topic."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.cfg = cfg
        self.tenant = namespace or "default"
        self._topic = f"projects/{cfg.get('project')}/topics/{cfg.get('topic')}"
        self._client = None

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        if self._client is None:
            from google.cloud import pubsub_v1

            self._client = pubsub_v1.PublisherClient()
        data = json.dumps({"topic": topic, **event}, default=str).encode()
        future = self._client.publish(self._topic, data, tenant=self.tenant, stage=str(event.get("stage", "")))
        future.result(timeout=30)

    def health(self) -> str:
        return f"ok (Pub/Sub {self._topic}; publishes on first event)"
