"""ResolvedQuery + GrainPlan -> SQLAlchemy Select. This module decides nothing;
every verdict was reached in grain.py. It only emits what was decided."""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    MetaData,
    Select,
    and_,
    literal,
    literal_column,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import CTE

from .errors import FanOutRefused
from .grain import GrainPlan, MetricPlan, path_to_table
from .ontology import ColumnRef, JoinPair, Metric, ObjectType
from .resolve import Edge, ResolvedFilter, ResolvedProperty, ResolvedQuery


def sql_text(stmt: Select[Any]) -> str:
    """Render SQL with literal binds — for logging, provenance and tests."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _column(metadata: MetaData, table: str, column: str) -> Column[Any]:
    return metadata.tables[table].columns[column]


def _property_column(metadata: MetaData, rp: ResolvedProperty) -> Column[Any]:
    return _column(metadata, rp.prop.column.table, rp.prop.column.column)


def _filter_clause(
    metadata: MetaData, rf: ResolvedFilter, col: ColumnElement[Any] | None = None
) -> ColumnElement[bool]:
    """`col` overrides which column the comparison binds to — needed when the
    filter's table has been aliased inside an EXISTS."""
    col = _property_column(metadata, rf.property) if col is None else col
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


def _recursive_cte(metadata: MetaData, edge: Edge, suffix: str = "") -> CTE:
    """A self-referential link becomes a depth-bounded recursive CTE.

    `suffix` disambiguates the CTE's name. Every CTE anywhere in a statement —
    including one built inside a metric subquery — is hoisted into the SAME
    top-level `WITH` list, so two distinct CTE objects sharing a name make
    Postgres reject the statement outright. The caller therefore owns
    uniqueness: `_apply_edges` threads its own position plus a per-call-site
    tag through, so the outer path's CTE and a metric subquery's CTE for the
    very same link can never collide.

    `depth` bounds runaway recursion (`edge.link.max_depth`); `path` carries
    every visited id so a cycle in the data terminates the walk instead of
    looping forever. The two guard different failure modes: `max_depth` alone
    still lets a two-node cycle spin until the bound is hit; `path` alone
    still needs a floor in case future data has no cycle but is simply deep.

    The base case anchors at the top of the hierarchy (`child_col IS NULL` —
    e.g. `reports_to IS NULL`) and walks downward, matching the direction the
    integration anchor (`test_employee_hierarchy_is_three_levels`) measures.
    """
    table = metadata.tables[edge.to_object.primary]
    pair = edge.link.on[0]
    child_col = table.columns[pair.from_.column]  # e.g. employee.reports_to
    parent_col = table.columns[pair.to.column]  # e.g. employee.employee_id

    base = (
        select(
            table,
            literal(1).label("depth"),
            array((parent_col,)).label("path"),
        )
        .where(child_col.is_(None))
        .cte(name=f"{edge.link.name.lower()}_cte{suffix}", recursive=True)
    )
    step = (
        select(
            table,
            (base.c.depth + 1).label("depth"),
            (base.c.path + array((parent_col,))).label("path"),
        )
        .join(base, child_col == base.c[pair.to.column])
        .where(
            and_(
                base.c.depth < edge.link.max_depth,
                ~base.c.path.any(parent_col),
            )
        )
    )
    return base.union_all(step)


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


def _apply_edge(
    stmt: Select[Any], metadata: MetaData, edge: Edge, cte_suffix: str = ""
) -> Select[Any]:
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
    elif edge.link.kind == "recursive":
        # A recursive link self-joins the object already in scope — it must
        # NOT call _apply_object_joins for edge.to_object, since that object's
        # spanned tables are already joined (by the root's own object-joins
        # pass, or by an earlier edge that brought it into scope). Doing so
        # again would duplicate them.
        cte = _recursive_cte(metadata, edge, cte_suffix)
        pair = edge.link.on[0]
        # Join against the CTE's own column, not `edge.link.on`'s generic
        # from/to pair (that would compare the base table to itself, since
        # both sides of a recursive link's `on` name the same table).
        stmt = stmt.join(
            cte,
            _column(metadata, pair.to.table, pair.to.column) == cte.c[pair.to.column],
        )
    return stmt


