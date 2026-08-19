"""The agent's only input. Every string here must name something the ontology
declares, so an invalid request is unrepresentable rather than merely rejected."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FilterOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "like", "is_null"]

STRICT = ConfigDict(extra="forbid")


class Filter(BaseModel):
    model_config = STRICT
    property: str
    op: FilterOp
    value: Any = None

    @model_validator(mode="after")
    def _check_value_matches_op(self) -> "Filter":
        """`in` needs a sequence; `is_null` takes no value. Catching this here
        keeps the failure a typed validation error at the door, instead of a
        SQLAlchemy ArgumentError surfacing from deep inside the compiler."""
        if self.op == "in" and not isinstance(self.value, (list, tuple)):
            raise ValueError("op 'in' requires a list or tuple of values")
        if self.op == "is_null" and self.value is not None:
            raise ValueError("op 'is_null' takes no value")
        return self


class Hop(BaseModel):
    model_config = STRICT
    link: str
    max_depth: int | None = Field(default=None, ge=1, le=50)


class OrderBy(BaseModel):
    model_config = STRICT
    key: str
    desc: bool = False


class QuerySpec(BaseModel):
    model_config = STRICT
    object: str
    filters: list[Filter] = Field(default_factory=list)
    traverse: list[Hop] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    # No upper bound: this database is small and known (largest table 8,715
    # rows), so a hard ceiling here would add friction without buying real
    # protection -- GuardConfig.row_cap is the actual backstop against an
    # unbounded result, and unlike this field it can be sized to the data
    # rather than to an arbitrary round number. `None` is legal and means "no
    # LIMIT clause at all"; `ge=1` still rejects zero and negative counts,
    # which are never a real request. The default of 100 stays -- removing
    # the ceiling on what's expressible is not the same as making unbounded
    # the default, and a caller that forgot to think about size should still
    # get a small result back.
    limit: int | None = Field(default=100, ge=1)
