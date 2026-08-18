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


def _apply_edge(stmt: Select[Any], metadata: MetaData, edge: Edge) -> Select[Any]:
    """Apply one edge's join(s), then that target's own extra table joins —
    but only once the target has actually landed in FROM.

    `direct` joins the target table directly; `through` joins the junction
    table and then the target, so the junction never reaches the caller.
    Both bring `edge.to_object` into scope, so both apply its object joins.
    `recursive` (Task 12) self-joins on the object already in scope, so it
    must stay out of this — applying object joins again would duplicate them.
    """
    if edge.link.kind == "direct":
        stmt = stmt.join(
            metadata.tables[edge.to_object.primary], _edge_onclause(metadata, edge)
        )
        stmt = _apply_object_joins(stmt, metadata, edge.to_object)
    elif edge.link.kind == "through":
        via = metadata.tables[edge.link.via]
        stmt = stmt.join(
            via,
            and_(
                *[
                    _column(metadata, p.from_.table, p.from_.column)
                    == _column(metadata, p.to.table, p.to.column)
                    for p in edge.link.on_from
                ]
            ),
        )
        stmt = stmt.join(
            metadata.tables[edge.to_object.primary],
            and_(
                *[
                    _column(metadata, p.from_.table, p.from_.column)
                    == _column(metadata, p.to.table, p.to.column)
                    for p in edge.link.on_to
                ]
            ),
        )
        stmt = _apply_object_joins(stmt, metadata, edge.to_object)
    # `recursive` edges: Task 12 owns emitting the CTE. Left unjoined here.
    return stmt


def _apply_edges(stmt: Select[Any], metadata: MetaData, edges: list[Edge]) -> Select[Any]:
    """Apply a specific list of edges. Task 13's metric subquery passes only the
    prefix reaching its grain; the outer query passes the whole path — same
    helper either way, so a `through`/`recursive` edge can't be dropped from
    one caller and not the other."""
    for edge in edges:
        stmt = _apply_edge(stmt, metadata, edge)
    return stmt


def _apply_path(stmt: Select[Any], metadata: MetaData, rq: ResolvedQuery) -> Select[Any]:
    """Apply every edge on the walked path, plus each target's object joins."""
    return _apply_edges(stmt, metadata, rq.path)


def _exists_clause(metadata: MetaData, rf: ResolvedFilter) -> ColumnElement[bool]:
    """A filter across a fanning edge, as `EXISTS` rather than a join — a join
    would return one row per matching child, silently multiplying the parent.

    `direct`: correlate straight to the target table. `through`: correlate via
    the junction table so it, like everywhere else, never surfaces to the
    caller — not in FROM of the outer query, not in a selected column, and
    not left exposed here as the thing the EXISTS binds against.
    """
    link = rf.hops[0]
    target = metadata.tables[rf.property.prop.column.table]
    if link.kind == "through":
        via = metadata.tables[link.via]
        sub = (
            select(1)
            .select_from(via)
            .join(
                target,
                and_(
                    *[
                        _column(metadata, p.from_.table, p.from_.column)
                        == _column(metadata, p.to.table, p.to.column)
                        for p in link.on_to
                    ]
                ),
            )
            .where(
                and_(
                    *[
                        _column(metadata, p.from_.table, p.from_.column)
                        == _column(metadata, p.to.table, p.to.column)
                        for p in link.on_from
                    ],
                    _filter_clause(metadata, rf),
                )
            )
        )
    else:
        sub = (
            select(1)
            .select_from(target)
            .where(
                and_(
                    *[
                        _column(metadata, p.from_.table, p.from_.column)
                        == _column(metadata, p.to.table, p.to.column)
                        for p in link.on
                    ],
                    _filter_clause(metadata, rf),
                )
            )
        )
    return sub.exists()


def compile_query(rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData) -> Select[Any]:
    group_cols = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    stmt = select(*group_cols).select_from(metadata.tables[rq.root.primary])
    stmt = _apply_object_joins(stmt, metadata, rq.root)
    stmt = _apply_path(stmt, metadata, rq)

    # A filter across a fanning edge (one_to_many / many_to_many) must not
    # become a join — that returns one row per matching child and silently
    # multiplies the parent. Route those to EXISTS instead; everything else
    # is a plain WHERE.
    plain = [rf for rf in rq.filters if not any(l.fans_out for l in rf.hops)]
    existential = [rf for rf in rq.filters if any(l.fans_out for l in rf.hops)]

    if plain:
        stmt = stmt.where(and_(*[_filter_clause(metadata, rf) for rf in plain]))
    for rf in existential:
        stmt = stmt.where(_exists_clause(metadata, rf))

    if group_cols:
        stmt = stmt.group_by(*[_property_column(metadata, rp) for rp in rq.group_by])

    return stmt.limit(rq.limit)
