"""Runs a pack: evaluates its building blocks over anchor rows and writes the entity through GenContext.

Determinism: every random value comes from RowRandom(seed, stream, row key) and a named draw, so a row's values
do not depend on the shard, on other rows, or on the order expressions are evaluated. Rows are selected first
(rows.when over all anchor rows), then columns are evaluated on the selected rows only; because draws are keyed
per row, this gives the same values as evaluating everything and filtering.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from factory.data_engine import scenarios as recipes
from factory.data_engine.context import GenContext, dates
from factory.data_engine.packs.model import (
    ANCHOR_STREAM, AllOf, AnchorField, AnyOf, Bernoulli, Choice, Const, DaysBeforeAsOf, IfThen, Integer, MapValues, Not,
    Pack, RecipeFlag, RowId,
)
from factory.data_engine.rng import RowRandom


@dataclass(eq=False)
class AnchorFrame:
    """Anchor rows a pack entity hangs off, as a generator sees them (one shard)."""

    anchor: str  # member
    keys: np.ndarray  # anchor row keys (member index)
    scen: np.ndarray  # scenario tag per anchor row
    fields: dict[str, np.ndarray]  # anchor columns exposed to packs (model.ANCHOR_FIELDS)

    def take(self, sel: np.ndarray) -> "AnchorFrame":
        return AnchorFrame(self.anchor, self.keys[sel], self.scen[sel], {k: v[sel] for k, v in self.fields.items()})


class Evaluator:
    """Evaluates building blocks to one numpy array per expression, one value per row of the frame."""

    def __init__(self, ctx: GenContext, entity: str, rr: RowRandom, frame: AnchorFrame) -> None:
        self.ctx, self.entity, self.rr, self.frame = ctx, entity, rr, frame
        self.n = len(frame.keys)

    def bool(self, expr) -> np.ndarray:
        return np.asarray(self.eval(expr), dtype=bool)

    def eval(self, e) -> np.ndarray:  # noqa: C901 - one branch per building block
        rr, n = self.rr, self.n
        if isinstance(e, Const):
            return np.full(n, e.value, dtype=object)
        if isinstance(e, AnchorField):
            return self.frame.fields[e.field]
        if isinstance(e, RecipeFlag):
            return np.isin(self.frame.scen, [s.id for s in recipes.having(e.flag)])
        if isinstance(e, Choice):
            return rr.choice(e.draw, e.options, e.weights)
        if isinstance(e, Bernoulli):
            p = e.p if isinstance(e.p, (int, float)) else np.asarray(self.eval(e.p), dtype=np.float64)
            return rr.bernoulli(e.draw, p)
        if isinstance(e, Integer):
            return rr.integers(e.draw, e.low, e.high)
        if isinstance(e, DaysBeforeAsOf):
            return self.ctx.as_of - rr.integers(e.draw, e.min_days, e.max_days).astype("timedelta64[D]")
        if isinstance(e, MapValues):
            return np.array([e.values.get(str(x), e.default) for x in self.eval(e.of)], dtype=object)
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
    raise TypeError(f"unknown column type {type_}")


def generate(ctx: GenContext, pack: Pack, frame: AnchorFrame) -> pa.Table:
    """Generate and write the pack's entity for the anchor rows of one shard; rows inherit the anchor's scenario tag."""
    spec = pack.entity
    stream = ANCHOR_STREAM[spec.anchor] if spec.random_stream == "anchor" else spec.name
    if spec.rows.when is not None:
        sel = np.where(Evaluator(ctx, spec.name, ctx.rr(stream, frame.keys), frame).bool(spec.rows.when))[0]
        frame = frame.take(sel)
    ev = Evaluator(ctx, spec.name, ctx.rr(stream, frame.keys), frame)
    cols = {c.name: _arrow(ev.eval(c.gen), c.type) for c in spec.columns}
    return ctx.write(spec.name, cols, scenario=frame.scen)
