"""Capability pack format.

Two kinds of pack, both data, both run by generic code:

  entity     a table anchored on members (stage s4): columns, building blocks per column, which members get rows
             (zero or one, or several with key slots), foreign keys and rules. Run by engine.py.
  use_case   an API use case: read endpoints over pack or core tables (member-scoped, with caregiver scope),
             FAQ and agent-SOP content for s6, and the tables it needs. Run by api_engine/generic.py.

These models are the single source of truth for the format; the skill's pack-schema.json is generated from them
(scripts/build_skill_refs.py). Cross-pack checks (does an endpoint's table have that column?) are done by the
capability registry, which sees all packs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import string
import zlib
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

NAME = r"^[a-z][a-z0-9_]*$"
DRAW = r"^[a-z][a-z0-9_:]*$"
RULE_ID = r"^[RTFCPA]-[A-Z][A-Z0-9]*(-[A-Z0-9]+)*$"


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ building blocks
class Const(_Op):
    """A fixed value (null allowed)."""

    op: Literal["const"]
    value: Any


class AnchorField(_Op):
    """A column of the anchor row (see the anchor table)."""

    op: Literal["anchor"]
    field: str


class ColumnRef(_Op):
    """The value of an earlier column of the same row."""

    op: Literal["column"]
    name: str


class RowIndex(_Op):
    """The row's position within its anchor: 0, 1, 2, ... (always 0 for zero_or_one entities)."""

    op: Literal["row_index"]


class RecipeFlag(_Op):
    """True where the anchor row's scenario recipe sets `flag` (a Scenario field in scenarios.py)."""

    op: Literal["recipe"]
    flag: str


