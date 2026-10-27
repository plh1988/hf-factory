"""Capability pack format (phase 0: one entity with zero or one row per anchor row).

A pack declares an entity: its columns, how each column's values are generated from building blocks (expressions,
see Expr), and which anchor rows get a row. Generic code (engine.py) runs it, so a new entity needs no Python.
These models are the single source of truth for the pack format; the skill's pack-schema.json is generated from
them (scripts/build_skill_refs.py).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-z][a-z0-9_]*$"
DRAW = r"^[a-z][a-z0-9_:]*$"


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ building blocks
class Const(_Op):
    """A fixed value."""

    op: Literal["const"]
    value: Any


class AnchorField(_Op):
    """A column of the anchor row (see ANCHOR_FIELDS)."""

    op: Literal["anchor"]
    field: str


class RecipeFlag(_Op):
    """True where the anchor row's scenario recipe sets `flag` (a Scenario field in scenarios.py)."""

    op: Literal["recipe"]
    flag: str


class Choice(_Op):
    """One of `options`, weighted by `weights` (uniform when omitted). Options are listed explicitly, in order."""

    op: Literal["choice"]
    draw: str = Field(pattern=DRAW, description="draw name: the random value is fixed by (seed, stream, row key, draw)")
    options: list[Any] = Field(min_length=1)
    weights: list[float] | None = None

    @model_validator(mode="after")
    def _weights_match(self) -> "Choice":
        if self.weights is not None and len(self.weights) != len(self.options):
            raise ValueError(f"choice {self.draw!r}: {len(self.weights)} weights for {len(self.options)} options")
        return self


class Bernoulli(_Op):
    """True with probability p (a number, or an expression giving one per row)."""

    op: Literal["bernoulli"]
    draw: str = Field(pattern=DRAW)
    p: Union[float, "Expr"]


class Integer(_Op):
    """Uniform integer in [low, high)."""

    op: Literal["integer"]
    draw: str = Field(pattern=DRAW)
    low: int
    high: int

    @model_validator(mode="after")
    def _range(self) -> "Integer":
        if self.high <= self.low:
            raise ValueError(f"integer {self.draw!r}: high must be greater than low")
        return self


class DaysBeforeAsOf(_Op):
    """A date: as_of minus a uniform number of days in [min_days, max_days). Generators never read the clock."""

    op: Literal["days_before_as_of"]
    draw: str = Field(pattern=DRAW)
    min_days: int = Field(ge=0)
    max_days: int

    @model_validator(mode="after")
    def _range(self) -> "DaysBeforeAsOf":
        if self.max_days <= self.min_days:
            raise ValueError(f"days_before_as_of {self.draw!r}: max_days must be greater than min_days")
        return self


class MapValues(_Op):
    """Look up each row's value of `of` in `values`; rows without a match get `default`."""

    op: Literal["map"]
    of: "Expr"
    values: dict[str, Any]
    default: Any = None


class AnyOf(_Op):
    """Boolean OR of the expressions."""

    op: Literal["any"]
    of: list["Expr"] = Field(min_length=1)


class AllOf(_Op):
    """Boolean AND of the expressions."""

    op: Literal["all"]
    of: list["Expr"] = Field(min_length=1)


class Not(_Op):
    """Boolean NOT of the expression."""

    op: Literal["not"]
    of: "Expr"


class IfThen(_Op):
    """`then` where `cond` is true, otherwise `else`."""

    op: Literal["if"]
    cond: "Expr"
    then: "Expr"
    else_: "Expr" = Field(alias="else")


class RowId(_Op):
    """The row's primary key: UUIDv5(dataset_id, entity, seed, row key)."""

    op: Literal["row_id"]


Expr = Annotated[
    Union[Const, AnchorField, RecipeFlag, Choice, Bernoulli, Integer, DaysBeforeAsOf, MapValues, AnyOf, AllOf, Not, IfThen, RowId],
    Field(discriminator="op"),
]
for _m in (Bernoulli, MapValues, AnyOf, AllOf, Not, IfThen):
    _m.model_rebuild()