def _apply_edges(
    stmt: Select[Any], metadata: MetaData, edges: list[Edge], tag: str = ""
) -> Select[Any]:
    """Apply a specific list of edges. Task 13's metric subquery passes only the
    prefix reaching its grain; the outer query passes the whole path — same
    helper either way, so a `through`/`recursive` edge can't be dropped from
    one caller and not the other.

    `tag` names the call site (the metric name, for a metric subquery) and the
    position index distinguishes repeats of one link within a single list — a
    self-referential link may legally be traversed twice. Together they give
    every recursive CTE in the finished statement a unique name.
    """
    for index, edge in enumerate(edges):
        suffix = f"_{tag}_{index}" if tag else f"_{index}"
        stmt = _apply_edge(stmt, metadata, edge, suffix)
    return stmt


def _apply_path(stmt: Select[Any], metadata: MetaData, rq: ResolvedQuery) -> Select[Any]:
    """Apply every edge on the walked path, plus each target's object joins."""
    return _apply_edges(stmt, metadata, rq.path)


def _exists_clause(metadata: MetaData, rf: ResolvedFilter) -> ColumnElement[bool]:
    """Every dotted filter, as `EXISTS` rather than a join-plus-WHERE.

    Across a fanning hop, a join would return one row per matching child,
    silently multiplying the parent. Across a non-fanning (many_to_one) hop
    there is no multiplying risk, but the hop's target table is never
    guaranteed to be in FROM (it only lands there if the same link also
    appears in `traverse`) — a plain WHERE on an unjoined table becomes an
    implicit, unconditional cartesian product that SQLAlchemy silently adds
    to FROM. EXISTS carries its own join condition, so that table can never
    appear unjoined.

    `direct`: correlate straight to the target table. `through`: correlate via
    the junction table so it, like everywhere else, never surfaces to the
    caller — not in FROM of the outer query, not in a selected column, and
    not left exposed here as the thing the EXISTS binds against.

    Correlation is stated explicitly rather than inferred. When the filter's
    target table is ALSO in the enclosing query's FROM — the same link both
    traversed and filtered on, or a metric subquery whose prefix reaches it —
    SQLAlchemy's auto-correlation strips that table out of the EXISTS, leaving
    it with no FROM at all and raising InvalidRequestError. Naming the
    correlated side (always the root, which the link's `from` columns live on)
    pins exactly one table as correlatable and leaves the target in place.

    A SELF-REFERENTIAL link needs more than that. Its `from` and `to` name the
    SAME table, so the correlated side and the target are one object and the
    EXISTS is left with no FROM at all — every column reference in it binds to
    the outer row, and `employees whose manager is named X` compiles to
    `employees who are their own manager`. Wrong rows, no error, no alias to
    tell them apart. So the target is aliased whenever it is also the
    correlated table, giving the inner row an identity of its own.
    """
    link = rf.hops[0]
    target = metadata.tables[rf.property.prop.column.table]
    outer = {p.from_.table for p in (link.on_from if link.kind == "through" else link.on)}
    inner = target.alias() if target.name in outer else target

    def _to(ref: ColumnRef) -> ColumnElement[Any]:
        """The INNER side of a join pair, read off the alias where there is one.

        The mapping is positional, not by table name: for a self-referential
        link both sides name the target, and only the `to` side belongs to the
        inner row. Aliasing by name would alias the correlated side too and
        correlate the EXISTS to nothing.
        """
        if ref.table == target.name:
            return inner.columns[ref.column]
        return _column(metadata, ref.table, ref.column)

    def _pairs(pairs: list[JoinPair]) -> list[ColumnElement[bool]]:
        return [_column(metadata, p.from_.table, p.from_.column) == _to(p.to) for p in pairs]

    predicate = _filter_clause(metadata, rf, inner.columns[rf.property.prop.column.column])
    if link.kind == "through":
        sub = (
            select(1)
            .select_from(metadata.tables[link.via])
            .join(inner, and_(*_pairs(link.on_to)))
            .where(and_(*_pairs(link.on_from), predicate))
        )
    else:
        sub = select(1).select_from(inner).where(and_(*_pairs(link.on), predicate))
    return sub.correlate(*[metadata.tables[name] for name in sorted(outer)]).exists()