class Choice(_Op):
    """One of `options`, weighted by `weights` (uniform when omitted). Options are listed explicitly, in order."""

    op: Literal["choice"]
    draw: str = Field(pattern=DRAW, description="draw name: the random value is fixed by (seed, stream, row key, draw)")
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row, shared by all its rows (e.g. a member's premium)")
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
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row, shared by all its rows (e.g. a member's premium)")
    p: Union[float, "Expr"]


class Integer(_Op):
    """Uniform integer in [low, high)."""

    op: Literal["integer"]
    draw: str = Field(pattern=DRAW)
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row, shared by all its rows (e.g. a member's premium)")
    low: int
    high: int

    @model_validator(mode="after")
    def _range(self) -> "Integer":
        if self.high <= self.low:
            raise ValueError(f"integer {self.draw!r}: high must be greater than low")
        return self


class Money(_Op):
    """Uniform amount in whole cents between `low` and `high` dollars (numbers or per-row expressions). Use with
    column type money."""

    op: Literal["money"]
    draw: str = Field(pattern=DRAW)
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row, shared by all its rows (e.g. a member's premium)")
    low: Union[float, "Expr"]
    high: Union[float, "Expr"]


class DaysBeforeAsOf(_Op):
    """A date: as_of minus a uniform number of days in [min_days, max_days). Generators never read the clock."""

    op: Literal["days_before_as_of"]
    draw: str = Field(pattern=DRAW)
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row, shared by all its rows (e.g. a member's premium)")
    min_days: int = Field(ge=0)
    max_days: int

    @model_validator(mode="after")
    def _range(self) -> "DaysBeforeAsOf":
        if self.max_days <= self.min_days:
            raise ValueError(f"days_before_as_of {self.draw!r}: max_days must be greater than min_days")
        return self


class MonthsBeforeAsOf(_Op):
    """The first day of the month `months` months before the as-of month (0 = the as-of month)."""

    op: Literal["months_before_as_of"]
    months: Union[int, "Expr"]


class AddDays(_Op):
    """A date expression plus `days` (a number or a per-row expression; negative moves back). Null stays null."""

    op: Literal["add_days"]
    of: "Expr"
    days: Union[int, "Expr"]


class Format(_Op):
    """Text from a template: {field} for anchor fields and earlier columns, {index} for the row index,
    with Python format specs, e.g. "INV-{member_number}-{index:02d}"."""

    op: Literal["format"]
    template: str


class Lookup(_Op):
    """A value of `column` from a row of a table generated in an earlier stage (e.g. a pharmacy's npi from provider),
    picked among the rows matching `where` (column -> allowed values), ordered by primary key. Lookups with the same
    draw, table and where pick the same row, so related columns (npi and org_name) stay consistent. Use it for
    foreign keys to reference tables and for names shown with them."""

    op: Literal["lookup"]
    draw: str = Field(pattern=DRAW)
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row")
    table: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)
    where: dict[str, list[str]] = Field(default_factory=dict)


class MapValues(_Op):
    """Look up each row's value of `of` in `values`; rows without a match get `default`. Keys are compared as text
    (a key 1 matches the value 1)."""

    op: Literal["map"]
    of: "Expr"
    values: dict[str, Any]
    default: Any = None

    @field_validator("values", mode="before")
    @classmethod
    def _text_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            nested = [str(k) for k, x in v.items() if isinstance(x, dict) and "op" in x]
            if nested:
                raise ValueError(f"map values are constants, not expressions (keys {nested}); to vary a random value by "
                                 "category, map its bounds instead, e.g. {op: money, low: {op: map, ...}, high: {op: map, ...}}")
            return {str(k): x for k, x in v.items()}
        return v


class Digits(_Op):
    """A random string of `length` digits (a synthetic code or number, e.g. an 11-digit NDC). Combine with text
    through an earlier column and `format`."""

    op: Literal["digits"]
    draw: str = Field(pattern=DRAW)
    per: Literal["row", "anchor"] = Field("row", description="row: a value per row; anchor: one value per anchor row")
    length: int = Field(ge=1, le=18)


class Equals(_Op):
    """True where `of` equals `value`."""

    op: Literal["eq"]
    of: "Expr"
    value: Any


class OneOf(_Op):
    """True where `of` is one of `values`."""

    op: Literal["in"]
    of: "Expr"
    values: list[Any] = Field(min_length=1)


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
    Union[Const, AnchorField, ColumnRef, RowIndex, RecipeFlag, Choice, Bernoulli, Integer, Money, DaysBeforeAsOf,
          MonthsBeforeAsOf, AddDays, Format, Lookup, MapValues, Digits, Equals, OneOf, AnyOf, AllOf, Not, IfThen, RowId],
    Field(discriminator="op"),
]
for _m in (Bernoulli, Money, MonthsBeforeAsOf, AddDays, MapValues, Equals, OneOf, AnyOf, AllOf, Not, IfThen):
    _m.model_rebuild()


# ------------------------------------------------------------------ anchors
ANCHOR_FIELDS = {"member": ["member_id", "member_number", "lob", "plan_id", "sex", "premium_status"]}
ANCHOR_STAGE = {"member": "s4"}
ANCHOR_STREAM = {"member": "member_demographics"}  # entity whose row streams the anchor uses
ANCHOR_TABLE = {"member": ("member_demographics", "member_id")}


def _recipe_fields() -> set[str]:
    from factory.data_engine.scenarios import Scenario

    return {f.name for f in dataclasses.fields(Scenario)} - {"id", "anchor", "kind", "title"}


_RECIPE_FIELDS = _recipe_fields()


def _earlier_tables(stage: str) -> set[str]:
    """Core tables generated before `stage` (lookups read them while the stage runs)."""
    from factory.data_engine import catalog

    order = ["s3", "s4", "s5", "s6"]
    return {e for st in order[:order.index(stage)] for e in catalog.STAGE_ENTITIES[st] if e in catalog.CORE_ENTITIES}


# ------------------------------------------------------------------ entity packs
class References(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME)
    type: Literal["string", "bool", "int", "float", "date", "money"]
    gen: Expr
    references: References | None = Field(None, description="foreign key; becomes rule R-01:<entity>.<column>")
    description: str = ""


class Rows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_anchor: Literal["zero_or_one", "many"] = "zero_or_one"
    when: Expr | None = Field(None, description="anchor rows that get rows (all when omitted)")
    count: Expr | None = Field(None, description="many: rows per anchor row (clipped to slots)")
    slots: int | None = Field(None, ge=2, le=64, description="many: key slots reserved per anchor row; never change it")

    @model_validator(mode="after")
    def _many(self) -> "Rows":
        if self.per_anchor == "many" and (self.count is None or self.slots is None):
            raise ValueError("per_anchor many needs count and slots")
        if self.per_anchor == "zero_or_one" and (self.count is not None or self.slots is not None):
            raise ValueError("count and slots are for per_anchor many")
        return self


class RuleSpec(BaseModel):
    """A data rule: SQL returning one number, the count of violating rows (0 = pass)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=RULE_ID, description="family letter first (R/T/F/C/P/A), e.g. F-PREMIUM-01")
    description: str
    sql: str = Field(description="SELECT count(*) of violating rows; write AS_OF_DATE for the run's as-of date (never "
                                 "CURRENT_DATE or now())")
    needs: list[str] = Field(default_factory=list, description="tables the rule reads (it is skipped without them)")


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
        anchor_fields = set(ANCHOR_FIELDS[self.anchor])
        level = [x for x in (self.rows.when, self.rows.count) if x is not None]
        for e in _walk(level):  # evaluated per anchor row: no columns or row index exist yet
            if isinstance(e, (ColumnRef, RowIndex, Format)):
                raise ValueError(f"rows.when / rows.count cannot use {e.op!r}: they are evaluated per anchor row")
        for i, col in enumerate(self.columns):
            earlier = set(names[:i])
            for e in _walk([col.gen]):
                if isinstance(e, ColumnRef) and e.name not in earlier:
                    raise ValueError(f"column {col.name!r}: {{op: column, name: {e.name}}} must name an earlier column")
                if isinstance(e, Format):
                    for _, fld, _, _ in string.Formatter().parse(e.template):
                        if fld and fld != "index" and fld not in anchor_fields | earlier:
                            raise ValueError(f"column {col.name!r}: template field {{{fld}}} is not an anchor field or earlier column")
        for e in _walk([c.gen for c in self.columns] + level):
            if isinstance(e, AnchorField) and e.field not in anchor_fields:
                raise ValueError(f"anchor field {e.field!r} unknown for anchor {self.anchor}; known: {sorted(anchor_fields)}")
            if isinstance(e, RecipeFlag) and e.flag not in _RECIPE_FIELDS:
                raise ValueError(f"recipe flag {e.flag!r} is not a Scenario field")
            if isinstance(e, Lookup):
                earlier = _earlier_tables(self.stage)
                if e.table not in earlier:
                    raise ValueError(f"lookup table {e.table!r} is not generated before stage {self.stage}; "
                                     f"use one of {sorted(earlier)}")
        return self


class EntityPack(BaseModel):
    """A capability pack that adds one entity."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^entity\.[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    kind: Literal["entity"] = "entity"
    core: bool = Field(False, description="part of the built-in catalog (may use the anchor's random streams)")
    description: str = ""
    entity: EntitySpec
    rules: list[RuleSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "EntityPack":
        if self.id != f"entity.{self.entity.name}":
            raise ValueError(f"pack id {self.id!r} must be 'entity.{self.entity.name}'")
        if self.entity.random_stream == "anchor" and not self.core:
            raise ValueError("random_stream 'anchor' shares draw names with core generators; only core packs may use it")
        if self.entity.random_stream == "anchor" and self.entity.rows.per_anchor != "zero_or_one":
            raise ValueError("random_stream 'anchor' needs per_anchor zero_or_one (row keys must be anchor keys)")
        for r in self.rules:
            check_rule_sql(r.sql)
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def digest(self) -> str:
        return _digest(self)


# ------------------------------------------------------------------ use-case packs
class FieldMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME, description="name in the API response")
    column: str = Field(pattern=NAME)


class QueryParam(BaseModel):
    """An optional filter: ?<name>=<value> keeps rows whose column equals the value (case-insensitive)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)
    values: list[str] | None = Field(None, description="allowed values (others are 422)")
    description: str = ""


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(pattern=NAME)
    desc: bool = False


class Aggregate(BaseModel):
    """A summary over all of the member's matching rows (not only the returned page)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME)
    fn: Literal["sum", "count", "max", "min"]
    column: str | None = Field(None, pattern=NAME, description="not needed for count")
    where: dict[str, list[str]] = Field(default_factory=dict, description="column -> values the row must have")

    @model_validator(mode="after")
    def _column(self) -> "Aggregate":
        if self.fn != "count" and self.column is None:
            raise ValueError(f"aggregate {self.name!r}: {self.fn} needs a column")
        return self


class ItemLookup(BaseModel):
    """Single-row endpoint: the path parameter `param` must match `column` (404 RESOURCE_NOT_FOUND otherwise)."""

    model_config = ConfigDict(extra="forbid")

    param: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)


class Limit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: int = Field(20, ge=1)
    max: int = Field(100, ge=1, le=500)


class EndpointSpec(BaseModel):
    """A member-scoped read endpoint: rows of `entity` belonging to the member in the path."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET"] = "GET"
    path: str = Field(pattern=r"^/members/\{member_id\}(/[a-z][a-z0-9_-]*)+(/\{[a-z_]+\})?$",
                      description="under the base path, e.g. /members/{member_id}/premiums")
    operation_id: str = Field(pattern=NAME)
    summary: str
    description: str = ""
    entity: str = Field(pattern=NAME)
    member_column: str = Field("member_id", pattern=NAME, description="the entity's column holding member_id")
    item: ItemLookup | None = None
    fields: list[FieldMap] = Field(min_length=1)
    query_params: list[QueryParam] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: Limit = Limit()
    aggregates: list[Aggregate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "EndpointSpec":
        extra = re.findall(r"\{([a-z_]+)\}", self.path)[1:]
        if self.item is None and extra:
            raise ValueError(f"{self.path}: path parameter {extra} needs an item lookup")
        if self.item is not None and extra != [self.item.param]:
            raise ValueError(f"{self.path}: item endpoints end with /{{{self.item.param}}}")
        if self.item is not None and (self.query_params or self.aggregates):
            raise ValueError(f"{self.operation_id}: item endpoints take no query parameters or aggregates")
        return self

    def columns(self) -> set[str]:
        """Every column of `entity` the endpoint reads (checked against the table by the registry)."""
        cols = {self.member_column, *(f.column for f in self.fields), *(q.column for q in self.query_params),
                *(o.column for o in self.order_by)}
        cols |= {a.column for a in self.aggregates if a.column} | {c for a in self.aggregates for c in a.where}
        if self.item:
            cols.add(self.item.column)
        return cols


class UseCaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[A-Z]{3}$")
    name: str
    group: Literal["member"] = "member"
    caregiver_scope: str = Field(pattern=r"^[A-Z_]+$", description="scope a caregiver's authorization must include (R-06)")
    personas: list[str] = Field(min_length=1)
    can: list[str] = Field(min_length=1)
    escalate: list[str] = Field(min_length=1)
    never: list[str] = Field(min_length=1)
    verification: list[str] = Field(min_length=1)
    handoff_queue: str = Field(pattern=r"^[A-Z_]+$")
    faqs: list[list[str]] = Field(min_length=1, description="question variants, one list per `can` item")
    needs: list[str] = Field(min_length=1, description="tables the endpoints read (s8 checks they were generated)")
    knowledge_slot: int | None = Field(None, ge=10, le=98, description="knowledge-asset slot; default derived from the code")
    endpoints: list[EndpointSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "UseCaseSpec":
        ops = [e.operation_id for e in self.endpoints]
        if len(set(ops)) != len(ops):
            raise ValueError("operation ids must be unique")
        if len(self.faqs) != len(self.can):
            raise ValueError("faqs needs one list of question variants per `can` item")
        missing = sorted({e.entity for e in self.endpoints} - set(self.needs))
        if missing:
            raise ValueError(f"endpoints read {missing}; list them in needs")
        return self

    @property
    def slot(self) -> int:
        """Knowledge-asset slot (core use cases use 0-9): fixed per code, so asset ids never shift."""
        return self.knowledge_slot if self.knowledge_slot is not None else 10 + zlib.crc32(self.code.encode()) % 89


class UseCasePack(BaseModel):
    """A capability pack that adds an API use case."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^uc\.[A-Z]{3}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    kind: Literal["use_case"] = "use_case"
    core: bool = False
    description: str = ""
    use_case: UseCaseSpec

    @model_validator(mode="after")
    def _check(self) -> "UseCasePack":
        if self.id != f"uc.{self.use_case.code}":
            raise ValueError(f"pack id {self.id!r} must be 'uc.{self.use_case.code}'")
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def digest(self) -> str:
        return _digest(self)


AnyPack = Annotated[Union[EntityPack, UseCasePack], Field(discriminator="kind")]
_PACK = TypeAdapter(AnyPack)
Pack = EntityPack  # phase 0 name for the entity kind


def parse_pack(doc: Any) -> EntityPack | UseCasePack:
    """Validate a pack document (dict from YAML or JSON) of either kind; kind defaults to entity."""
    if isinstance(doc, dict) and "kind" not in doc:
        doc = {**doc, "kind": "entity"}
    return _PACK.validate_python(doc)


def pack_json_schema() -> dict:
    return _PACK.json_schema(by_alias=True)


# ------------------------------------------------------------------ helpers
def _parse_rule(sql: str):
    """Parse rule SQL (DuckDB dialect) into one SELECT, or raise ValueError with the reason."""
    import sqlglot
    from sqlglot import exp

    try:
        stmts = [x for x in sqlglot.parse(sql, read="duckdb") if x is not None]
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"rule SQL does not parse: {str(exc).splitlines()[0][:200]}") from None
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Select):
        raise ValueError("rule SQL must be exactly one SELECT statement")
    return stmts[0], exp


