"""Shared state for one data-engine run: blueprint, stores, scenario mix, write helper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from factory.adapters.ports import Stores
from factory.contracts import Blueprint
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
    bp: Blueprint
    stores: Stores
    shards: int = 4
    checksums: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        r = self.bp.repeatability
        self.seed = r.root_seed
        self.as_of = np.datetime64(r.as_of_date.isoformat(), "D")
        self.dataset_id = self.bp.data_spec.dataset_id
        self.tenant_id = self.bp.tenant.id
        self.in_scope = self._entities_in_scope()

    # ------------------------------------------------------------ scope
    def _entities_in_scope(self) -> set[str]:
        out: set[str] = set()
        for d in self.bp.scope.domains:
            ents = catalog.ENTITIES[d.code] if d.role == "primary" else (d.entities or [])
            out |= set(ents)
        return out

    def wants(self, entity: str) -> bool:
        return entity in self.in_scope

    def unimplemented(self) -> list[str]:
        return sorted(self.in_scope - catalog.IMPLEMENTED)

    # ------------------------------------------------------------ randomness and ids
    def rr(self, entity: str, keys: np.ndarray) -> RowRandom:
        return RowRandom(self.seed, entity, keys)

    def ids(self, entity: str, keys: np.ndarray) -> list[str]:
        return row_ids(self.dataset_id, entity, self.seed, keys)

    def scenarios(self, anchor: str, rr: RowRandom) -> np.ndarray:
        """Assign scenario ids to anchor rows using the blueprint's scenario mix."""
        mix = [
            (s.scenario_id, s.share_pct)
            for s in self.bp.data_spec.scenario_mix
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
        typed = not any(pa.types.is_null(f.type) for f in table.schema)
        if n or (typed and not self.stores.tables.exists(entity)):
            # an empty shard only creates the table when every column type is known
            self.stores.tables.upsert(entity, table, catalog.PRIMARY_KEY[entity])
        self.counts[entity] = self.counts.get(entity, 0) + n
        self.checksums[entity] = (self.checksums.get(entity, 0) + checksum(table)) % (1 << 64)
        return table

    def shard_ranges(self, n: int) -> list[tuple[int, int]]:
        per = -(-n // self.shards)
        return [(s * per, min(n, (s + 1) * per)) for s in range(self.shards) if s * per < n]
