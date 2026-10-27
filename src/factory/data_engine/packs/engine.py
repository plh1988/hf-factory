"""Runs an entity pack: evaluates its building blocks over anchor rows and writes the entity through GenContext.

Determinism: every random value comes from RowRandom(seed, stream, row key) and a named draw, so a row's values
do not depend on the shard, on other rows, or on the order expressions are evaluated.

  zero_or_one  rows.when picks anchor rows; the row key is the anchor key (stream: the entity, or the anchor's for
               core packs). Columns are evaluated on the picked rows only: draws are keyed per row, so this gives
               the same values as evaluating everything and filtering.
  many         rows.when and rows.count are evaluated per anchor row on the stream "<entity>:anchor"; anchor row a
               gets count rows with keys a * slots + j (keys.member_key), so growing a dataset never changes keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from factory.data_engine import keys as K
from factory.data_engine import scenarios as recipes
from factory.data_engine.context import GenContext, dates
from factory.data_engine.packs.model import (
    ANCHOR_STREAM, AddDays, AllOf, AnchorField, AnyOf, Bernoulli, Choice, ColumnRef, Const, DaysBeforeAsOf, EntityPack,
    Digits, Equals, Format, IfThen, Integer, Lookup, MapValues, Money, MonthsBeforeAsOf, Not, OneOf, RecipeFlag, RowId,
    RowIndex,
)
from factory.data_engine.rng import RowRandom, money


@dataclass(eq=False)
class AnchorFrame:
    """Anchor rows a pack entity hangs off, as a generator sees them (one shard)."""

    anchor: str  # member
    keys: np.ndarray  # anchor row keys (member index)
    scen: np.ndarray  # scenario tag per anchor row
    fields: dict[str, np.ndarray]  # anchor columns exposed to packs (model.ANCHOR_FIELDS)
    index: np.ndarray | None = field(default=None)  # row index within the anchor (many rows)
    anchor_keys: np.ndarray | None = field(default=None)  # anchor key per row (defaults to keys)

    def __post_init__(self) -> None:
        if self.anchor_keys is None:
            self.anchor_keys = self.keys

    def take(self, sel: np.ndarray) -> "AnchorFrame":
        idx = None if self.index is None else self.index[sel]
        return AnchorFrame(self.anchor, self.keys[sel], self.scen[sel], {k: v[sel] for k, v in self.fields.items()}, idx,
                           self.anchor_keys[sel])


class Evaluator:
    """Evaluates building blocks to one numpy array per expression, one value per row of the frame."""

    def __init__(self, ctx: GenContext, entity: str, rr: RowRandom, frame: AnchorFrame) -> None:
        self.ctx, self.entity, self.rr, self.frame = ctx, entity, rr, frame
        self.n = len(frame.keys)
        self.index = frame.index if frame.index is not None else np.zeros(self.n, dtype=np.int64)
        self.cols: dict[str, np.ndarray] = {}  # raw values of the columns evaluated so far
        self._anchor_rr: RowRandom | None = None

    def _rr(self, e) -> RowRandom:
        """Row stream, or for per: anchor the "<entity>:anchor" stream keyed by anchor key (shared by the anchor's rows)."""
        if getattr(e, "per", "row") == "row":
            return self.rr
        if self._anchor_rr is None:
            self._anchor_rr = self.ctx.rr(f"{self.entity}:anchor", self.frame.anchor_keys)
        return self._anchor_rr

    def _candidates(self, e: Lookup) -> list:
        """The lookup column of the matching rows, ordered by the table's primary key (read once per evaluator).
        Same draw + table + where => same row for every lookup, so e.g. a pharmacy's npi and org_name stay together.
        Identifiers are validated by the pack model (snake_case names); values are bound."""
        from factory.data_engine import catalog

        key = (e.table, e.column, tuple(sorted((k, tuple(v)) for k, v in e.where.items())))
        cache = self.__dict__.setdefault("_lookup_cache", {})
        if key not in cache:
            conds, params = ["TRUE"], []
            for col, values in sorted(e.where.items()):
                conds.append(f'CAST("{col}" AS VARCHAR) IN ({", ".join("?" * len(values))})')
                params += [str(v) for v in values]
            pk = catalog.PRIMARY_KEY[e.table]
            sql = f'SELECT "{e.column}" AS v FROM "{e.table}" WHERE {" AND ".join(conds)} ORDER BY "{pk}"'
            cache[key] = [r["v"] for r in self.ctx.stores.tables.query(sql, params).to_pylist()]
        return cache[key]

    def bool(self, expr) -> np.ndarray:
        return np.asarray(self.eval(expr), dtype=bool)

    def _num(self, v, dtype):
        return v if isinstance(v, (int, float)) else np.asarray(self.eval(v), dtype=dtype)

    def eval(self, e) -> np.ndarray:  # noqa: C901 - one branch per building block
        n = self.n
        if isinstance(e, Const):
            return np.full(n, e.value, dtype=object)
        if isinstance(e, AnchorField):
            return self.frame.fields[e.field]
        if isinstance(e, ColumnRef):
            return self.cols[e.name]
        if isinstance(e, RowIndex):
            return self.index
        if isinstance(e, RecipeFlag):
            return np.isin(self.frame.scen, [s.id for s in recipes.having(e.flag)])
        if isinstance(e, Choice):
            return self._rr(e).choice(e.draw, e.options, e.weights)
        if isinstance(e, Bernoulli):
            return self._rr(e).bernoulli(e.draw, self._num(e.p, np.float64))
        if isinstance(e, Integer):
            return self._rr(e).integers(e.draw, e.low, e.high)
        if isinstance(e, Money):
            lo = np.round(np.asarray(self._num(e.low, np.float64)) * 100).astype(np.int64)
            hi = np.round(np.asarray(self._num(e.high, np.float64)) * 100).astype(np.int64)
            return self._rr(e).integers(e.draw, lo, np.maximum(hi, lo) + 1)
        if isinstance(e, DaysBeforeAsOf):
            return self.ctx.as_of - self._rr(e).integers(e.draw, e.min_days, e.max_days).astype("timedelta64[D]")
        if isinstance(e, MonthsBeforeAsOf):
            months = np.broadcast_to(np.asarray(self._num(e.months, np.int64)), (n,)).astype(np.int64)
            return (self.ctx.as_of.astype("datetime64[M]") - months.astype("timedelta64[M]")).astype("datetime64[D]")
        if isinstance(e, AddDays):
            base = np.asarray(self.eval(e.of), dtype="datetime64[D]")
            days = np.broadcast_to(np.asarray(self._num(e.days, np.int64)), (n,)).astype(np.int64)
            return base + days.astype("timedelta64[D]")
        if isinstance(e, Format):
            names = list(self.frame.fields) + list(self.cols)
            values = {k: self.frame.fields.get(k, self.cols.get(k)) for k in names}
            return np.array([e.template.format(index=int(self.index[i]), **{k: _plain(v[i]) for k, v in values.items()})
                             for i in range(n)], dtype=object)
        if isinstance(e, Lookup):
            options = self._candidates(e)
            if not options:
                return np.full(n, None, dtype=object)
            u = self._rr(e).u(e.draw)
            return np.asarray(options, dtype=object)[np.minimum((u * len(options)).astype(np.int64), len(options) - 1)]
        if isinstance(e, Digits):
            numbers = (self._rr(e).u(e.draw) * 10 ** e.length).astype(np.int64)
            return np.array([f"{x:0{e.length}d}"[-e.length:] for x in numbers], dtype=object)
        if isinstance(e, MapValues):
            return np.array([e.values.get(str(x), e.default) for x in self.eval(e.of)], dtype=object)
        if isinstance(e, Equals):
            return np.array([x == e.value for x in self.eval(e.of)], dtype=bool)
        if isinstance(e, OneOf):
            allowed = list(e.values)
            return np.array([x in allowed for x in self.eval(e.of)], dtype=bool)
        if isinstance(e, AnyOf):
            return np.logical_or.reduce([self.bool(x) for x in e.of])
        if isinstance(e, AllOf):
            return np.logical_and.reduce([self.bool(x) for x in e.of])
        if isinstance(e, Not):
            return ~self.bool(e.of)
        if isinstance(e, IfThen):
            return np.where(self.bool(e.cond), self.eval(e.then), self.eval(e.else_))
        if isinstance(e, RowId):
            return np.asarray(self.ctx.ids(self.entity, self.frame.keys), dtype=object)
        raise TypeError(f"unknown building block {type(e).__name__}")


def _plain(v):
    """Template value: numpy scalars and enums as plain Python text/numbers."""
    if isinstance(v, np.datetime64):
        return str(v.astype("datetime64[D]"))
    if isinstance(v, np.generic):
        return v.item()
    return str(v) if isinstance(v, str) else v


def _arrow(values: np.ndarray, type_: str) -> pa.Array:
    if type_ == "string":
        return pa.array([None if v is None else str(v) for v in values], pa.string())
    if type_ == "bool":
        return pa.array(np.asarray(values, dtype=bool))
    if type_ == "int":
        return pa.array(np.asarray(values, dtype=np.int64))
    if type_ == "float":
        return pa.array(np.asarray(values, dtype=np.float64))
    if type_ == "date":
        return dates(np.asarray(values, dtype="datetime64[D]"))
    if type_ == "money":
        return money(np.asarray(values, dtype=np.int64))
    raise TypeError(f"unknown column type {type_}")


def rows_frame(ctx: GenContext, pack: EntityPack, frame: AnchorFrame) -> AnchorFrame:
    """The entity's rows for these anchor rows: anchor fields and scenario tags repeated per row, row keys, row index."""
    spec = pack.entity
    if spec.rows.per_anchor == "zero_or_one":
        if spec.rows.when is None:
            return frame
        stream = ANCHOR_STREAM[spec.anchor] if spec.random_stream == "anchor" else spec.name
        return frame.take(np.where(Evaluator(ctx, spec.name, ctx.rr(stream, frame.keys), frame).bool(spec.rows.when))[0])
    level = Evaluator(ctx, spec.name, ctx.rr(f"{spec.name}:anchor", frame.keys), frame)
    picked = level.bool(spec.rows.when) if spec.rows.when is not None else np.ones(len(frame.keys), dtype=bool)
    count = np.clip(np.asarray(level.eval(spec.rows.count), dtype=np.int64), 0, spec.rows.slots)
    count = np.where(picked, count, 0)
    ai = np.repeat(np.arange(len(frame.keys)), count)
    j = np.concatenate([np.arange(c) for c in count]).astype(np.int64) if len(ai) else np.array([], np.int64)
    rows = frame.take(ai)
    return AnchorFrame(rows.anchor, K.member_key(rows.keys, j, spec.rows.slots), rows.scen, rows.fields, j, rows.keys)


def generate(ctx: GenContext, pack: EntityPack, frame: AnchorFrame) -> pa.Table:
    """Generate and write the pack's entity for the anchor rows of one shard; rows inherit the anchor's scenario tag."""
    spec = pack.entity
    rows = rows_frame(ctx, pack, frame)
    stream = ANCHOR_STREAM[spec.anchor] if spec.random_stream == "anchor" else spec.name
    ev = Evaluator(ctx, spec.name, ctx.rr(stream, rows.keys), rows)
    cols = {}
    for c in spec.columns:
        ev.cols[c.name] = ev.eval(c.gen)
        cols[c.name] = _arrow(ev.cols[c.name], c.type)
    return ctx.write(spec.name, cols, scenario=rows.scen)