def check_rule_sql(sql: str) -> set[str]:
    """Rule SQL must be one read-only SELECT whose only output is count(*), over plain table names, with functions
    the parser knows. Returns the tables it reads. Parsed with sqlglot (phase 2: agents write these rules)."""
    sel, exp = _parse_rule(sql)
    outputs = sel.expressions
    if len(outputs) != 1 or not (isinstance(outputs[0], exp.Count) and isinstance(outputs[0].this, exp.Star)):
        raise ValueError("rule SQL must return exactly count(*): the number of violating rows")
    for node in sel.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command, exp.Alter)):
            raise ValueError(f"rule SQL may not contain {type(node).__name__.upper()}")
        if isinstance(node, exp.Anonymous):
            raise ValueError(f"rule SQL uses an unknown function {node.name!r}")
        if isinstance(node, exp.Func) and type(node).__name__.lower().startswith("read"):
            raise ValueError(f"rule SQL may not read files ({node.sql()[:60]})")
        if isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.CurrentDatetime)):
            raise ValueError("rule SQL may not read the clock (CURRENT_DATE, now()); write AS_OF_DATE for the run's as-of date")
    ctes = {c.alias_or_name.lower() for c in sel.find_all(exp.CTE)}
    tables = set()
    for t in sel.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier) or t.args.get("db") or t.args.get("catalog"):
            raise ValueError(f"rule SQL may only read plain table names, not {t.sql()[:60]!r}")
        if t.name.lower() not in ctes:
            tables.add(t.name.lower())
    return tables


def sql_tables(sql: str) -> set[str]:
    """Tables a rule reads (parsed)."""
    return check_rule_sql(sql)


def _digest(pack: BaseModel) -> str:
    """sha256 of the canonical JSON: recorded per run so a replay uses exactly this pack."""
    canon = json.dumps(pack.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":"))
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
    """Draw names used by the entity, sorted (they must not collide on a shared stream)."""
    exprs = [c.gen for c in spec.columns] + [x for x in (spec.rows.when, spec.rows.count) if x is not None]
    return sorted({e.draw for e in _walk(exprs) if hasattr(e, "draw")})
