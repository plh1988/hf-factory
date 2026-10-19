"""Shared state for one data-engine run: run spec (from job payloads), stores, scenario mix, write helper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from factory.adapters.ports import Stores
from factory.contracts import RunSpec
from factory.data_engine import catalog
from factory.data_engine.rng import RowRandom, checksum, row_ids


def dates(values: np.ndarray) -> pa.Array:
    """numpy datetime64[D] (NaT = null) -> Arrow date32."""
    return pa.array(np.asarray(values, dtype="datetime64[D]"), from_pandas=True)


def stamps(values: np.ndarray) -> pa.Array:
    """numpy datetime64[s] (NaT = null) -> Arrow timestamp."""
    return pa.array(np.asarray(values, dtype="datetime64[s]"), from_pandas=True)


@dataclass
class GenContext:
    """Everything a generator needs for one run: the run spec, the stores, the shard count, and running
    per-entity row counts and checksums (summed across shards, so the order of shards never matters).
    """
    spec: RunSpec
    stores: Stores
    shards: int = 4
    checksums: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seed = self.spec.root_seed
        self.as_of = np.datetime64(self.spec.as_of_date.isoformat(), "D")
        self.dataset_id = self.spec.dataset_id
        self.tenant_id = self.spec.tenant_id

    # ------------------------------------------------------------ scope: entities named in the payloads so far
    @property
    def in_scope(self) -> set[str]:
        return self.spec.entities

    def wants(self, entity: str) -> bool:
        """True if some payload in this run listed the entity (generators skip entities nobody asked for)."""
        return entity in self.in_scope

    def unimplemented(self) -> list[str]:
        return sorted(self.in_scope - catalog.IMPLEMENTED)

    # ------------------------------------------------------------ randomness and ids
    def rr(self, entity: str, keys: np.ndarray) -> RowRandom:
        """Random streams for these row keys of an entity (deterministic per seed, entity and key)."""
        return RowRandom(self.seed, entity, keys)

    def ids(self, entity: str, keys: np.ndarray) -> list[str]:
        """Primary keys: UUIDv5(dataset_id, entity, seed, row key)."""
        return row_ids(self.dataset_id, entity, self.seed, keys)

    def scenarios(self, anchor: str, rr: RowRandom) -> np.ndarray:
        """Assign scenario ids to anchor rows using the run's scenario mix (from the job payloads)."""
        mix = [
            (s.scenario_id, s.share_pct)
            for s in self.spec.scenario_mix
            if s.scenario_id in catalog.SCENARIOS and catalog.SCENARIOS[s.scenario_id]["anchor"] == anchor
        ]
        if not mix:
            return np.full(rr.n, "BASE", dtype=object)
        names, shares = zip(*mix)
        return rr.choice(f"scenario:{anchor}", names, shares)

    # ------------------------------------------------------------ writing
    def write(self, entity: str, cols: dict[str, Any], scenario: np.ndarray | str = "BASE") -> pa.Table:
        """Stamp P-01 columns, upsert into the TableStore, accumulate count and checksum."""
        n = len(next(iter(cols.values()))) if cols else 0
        scen = np.full(n, scenario, dtype=object) if isinstance(scenario, str) else scenario
        table = pa.table(
            {
                **cols,
                "is_synthetic": pa.array(np.ones(n, dtype=bool)),
                "tenant_id": pa.array([self.tenant_id] * n, pa.string()),
                "scenario_id": pa.array(list(scen), pa.string()),
            }
        )
        if n == 0:  # columns built from empty lists have no type: ids and text default to string
            table = pa.table({f.name: (pa.array([], pa.string()) if pa.types.is_null(f.type) else table[f.name])
                              for f in table.schema})
        if n or not self.stores.tables.exists(entity):
            # every generated entity exists afterwards, even with zero rows
            self.stores.tables.upsert(entity, table, catalog.PRIMARY_KEY[entity])
        analytics = self.stores.analytics
        if n and analytics is not None and analytics.wants(entity):  # e.g. claims mirrored to BigQuery
            analytics.load(entity, table, catalog.PRIMARY_KEY[entity])
        self.counts[entity] = self.counts.get(entity, 0) + n
        self.checksums[entity] = (self.checksums.get(entity, 0) + checksum(table)) % (1 << 64)
        return table

    def shard_ranges(self, n: int) -> list[tuple[int, int]]:
        """Split n anchor rows into `shards` contiguous index ranges (empty ranges are dropped)."""
        per = -(-n // self.shards)
        return [(s * per, min(n, (s + 1) * per)) for s in range(self.shards) if s * per < n]
