"""The logical layer, as data. These types are the whole vocabulary the engine
reasons over; nothing here knows about any particular database."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
LinkKind = Literal["direct", "through", "recursive"]
ValueType = Literal["string", "integer", "decimal", "boolean", "date", "datetime"]

FANNING: frozenset[str] = frozenset({"one_to_many", "many_to_many"})

AggFunc = Literal["sum", "count", "count_distinct", "min", "max", "avg"]

QuantityKind = Literal["extensive", "rate", "ratio"]

ACCUMULATES: frozenset[str] = frozenset({"extensive"})
"""Kinds that may be summed.

An EXTENSIVE quantity scales with the size of the set it is measured over --
money, counts, durations. Adding two of them yields a quantity of the same kind,
which is what makes a total mean anything.

A RATE is per-unit (a price, a speed) and a RATIO is a proportion (a percentage,
a share). Neither accumulates: summing prices produces a number with no
referent, and summing percentages can exceed 100. They can be averaged,
minimised and maximised perfectly well -- only `sum` is refused.

The distinction is not decoration. `grain` validates a metric's GRAIN -- that
its rows are not replicated -- and had no way to say whether the QUANTITY was
additive at all, so `sum(track.unit_price)` came back arithmetically perfect,
flagged `additive: true`, and answered no question."""

FANOUT_IMMUNE: frozenset[str] = frozenset({"min", "max", "count_distinct"})
"""Aggregates that row replication cannot change.

`min`/`max` of a multiset are unaffected by duplicates, and `count(distinct x)`
dedupes by construction. A fanning join downstream of such a metric's grain
therefore needs no rewrite at all -- the naive inline aggregate is already the
right number. The planner never knew this because it read path cardinality and
never looked at the metric, so chinook's three `count(distinct pk)` metrics were
pre-aggregated for nothing.

This is about the AGGREGATE only. It says nothing about whether the groups
overlap, which is a property of the path and is decided separately -- an immune
metric grouped across a many_to_many is still non-additive.
"""


class ColumnRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    table: str
    column: str

    @classmethod
    def parse(cls, value: str | "ColumnRef" | dict[str, Any]) -> "ColumnRef":
        if isinstance(value, ColumnRef):
            return value
        if isinstance(value, dict):
            return cls(**value)
        if not isinstance(value, str):
            raise ValueError(f"Column reference must be 'table.column', got '{value}'")
        table, sep, column = value.partition(".")
        if not sep or not table or not column:
            raise ValueError(f"Column reference must be 'table.column', got '{value}'")
        return cls(table=table, column=column)

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


def _as_column_ref(value: Any) -> ColumnRef:
    return ColumnRef.parse(value)


class JoinPair(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: ColumnRef = Field(alias="from")
    to: ColumnRef

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> ColumnRef:
        return _as_column_ref(v)


class TableJoin(BaseModel):
    """One extra table a single object SPANS — not a relationship between two
    objects, which is a `LinkType`.

    `cardinality` is REQUIRED and has no default, deliberately. It used to be
    absent entirely, which meant every object join was silently assumed
    many_to_one and the engine's central claim — that every verdict is decided
    from declared cardinality alone — was false for a whole class of join
    (defect C5). A fanning object join replicated the object's own rows with no
    rewrite and no flag: `sum(thing.weight)` over two fanning joins returned 6
    against a true 3, reported as `additive: true` with no rewrites.

    A default would have reintroduced exactly the silent assumption that caused
    it, so there is none: the author states the cardinality, and `loader.py`
    checks the statement against the database's own constraints rather than
    trusting it.
    """

    to: str
    kind: Literal["left", "inner"] = "left"
    cardinality: Cardinality
    on: list[JoinPair]

    @property
    def fans_out(self) -> bool:
        """True when joining this table can replicate the object's own rows."""
        return self.cardinality in FANNING


class AiContext(BaseModel):
    synonyms: list[str] = Field(default_factory=list)
    instructions: str | None = None