def _apply_filters(stmt: Select[Any], metadata: MetaData, rq: ResolvedQuery) -> Select[Any]:
    """Every dotted filter (one with a hop) goes through EXISTS, fanning or not.

    A fanning hop as a plain join would return one row per matching child,
    multiplying the parent. A non-fanning (many_to_one) hop has no such risk on
    its own, but its target table is only ever joined into FROM when that same
    link also appears in `traverse` — left as a plain WHERE otherwise, the
    unjoined table still gets into the query via SQLAlchemy's implicit FROM
    inference, an unconditional cartesian product that runs without error and
    returns plausible but wrong rows. EXISTS carries its own join condition
    inside the subquery, so that unjoined-table cartesian is unconstructible
    rather than merely unlikely. A bare (non-dotted) filter has no hop and
    stays a plain WHERE — there is nothing to correlate.

    Both forms are anchored on the ROOT object (see `resolve._resolve_filter`),
    and the root table is in FROM for the outer query and for every metric
    subquery alike — which is what lets the same helper filter both.
    """
    plain = [rf for rf in rq.filters if not rf.hops]
    if plain:
        stmt = stmt.where(and_(*[_filter_clause(metadata, rf) for rf in plain]))
    for rf in (rf for rf in rq.filters if rf.hops):
        stmt = stmt.where(_exists_clause(metadata, rf))
    return stmt


def _metric_expr(metric: Metric) -> ColumnElement[Any]:
    """The metric's aggregate expression, rendered verbatim.

    This is safe ONLY because the loader proved that every `table.column` token
    in `expr` belongs to the metric's own grain table. Nothing here re-checks
    that, so nothing here may weaken it.

    `literal_column` rather than `text`: the two render the string identically,
    but a `TextClause` cannot be labelled (`text(...).label(...)` raises
    NotImplementedError on SQLAlchemy 2.0.52) and every use below needs a label
    — to read the value back by name, and to join the subquery's copy of it.
    """
    return literal_column(metric.expr)


def _metric_column(metric: Metric) -> ColumnElement[Any]:
    return _metric_expr(metric).label(metric.name)


def _key_is_nullable(metadata: MetaData, rp: ResolvedProperty) -> bool:
    """Can this group key ever be NULL? Three independent sources say yes.

    The DATABASE is authoritative and is checked first: a hand-written
    `nullable: false` over a genuinely nullable column is a wrong answer
    waiting to happen, and while the loader now refuses to load one, nothing
    downstream should have to trust that it did. A declaration may only ever
    ADD nullability here, never take it away.

    A `via` onto a `kind: left` join makes the property nullable however the
    column is defined — the outer join manufactures NULLs of its own for every
    unmatched row.
    """
    if rp.prop.nullable or _property_column(metadata, rp).nullable:
        return True
    join = rp.object.joins.get(rp.prop.via) if rp.prop.via else None
    return join is not None and join.kind == "left"


def _key_match(
    metadata: MetaData, outer: Column[Any], inner: ColumnElement[Any], rp: ResolvedProperty
) -> ColumnElement[bool]:
    """Match one outer group key against the metric subquery's copy of it.

    A nullable key needs `IS NOT DISTINCT FROM`: `country = country` is NULL,
    not true, when both sides are NULL, so every row whose key is NULL misses
    its own group — a silent undercount of exactly the kind this engine exists
    to make unreachable. A key that is provably NOT NULL uses plain equality,
    which more optimisers can hash.
    """
    if _key_is_nullable(metadata, rp):
        return outer.is_not_distinct_from(inner)
    return outer == inner


