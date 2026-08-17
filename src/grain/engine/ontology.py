"""The logical layer, as data. These types are the whole vocabulary the engine
reasons over; nothing here knows about any particular database."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
LinkKind = Literal["direct", "through", "recursive"]
ValueType = Literal["string", "integer", "decimal", "boolean", "date", "datetime"]

FANNING: frozenset[str] = frozenset({"one_to_many", "many_to_many"})


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
    to: str
    kind: Literal["left", "inner"] = "left"
    on: list[JoinPair]


class AiContext(BaseModel):
    synonyms: list[str] = Field(default_factory=list)
    instructions: str | None = None


class Property(BaseModel):
    column: ColumnRef
    type: ValueType
    nullable: bool = False
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
    def fans_out(self) -> bool:
        """True when traversing this edge can replicate the rows we started with."""
        return self.cardinality in FANNING


class Metric(BaseModel):
    name: str
    grain: str
    expr: str
    type: ValueType
    description: str | None = None
    ai_context: AiContext | None = None


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