# ------------------------------------------------------------------ entity and pack
def _recipe_fields() -> set[str]:
    from factory.data_engine.scenarios import Scenario

    return {f.name for f in dataclasses.fields(Scenario)} - {"id", "anchor", "kind", "title"}


_RECIPE_FIELDS = _recipe_fields()
ANCHOR_FIELDS = {"member": ["member_id", "member_number", "lob", "plan_id", "sex"]}
ANCHOR_STAGE = {"member": "s4"}
ANCHOR_STREAM = {"member": "member_demographics"}  # entity whose row streams the anchor uses


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME)
    type: Literal["string", "bool", "int", "float", "date"]
    gen: Expr
    description: str = ""


class Rows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_anchor: Literal["zero_or_one"] = "zero_or_one"  # phase 0; "many" (with key slots) comes later
    when: Expr | None = Field(None, description="anchor rows that get a row (all when omitted)")


class EntitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME)
    anchor: Literal["member"]
    primary_key: str
    rows: Rows = Rows()
    random_stream: Literal["own", "anchor"] = Field(
        "own", description="own: streams of this entity (independent of every other entity). anchor: the anchor's "
                           "streams, sharing draw names with core generators; allowed for core packs only")
    columns: list[Column] = Field(min_length=1)

    @property
    def stage(self) -> str:
        return ANCHOR_STAGE[self.anchor]

    @model_validator(mode="after")
    def _check(self) -> "EntitySpec":
        names = [c.name for c in self.columns]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate columns {dupes}")
        pk = next((c for c in self.columns if c.name == self.primary_key), None)
        if pk is None or not isinstance(pk.gen, RowId) or pk.type != "string":
            raise ValueError(f"primary key {self.primary_key!r} must be a string column with gen op row_id")
        reserved = {"is_synthetic", "tenant_id", "scenario_id"} & set(names)
        if reserved:
            raise ValueError(f"columns {sorted(reserved)} are stamped by the engine (P-01)")
        allowed = set(ANCHOR_FIELDS[self.anchor])
        for expr in _walk([c.gen for c in self.columns] + ([self.rows.when] if self.rows.when else [])):
            if isinstance(expr, AnchorField) and expr.field not in allowed:
                raise ValueError(f"anchor field {expr.field!r} unknown for anchor {self.anchor}; known: {sorted(allowed)}")
            if isinstance(expr, RecipeFlag) and expr.flag not in _RECIPE_FIELDS:
                raise ValueError(f"recipe flag {expr.flag!r} is not a Scenario field")
        return self


class Pack(BaseModel):
    """A capability pack. Phase 0 packs carry one entity."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^entity\.[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    kind: Literal["entity"] = "entity"
    core: bool = Field(False, description="part of the built-in catalog (may use the anchor's random streams)")
    description: str = ""
    entity: EntitySpec

    @model_validator(mode="after")
    def _check(self) -> "Pack":
        if self.id != f"entity.{self.entity.name}":
            raise ValueError(f"pack id {self.id!r} must be 'entity.{self.entity.name}'")
        if self.entity.random_stream == "anchor" and not self.core:
            raise ValueError("random_stream 'anchor' shares draw names with core generators; only core packs may use it")
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def digest(self) -> str:
        """sha256 of the canonical JSON: recorded per run so a replay uses exactly this pack."""
        canon = json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def _walk(exprs):
    """Every expression node, depth first."""
    stack = [e for e in exprs if e is not None]
    while stack:
        e = stack.pop()
        yield e
        for name in type(e).model_fields:
            v = getattr(e, name)
            if isinstance(v, _Op):
                stack.append(v)
            elif isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, _Op))


def draws(spec: EntitySpec) -> list[str]:
    """Draw names used by the entity, sorted (for review: they must not collide on a shared stream)."""
    exprs = [c.gen for c in spec.columns] + ([spec.rows.when] if spec.rows.when else [])
    return sorted({e.draw for e in _walk(exprs) if hasattr(e, "draw")})