def _aggregate_then_join(
    stmt: Select[Any], metadata: MetaData, rq: ResolvedQuery, mp: MetricPlan
) -> Select[Any]:
    """Compute the metric at its own grain, then LEFT JOIN it back on the keys.

    The subquery always starts at the ROOT table and walks outward: a metric
    whose grain IS the root (`prefix == []`) is pre-aggregated over the root
    alone, which is reachable since Task 8 — a fanning hop DOWNSTREAM of the
    grain forces the rewrite even when nothing upstream of it fans.

    Only the prefix reaching the grain is applied, never the whole path.
    Applying the downstream fanning edges here would replicate the grain's rows
    inside the very subquery built to stop that happening — the same bug, one
    level down.

    `correlate(None)`: the subquery names the same tables as the enclosing
    query, and a silently correlated FROM element would turn an independent
    pre-aggregate into a per-outer-row one. Saying so explicitly costs nothing
    and removes the question.
    """
    keys = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    sub = select(*keys, _metric_column(mp.metric))
    sub = sub.select_from(metadata.tables[rq.root.primary]).correlate(None)
    sub = _apply_object_joins(sub, metadata, rq.root)
    prefix = path_to_table(rq, mp.metric.grain) or []
    sub = _apply_edges(sub, metadata, prefix, tag=mp.metric.name)
    sub = _apply_filters(sub, metadata, rq)
    if rq.group_by:
        sub = sub.group_by(*[_property_column(metadata, rp) for rp in rq.group_by])
    joined = sub.subquery(name=f"{mp.metric.name}_at_{mp.metric.grain}")

    matches = [
        _key_match(metadata, _property_column(metadata, rp), joined.c[rp.name], rp)
        for rp in rq.group_by
    ]
    # No keys means the subquery is a single row, so `ON true` is the whole
    # truth — and `and_()` with no clauses is deprecated, not empty-true.
    onclause = and_(*matches) if matches else true()
    return (
        stmt.join(joined, onclause, isouter=True)
        # The value is taken straight, NOT wrapped in coalesce(..., 0). The
        # outer key set is a subset of the subquery's — both start at the same
        # root under the same filters, and the outer's extra edges are inner
        # joins, which can only drop keys, never invent one. So a miss is not a
        # group with no facts; it is a key that failed to match a key it should
        # have matched, and the only way that happens is a broken comparison.
        # A zero there would repaint that as a plausible number and hide it;
        # NULL leaves it visible. (It would also be a type error the moment a
        # metric is not numeric.)
        .add_columns(joined.c[mp.metric.name].label(mp.metric.name))
        # The joined value is not an aggregate of the outer query, so it has to
        # be grouped. It is functionally determined by the keys already grouped
        # (one subquery row per key tuple), so this cannot split a group.
        .group_by(joined.c[mp.metric.name])
    )


def compile_query(rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData) -> Select[Any]:
    group_cols = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    inline = [mp for mp in plan.metric_plans if mp.strategy == "inline"]
    rewritten = [mp for mp in plan.metric_plans if mp.strategy == "aggregate_then_join"]
    inline_cols = [_metric_column(mp.metric) for mp in inline]

    if rewritten and not group_cols and not inline_cols:
        # Only rewritten metrics and no keys to join them back on: there is no
        # correct query to emit, so refuse rather than guess.
        first = rewritten[0]
        raise FanOutRefused(
            first.metric.name,
            first.metric.grain,
            first.forced_by or "<path>",
            [f"add a group_by key, or choose a metric at {rq.root.primary} grain"],
        )

    stmt = select(*group_cols, *inline_cols).select_from(metadata.tables[rq.root.primary])
    stmt = _apply_object_joins(stmt, metadata, rq.root)
    stmt = _apply_path(stmt, metadata, rq)
    stmt = _apply_filters(stmt, metadata, rq)

    if group_cols:
        stmt = stmt.group_by(*[_property_column(metadata, rp) for rp in rq.group_by])
    for mp in rewritten:
        stmt = _aggregate_then_join(stmt, metadata, rq, mp)

    return stmt.limit(rq.limit)
