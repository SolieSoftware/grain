"""ResolvedQuery + GrainPlan -> SQLAlchemy Select. This module decides nothing;
every verdict was reached in grain.py. It only emits what was decided."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, Select, and_, select
from sqlalchemy.sql import ColumnElement

from .grain import GrainPlan
from .ontology import ObjectType
from .resolve import Edge, ResolvedFilter, ResolvedProperty, ResolvedQuery


def sql_text(stmt: Select[Any]) -> str:
    """Render SQL with literal binds — for logging, provenance and tests."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _column(metadata: MetaData, table: str, column: str) -> Column[Any]:
    return metadata.tables[table].columns[column]


def _property_column(metadata: MetaData, rp: ResolvedProperty) -> Column[Any]:
    return _column(metadata, rp.prop.column.table, rp.prop.column.column)


def _filter_clause(metadata: MetaData, rf: ResolvedFilter) -> ColumnElement[bool]:
    col = _property_column(metadata, rf.property)
    op = rf.op
    if op == "eq":
        return col == rf.value
    if op == "ne":
        return col != rf.value
    if op == "gt":
        return col > rf.value
    if op == "gte":
        return col >= rf.value
    if op == "lt":
        return col < rf.value
    if op == "lte":
        return col <= rf.value
    if op == "in":
        return col.in_(rf.value)
    if op == "like":
        return col.like(rf.value)
    if op == "is_null":
        return col.is_(None)
    raise AssertionError(f"unhandled filter op '{op}'")  # spec.FilterOp is closed


def _edge_onclause(metadata: MetaData, edge: Edge) -> ColumnElement[bool]:
    pairs = edge.link.on
    return and_(
        *[
            _column(metadata, p.from_.table, p.from_.column)
            == _column(metadata, p.to.table, p.to.column)
            for p in pairs
        ]
    )


def _apply_object_joins(stmt: Select[Any], metadata: MetaData, obj: ObjectType) -> Select[Any]:
    """Join the extra tables ONE object spans. Always outer — an inner join to
    `genre` would silently drop every track without one.

    Call this for an object only once that object is itself in scope, or the
    extra tables reference a FROM element that does not exist yet.
    """
    for join in obj.joins.values():
        onclause = and_(
            *[
                _column(metadata, p.from_.table, p.from_.column)
                == _column(metadata, p.to.table, p.to.column)
                for p in join.on
            ]
        )
        stmt = stmt.join(metadata.tables[join.to], onclause, isouter=(join.kind == "left"))
    return stmt


def compile_query(rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData) -> Select[Any]:
    group_cols = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    stmt = select(*group_cols).select_from(metadata.tables[rq.root.primary])
    stmt = _apply_object_joins(stmt, metadata, rq.root)

    for edge in rq.path:
        if edge.link.kind == "direct":
            stmt = stmt.join(
                metadata.tables[edge.to_object.primary], _edge_onclause(metadata, edge)
            )
            stmt = _apply_object_joins(stmt, metadata, edge.to_object)
        # `through` and `recursive` edges arrive in Tasks 11 and 12 — left
        # unjoined here, not raised on and not half-implemented.

    clauses = [_filter_clause(metadata, rf) for rf in rq.filters]
    if clauses:
        stmt = stmt.where(and_(*clauses))

    if group_cols:
        stmt = stmt.group_by(*[_property_column(metadata, rp) for rp in rq.group_by])

    return stmt.limit(rq.limit)