class Property(BaseModel):
    """`quantity` declares what KIND of number this is, so the loader can refuse
    a metric that sums something which does not accumulate. Required only on a
    column some `sum` metric actually reads -- a name or a country is never
    summed and needs nothing said about it.

    `unique: true` declares that this property identifies ONE row of its
    object — a claim `loader.py` checks against the database's primary key and
    unique constraints, never merely accepts.

    It exists because non-additivity's per-group guarantee depends on it. "Each
    group is still correct on its own" is true only when a group is one root
    row; grouping `revenue` by `playlist.name`, where two playlists share a
    name, merged them and double-counted every track they both held (defect
    C2). `grain.analyse` now requires a unique key on any non-additive query, so
    this flag is load-bearing rather than documentation.
    """

    column: ColumnRef
    type: ValueType
    nullable: bool = False
    unique: bool = False
    quantity: QuantityKind | None = None
    via: str | None = None
    description: str | None = None

    @field_validator("column", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> ColumnRef:
        return _as_column_ref(v)


class ObjectType(BaseModel):
    name: str
    primary: str
    title_property: str | None = None
    description: str | None = None
    joins: dict[str, TableJoin] = Field(default_factory=dict)
    properties: dict[str, Property] = Field(default_factory=dict)
    ai_context: AiContext | None = None

    @property
    def tables(self) -> list[str]:
        return [self.primary] + [j.to for j in self.joins.values()]


class LinkType(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    from_: str = Field(alias="from")
    to: str
    kind: LinkKind
    cardinality: Cardinality
    on: list[JoinPair] = Field(default_factory=list)
    via: str | None = None
    on_from: list[JoinPair] = Field(default_factory=list)
    on_to: list[JoinPair] = Field(default_factory=list)
    max_depth: int = 10
    inverse_of: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "LinkType":
        if self.kind in ("direct", "recursive") and not self.on:
            raise ValueError(f"Link '{self.name}' of kind '{self.kind}' requires 'on'")
        if self.kind == "through":
            if not self.via:
                raise ValueError(f"Link '{self.name}' of kind 'through' requires 'via'")
            if not self.on_from or not self.on_to:
                raise ValueError(
                    f"Link '{self.name}' of kind 'through' requires 'on_from' and 'on_to'"
                )
        return self

    @property
    def effective_cardinality(self) -> Cardinality:
        """What TRAVERSING this link does to the rows it started from.

        For a recursive link this is deliberately NOT the declared cardinality.
        `cardinality` states one hop — each employee has exactly one manager, so
        `many_to_one` — and that is the fact an ontology author actually knows.
        But traversing a recursive link walks the depth-bounded CLOSURE: each
        employee has many ancestors, and each ancestor has many descendants.
        That relation is `many_to_many`, so it both fans out (a metric's rows are
        replicated once per ancestor) and makes groups overlap (a fact reachable
        from an employee is reachable from every ancestor above them).

        Deriving the closure here rather than asking the author to declare it
        keeps one fact in the ontology and puts the consequence in one place. It
        also means a recursive traversal is automatically caught by the
        non-additivity machinery C2 built, instead of needing its own rule.

        `max_depth == 1` is the exception and the escape hatch: one hop IS the
        declared cardinality, so `Hop(link="Employee_Manager", max_depth=1)` asks
        for the direct manager, with no fan-out and no non-additivity.
        """
        if self.kind == "recursive" and self.max_depth > 1:
            return "many_to_many"
        return self.cardinality

    @property
    def fans_out(self) -> bool:
        """True when traversing this edge can replicate the rows we started with."""
        return self.effective_cardinality in FANNING


class Metric(BaseModel):
    """An aggregate, declared either opaquely or structurally.

    `expr` is the original form: one SQL string, rendered verbatim, opaque to
    the engine. `agg` + `value` is the structured form, which splits the
    aggregate function from the per-row expression it wraps.

    The split is not cosmetic. Two things need the pieces separately: the
    fan-out taxonomy needs the FUNCTION, to know whether replication can change
    the answer, and the symmetric engine needs the VALUE, so it can wrap it in
    an encoded aggregate. Neither is recoverable from `expr` without parsing
    SQL, which would make a wrong answer depend on the quality of a regex.

    Exactly one form, enforced rather than defaulted. A metric with both would
    have two renderings and no rule saying which wins; a metric with neither
    would compile to `literal_column(None)` deep inside the compiler instead of
    failing at the door.
    """

    name: str
    grain: str
    type: ValueType
    expr: str | None = None
    agg: AggFunc | None = None
    value: str | None = None
    description: str | None = None
    ai_context: AiContext | None = None

    @model_validator(mode="after")
    def _check_exactly_one_form(self) -> "Metric":
        structured = self.agg is not None and self.value is not None
        partial = (self.agg is None) != (self.value is None)
        if partial or (structured == (self.expr is not None)):
            raise ValueError(
                f"metric '{self.name}' must declare exactly one of 'expr' or "
                f"'agg' + 'value' (both of that pair, together)"
            )
        return self

    @property
    def is_structured(self) -> bool:
        return self.agg is not None

    @property
    def sql_expr(self) -> str:
        """The aggregate as SQL. One accessor, so the compiler never branches on
        which form was declared."""
        if self.expr is not None:
            return self.expr
        if self.agg == "count_distinct":
            return f"count(distinct {self.value})"
        return f"{self.agg}({self.value})"

    @property
    def fanout_immune(self) -> bool:
        """True when row replication cannot change this metric's value.

        An opaque `expr` is never immune. It may well BE a `min` or a
        `count(distinct ...)`, but nothing here can prove that, and guessing
        would trade a needless subquery for a possible wrong number.
        """
        return self.agg in FANOUT_IMMUNE


class Ontology(BaseModel):
    name: str
    description: str | None = None
    objects: dict[str, ObjectType] = Field(default_factory=dict)
    links: dict[str, LinkType] = Field(default_factory=dict)
    metrics: dict[str, Metric] = Field(default_factory=dict)

    def links_from(self, object_name: str) -> list[LinkType]:
        return [l for l in self.links.values() if l.from_ == object_name]

    def object_for_table(self, table: str) -> ObjectType | None:
        for obj in self.objects.values():
            if obj.primary == table:
                return obj
        return None
