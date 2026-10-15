"""Deterministic, vectorized per-row randomness and IDs.

Every random value is a pure function of (seed, entity, row key, draw name). Nothing depends
on shard layout, worker count, execution order or the wall clock, so any shard can generate
any slice and a replay produces identical rows.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Sequence

import duckdb  # used as a pure compute library for checksums (no storage)
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

_GOLDEN = np.uint64(0x9E3779B97F4A7C15)
_C1 = np.uint64(0xBF58476D1CE4E5B9)
_C2 = np.uint64(0x94D049BB133111EB)


def stable_hash64(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")


def _mix(x: np.ndarray) -> np.ndarray:
    """splitmix64 finalizer over uint64 arrays."""
    with np.errstate(over="ignore"):
        x = x ^ (x >> np.uint64(30))
        x = x * _C1
        x = x ^ (x >> np.uint64(27))
        x = x * _C2
        x = x ^ (x >> np.uint64(31))
    return x


class RowRandom:
    """Random draws for a set of rows. Each draw name gives an independent stream."""

    def __init__(self, seed: int, entity: str, keys: np.ndarray) -> None:
        keys = np.asarray(keys, dtype=np.uint64)
        with np.errstate(over="ignore"):
            self._base = _mix(np.uint64(stable_hash64(f"{seed}|{entity}")) ^ _mix(keys * _GOLDEN + np.uint64(1)))
        self.n = len(keys)

    def u(self, draw: str) -> np.ndarray:
        x = _mix(self._base ^ np.uint64(stable_hash64(draw)))
        return (x >> np.uint64(11)).astype(np.float64) * (1.0 / (1 << 53))

    def integers(self, draw: str, lo: int, hi: int | np.ndarray) -> np.ndarray:
        """Uniform integers in [lo, hi)."""
        return (lo + np.floor(self.u(draw) * (np.asarray(hi) - lo))).astype(np.int64)

    def bernoulli(self, draw: str, p: float | np.ndarray) -> np.ndarray:
        return self.u(draw) < p

    def choice(self, draw: str, options: Sequence, p: Sequence[float] | None = None) -> np.ndarray:
        opts = np.asarray(options, dtype=object)
        if p is None:
            idx = self.integers(draw, 0, len(opts))
        else:
            cum = np.cumsum(np.asarray(p, dtype=np.float64))
            idx = np.minimum(np.searchsorted(cum / cum[-1], self.u(draw), side="right"), len(opts) - 1)
        return opts[idx]

    def normal(self, draw: str, mu: float = 0.0, sd: float = 1.0) -> np.ndarray:
        u1 = np.maximum(self.u(draw + ":a"), 1e-12)
        u2 = self.u(draw + ":b")
        return mu + sd * np.sqrt(-2.0 * np.log(u1)) * np.cos(2 * np.pi * u2)

    def lognormal(self, draw: str, median: float, sigma: float) -> np.ndarray:
        return median * np.exp(self.normal(draw, 0.0, sigma))

    def poisson(self, draw: str, lam: float | np.ndarray, kmax: int = 40) -> np.ndarray:
        """Inverse-CDF Poisson, vectorized; capped at kmax."""
        lam = np.broadcast_to(np.asarray(lam, dtype=np.float64), (self.n,))
        u = self.u(draw)
        k = np.zeros(self.n, dtype=np.int64)
        p = np.exp(-lam)
        cdf = p.copy()
        for i in range(1, kmax + 1):
            more = u > cdf
            if not more.any():
                break
            k += more
            p = p * lam / i
            cdf = cdf + p
        return k


def row_ids(dataset_id: str, entity: str, seed: int, keys: Sequence[int] | np.ndarray) -> list[str]:
    """UUIDv5(dataset_id, entity, seed, row key). Never contains project, region or host names."""
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"hf-factory/{dataset_id}")
    return [str(uuid.uuid5(ns, f"{entity}:{seed}:{int(k)}")) for k in keys]


def money(cents: np.ndarray) -> pa.Array:
    """Integer cents -> exact DECIMAL(14,2)."""
    a = pa.array(np.asarray(cents, dtype=np.int64)).cast(pa.decimal128(19, 0))
    return pc.multiply(a, pa.scalar(Decimal("0.01"), pa.decimal128(3, 2))).cast(pa.decimal128(14, 2))


def checksum(table: pa.Table) -> int:
    """Order-independent checksum: sum of DuckDB row hashes mod 2^64 (DuckDB version is pinned).

    Sums compose, so per-shard checksums add up to the checksum of the whole entity."""
    if table.num_rows == 0:
        return 0
    con = duckdb.connect()
    con.register("t", table)
    (value,) = con.execute("SELECT sum(hash(t)::HUGEINT) % 18446744073709551616 FROM t").fetchone()
    con.close()
    return int(value)


def fmt_checksum(value: int) -> str:
    return f"{value % (1 << 64):016x}"
