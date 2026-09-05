"""ResolvedQuery + GrainPlan -> SQLAlchemy Select. This module decides nothing;
every verdict was reached in grain.py. It only emits what was decided.

One idea runs through the whole file: a column reference is not enough to say
which ROW is meant. `employee.last_name` is one column, but a query that walks
`Employee_Manager` has `employee` in scope twice — once as the employee, once as
the ancestor — and those are different rows with the same column. So every
position on the walked path gets its own FROM element, and a property is read off
the element for ITS position. That is what `Scope` below is, and it is what makes
a qualified group key (`Employee_Manager.last_name`) mean the manager rather than
silently meaning the employee again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.sql.selectable import CTE, FromClause

from .errors import FanOutRefused, GrainError
from .grain import GrainPlan, MetricPlan
from .ontology import ColumnRef, JoinPair, Metric, ObjectType
from .resolve import Edge, ResolvedFilter, ResolvedProperty, ResolvedQuery

# Bookkeeping columns on a recursive CTE. Double-underscored so a real column of
# the traversed table can never collide with one: the CTE re-exposes every column
# of that table under its own name, and a table with a `depth` or `path` column
# would otherwise be shadowed silently.
_START = "__grain_start"
_DEPTH = "__grain_depth"
_PATH = "__grain_path"


def sql_text(stmt: Select[Any]) -> str:
    """Render SQL with literal binds — for logging, provenance and tests."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _column(metadata: MetaData, table: str, column: str) -> ColumnElement[Any]:
    return metadata.tables[table].columns[column]


@dataclass
class Scope:
    """Which FROM element holds each table, at each position on the path.

    Keyed by the same `edge_index` a `ResolvedProperty` carries: `None` for the
    root, `i` for the object hop `i` lands on. The value is whatever actually
    landed in FROM there — the real `Table`, an `Alias` of it, or a recursive
    `CTE` that re-exposes its columns.

    A table is ALIASED only when that name is already in scope. Two consequences,
    both wanted: a query that never revisits a table compiles to byte-identical
    SQL to before this class existed (which is why the provenance guarantee —
    same spec, same bytes — survives), and a query that does revisit one can no
    longer put the same table in FROM twice and let the database guess.

    An alias is named for the HOP that brought it in (`department_hop1`), not for
    a running counter. Both are deterministic, which is what the provenance
    guarantee needs — planning reads only the spec and the ontology, never data
    or the clock — but only one is readable in a compiled statement a human is
    expected to check. Uniqueness holds because one position registers each table
    name at most once.
    """

    metadata: MetaData
    positions: dict[int | None, dict[str, FromClause]] = field(default_factory=dict)
    registered: list[str] = field(default_factory=list)

    def register(self, index: int | None, table: str, element: FromClause) -> None:
        self.positions.setdefault(index, {})[table] = element
        self.registered.append(table)

    def element_for(self, table: str, index: int | None) -> FromClause:
        """A FROM element for `table`, aliased if that name is already in scope."""
        real = self.metadata.tables[table]
        if table not in self.registered:
            return real
        return real.alias(f"{table}_hop{(index or 0) + 1}")

    def latest(self, table: str) -> FromClause:
        """The most recently registered element for `table`.

        Join conditions read their `from` side through this: that side always
        belongs to a position already applied, and the most recent one is the row
        the current hop departs from. For a self-referential link both sides name
        the same table, and this is what keeps them apart.
        """
        for tables in reversed(list(self.positions.values())):
            if table in tables:
                return tables[table]
        return self.metadata.tables[table]

    def ref(self, ref: ColumnRef) -> ColumnElement[Any]:
        return self.latest(ref.table).columns[ref.column]

    def at(self, index: int | None, table: str) -> FromClause:
        element = self.positions.get(index, {}).get(table)
        if element is None:
            # Unreachable via the public API: `resolve` only produces an
            # edge_index for a link it found on the path, and every hop
            # registers its target before any column is read. Stated as an
            # error rather than a silent fallback to the real table, because
            # that fallback is precisely the wrong-row bug this class removes.
            raise GrainError(
                f"internal: table '{table}' is not in scope at position {index}"
            )
        return element

    def column(self, rp: ResolvedProperty) -> ColumnElement[Any]:
        return self.at(rp.edge_index, rp.prop.column.table).columns[rp.prop.column.column]


def _filter_clause(
    scope: Scope, rf: ResolvedFilter, col: ColumnElement[Any] | None = None
) -> ColumnElement[bool]:
    """`col` overrides which column the comparison binds to — needed when the
    filter's table has been aliased inside an EXISTS."""
    col = scope.column(rf.property) if col is None else col
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


def _ancestor_cte(scope: Scope, edge: Edge, suffix: str) -> CTE:
    """A recursive link becomes a depth-bounded walk to the ANCESTORS of each row.

    One row per (starting row, ancestor) pair, carrying the ancestor's own
    columns plus `__grain_start` — the key of the row the walk began at — so the
    outer query can join each row to its own ancestors.

    This replaced a CTE that enumerated the hierarchy from its ROOTS downward and
    then joined each row to ITS OWN row in it (`ON employee.employee_id =
    cte.employee_id`). That construction could not express anything: it added no
    column a caller could name, and its only real effect was to silently drop
    every row not reachable from a root within `max_depth` — invisible on
    chinook, whose 8 employees form one connected tree, and a silent population
    loss on any data with orphans (defect I3).

    `__grain_depth` bounds runaway recursion (`edge.link.max_depth`);
    `__grain_path` carries every ancestor visited so a cycle in the DATA
    terminates the walk instead of looping forever. The two guard different
    failure modes: `max_depth` alone still lets a two-node cycle spin until the
    bound is hit; `path` alone still needs a floor for data that has no cycle but
    is simply deep.

    `max_depth == 1` yields the direct parent only, and `LinkType.
    effective_cardinality` reports the declared (non-fanning) cardinality for
    exactly that case — so `Hop(link=..., max_depth=1)` is how a caller asks for
    "the manager" rather than "the management chain".

    An orphan — a row whose `from` column is NULL — has no ancestors and so no
    rows here, and the join in `_apply_edge` is inner. That drops it, which is
    what EVERY many_to_one hop in this engine does (`Customer_SupportRep` drops a
    customer with no rep in the same way). Consistent, and published as a rule
    rather than left to be discovered.

    `suffix` disambiguates the CTE's name. Every CTE anywhere in a statement —
    including one built inside a metric subquery — is hoisted into the SAME
    top-level `WITH` list, so two distinct CTE objects sharing a name make
    Postgres reject the statement outright. The caller therefore owns
    uniqueness: `_apply_edges` threads its own position plus a per-call-site tag
    through, so the outer path's CTE and a metric subquery's CTE for the very
    same link can never collide.
    """
    table = scope.metadata.tables[edge.to_object.primary]
    pair = edge.link.on[0]
    child = pair.from_.column  # e.g. employee.reports_to
    key = pair.to.column  # e.g. employee.employee_id

    # Both sides of a recursive link's `on` name the same table, so the walk's
    # two ends are aliased apart. Without this every column reference binds to
    # one row and "who is above me" compiles to "who am I".
    start = table.alias(f"start{suffix}")
    parent = table.alias(f"parent{suffix}")
    columns = [c.name for c in table.columns]

    base = (
        select(
            start.columns[key].label(_START),
            *[parent.columns[name].label(name) for name in columns],
            literal(1).label(_DEPTH),
            array((parent.columns[key],)).label(_PATH),
        )
        .select_from(start.join(parent, start.columns[child] == parent.columns[key]))
        .cte(name=f"{edge.link.name.lower()}_cte{suffix}", recursive=True)
    )
    higher = table.alias(f"higher{suffix}")
    step = (
        select(
            base.c[_START],
            *[higher.columns[name].label(name) for name in columns],
            (base.c[_DEPTH] + 1).label(_DEPTH),
            (base.c[_PATH] + array((higher.columns[key],))).label(_PATH),
        )
        # The CTE row IS an ancestor, so climbing means following that
        # ancestor's own `child` column upward.
        .select_from(base.join(higher, base.c[child] == higher.columns[key]))
        .where(
            and_(
                base.c[_DEPTH] < edge.link.max_depth,
                ~base.c[_PATH].any(higher.columns[key]),
            )
        )
    )
    return base.union_all(step)


def _apply_object_joins(
    stmt: Select[Any],
    scope: Scope,
    index: int | None,
    obj: ObjectType,
    primary: FromClause,
) -> Select[Any]:
    """Join the extra tables ONE object spans, off the element that object
    actually landed on at this position.

    `primary` matters: for a recursive hop it is the CTE, so an ancestor's
    spanned tables are joined to the ANCESTOR's row rather than re-reading the
    starting row's. That is why this is now called for a recursive edge, where it
    used to be skipped.

    Kind is honoured (`left` by default) — an inner join to `genre` would
    silently drop every track without one. The loader guarantees no object join
    fans out (defect C5), so none of these can replicate the object's rows.
    """
    for join in obj.joins.values():
        spanned = scope.element_for(join.to, index)
        onclause = and_(
            *[
                (
                    primary.columns[p.from_.column]
                    if p.from_.table == obj.primary
                    else scope.ref(p.from_)
                )
                == spanned.columns[p.to.column]
                for p in join.on
            ]
        )
        stmt = stmt.join(spanned, onclause, isouter=(join.kind == "left"))
        scope.register(index, join.to, spanned)
    return stmt


def _apply_edge(
    stmt: Select[Any], scope: Scope, index: int, edge: Edge, cte_suffix: str = ""
) -> Select[Any]:
    """Apply one edge's join(s), then that target's own extra table joins —
    but only once the target has actually landed in FROM.

    `direct` joins the target table directly; `through` joins the junction
    table and then the target, so the junction never reaches the caller.
    `recursive` joins the ancestor CTE. All three bring `edge.to_object` into
    scope at `index`, and all three then apply its object joins against the
    element it landed on.
    """
    if edge.link.kind == "direct":
        target = scope.element_for(edge.to_object.primary, index)
        stmt = stmt.join(
            target,
            and_(
                *[
                    scope.ref(p.from_) == target.columns[p.to.column]
                    for p in edge.link.on
                ]
            ),
        )
        scope.register(index, edge.to_object.primary, target)
        stmt = _apply_object_joins(stmt, scope, index, edge.to_object, target)
    elif edge.link.kind == "through":
        via = scope.element_for(edge.link.via, index)
        stmt = stmt.join(
            via,
            and_(
                *[
                    scope.ref(p.from_) == via.columns[p.to.column]
                    for p in edge.link.on_from
                ]
            ),
        )
        scope.register(index, edge.link.via, via)
        target = scope.element_for(edge.to_object.primary, index)
        stmt = stmt.join(
            target,
            and_(
                *[
                    via.columns[p.from_.column] == target.columns[p.to.column]
                    for p in edge.link.on_to
                ]
            ),
        )
        scope.register(index, edge.to_object.primary, target)
        stmt = _apply_object_joins(stmt, scope, index, edge.to_object, target)
    elif edge.link.kind == "recursive":
        pair = edge.link.on[0]
        # Read the departure row BEFORE registering the CTE: for a
        # self-referential link both name the same table, and after
        # registration `latest` would return the CTE itself.
        departure = scope.ref(pair.to)
        cte = _ancestor_cte(scope, edge, cte_suffix)
        stmt = stmt.join(cte, cte.c[_START] == departure)
        scope.register(index, edge.to_object.primary, cte)
        stmt = _apply_object_joins(stmt, scope, index, edge.to_object, cte)
    return stmt


def _apply_edges(
    stmt: Select[Any], scope: Scope, edges: list[Edge], tag: str = ""
) -> Select[Any]:
    """Apply a specific list of edges. The metric subquery passes only the prefix
    it needs; the outer query passes the whole path — same helper either way, so
    a `through`/`recursive` edge can't be dropped from one caller and not the
    other.

    The position index passed to `_apply_edge` is the edge's index in the FULL
    path, which is what a `ResolvedProperty.edge_index` refers to. A subquery
    applying a prefix therefore registers its tables under the same indices as
    the outer query, and a qualified group key resolves to the same object in
    both.

    `tag` names the call site (the metric name, for a metric subquery) and the
    position index distinguishes repeats of one link within a single list — a
    self-referential link may legally be traversed twice. Together they give
    every recursive CTE in the finished statement a unique name.
    """
    for index, edge in enumerate(edges):
        suffix = f"_{tag}_{index}" if tag else f"_{index}"
        stmt = _apply_edge(stmt, scope, index, edge, suffix)
    return stmt


def _apply_path(stmt: Select[Any], scope: Scope, rq: ResolvedQuery) -> Select[Any]:
    """Apply every edge on the walked path, plus each target's object joins."""
    return _apply_edges(stmt, scope, rq.path)


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

    The correlated side is read from `metadata` rather than from a `Scope`
    because a dotted filter is always anchored on the ROOT object (see
    `resolve._resolve_filter`), and the root's own tables are the one thing
    `Scope` never aliases — they are registered first, so nothing collides with
    them.

    A SELF-REFERENTIAL link needs more than that. Its `from` and `to` name the
    SAME table, so the correlated side and the target are one object and the
    EXISTS is left with no FROM at all — every column reference in it binds to
    the outer row, and `employees whose manager is named X` compiles to
    `employees who are their own manager`. Wrong rows, no error, no alias to
    tell them apart. So the target is aliased whenever it is also the
    correlated table, giving the inner row an identity of its own.

    The subquery is built from the link's target OBJECT — `object.primary` — and
    never from the table the filtered property happens to sit on. Those differ
    whenever the property is reached through one of the object's own joins
    (`Track.genre` lives on `genre`, not on `track`), and taking the property's
    table as the target put the link's own join columns in a subquery whose FROM
    lacked the table they name. SQLAlchemy then added it implicitly and
    unjoined: `EXISTS (SELECT 1 FROM genre, track WHERE album.album_id =
    track.album_id AND genre.name = 'Jazz')` — a cartesian product meaning
    "this album has any track, and some genre called Jazz exists anywhere",
    which returned 347 of 347 albums for a filter 13 should match (defect C3).
    Four specs in the shipped chinook ontology hit it. The `via` join is now
    applied INSIDE the subquery and the predicate binds to the joined column, so
    the filter constrains the row it names. `-W error::SAWarning` in
    pyproject.toml makes a recurrence a test failure rather than a warning
    nobody reads.

    NOTE what this does NOT do: a dotted filter is one hop, even on a recursive
    link. `Employee_Manager.last_name = 'Adams'` means "reports directly to
    Adams", not "is anywhere below Adams" — the transitive question is asked by
    traversing the link and grouping or filtering on the ancestor. Published as a
    rule in `describe()`, since the two readings differ by a lot.
    """
    link = rf.hops[0]
    target_obj = rf.property.object
    primary = metadata.tables[target_obj.primary]
    outer = {p.from_.table for p in (link.on_from if link.kind == "through" else link.on)}
    inner = primary.alias() if primary.name in outer else primary

    def _to(ref: ColumnRef) -> ColumnElement[Any]:
        """The INNER side of a join pair, read off the alias where there is one.

        The mapping is positional, not by table name: for a self-referential
        link both sides name the target, and only the `to` side belongs to the
        inner row. Aliasing by name would alias the correlated side too and
        correlate the EXISTS to nothing.
        """
        if ref.table == primary.name:
            return inner.columns[ref.column]
        return _column(metadata, ref.table, ref.column)

    def _pairs(pairs: list[JoinPair]) -> list[ColumnElement[bool]]:
        return [_column(metadata, p.from_.table, p.from_.column) == _to(p.to) for p in pairs]

    # The property may live on a table the target object merely SPANS, reached
    # by one of that object's declared joins. That join belongs inside this
    # subquery: it is what connects the filtered column to the row being tested.
    via = rf.property.prop.via
    via_join = target_obj.joins[via] if via is not None else None
    spanned = metadata.tables[via_join.to].alias() if via_join is not None else None
    exists_scope = Scope(metadata)
    if spanned is not None and via_join is not None:
        spanned_on = and_(
            *[_to(p.from_) == spanned.columns[p.to.column] for p in via_join.on]
        )
        predicate = _filter_clause(
            exists_scope, rf, spanned.columns[rf.property.prop.column.column]
        )
    else:
        predicate = _filter_clause(
            exists_scope, rf, inner.columns[rf.property.prop.column.column]
        )

    if link.kind == "through":
        sub = (
            select(1)
            .select_from(metadata.tables[link.via])
            .join(inner, and_(*_pairs(link.on_to)))
        )
        where = and_(*_pairs(link.on_from), predicate)
    else:
        sub = select(1).select_from(inner)
        where = and_(*_pairs(link.on), predicate)
    if spanned is not None and via_join is not None:
        # Faithful to the declaration: an `is_null` filter on a left-joined
        # property means "reached this row, and it has no match", which an inner
        # join would make unsatisfiable.
        sub = sub.join(spanned, spanned_on, isouter=(via_join.kind == "left"))
    sub = sub.where(where)
    return sub.correlate(*[metadata.tables[name] for name in sorted(outer)]).exists()


def _apply_filters(
    stmt: Select[Any], scope: Scope, metadata: MetaData, rq: ResolvedQuery
) -> Select[Any]:
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
        stmt = stmt.where(and_(*[_filter_clause(scope, rf) for rf in plain]))
    for rf in (rf for rf in rq.filters if rf.hops):
        stmt = stmt.where(_exists_clause(metadata, rf))
    return stmt


def _metric_expr(metric: Metric) -> ColumnElement[Any]:
    """The metric's aggregate expression, rendered verbatim.

    This is safe ONLY because the loader proved that every `table.column` token
    in `expr` belongs to the metric's own grain table. Nothing here re-checks
    that, so nothing here may weaken it.

    It is also why a metric's grain table is never read through `Scope`: the
    expression is raw SQL naming a real table, so it binds to the first,
    un-aliased occurrence of that table. `path_to_table` finds the first
    occurrence too, so the two agree. A metric whose grain is only in scope
    under an alias is therefore not expressible, which is correct — its rows
    would be a different row set than the name suggests.

    `sql_expr` rather than `expr`: a metric may be declared structurally
    (`agg` + `value`), and this function is deliberately indifferent to which.
    The loader validates the tokens of whichever form was declared, so the
    guarantee above is unchanged either way.

    `literal_column` rather than `text`: the two render the string identically,
    but a `TextClause` cannot be labelled (`text(...).label(...)` raises
    NotImplementedError on SQLAlchemy 2.0.52) and every use below needs a label
    — to read the value back by name, and to join the subquery's copy of it.
    """
    return literal_column(metric.sql_expr)


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

    Read from `metadata` rather than from the `Scope` element on purpose: this
    asks about the PHYSICAL column, which an alias or a CTE wrapper does not
    change, and a CTE column reports no useful nullability of its own.
    """
    physical = metadata.tables[rp.prop.column.table].columns[rp.prop.column.column]
    if rp.prop.nullable or physical.nullable:
        return True
    join = rp.object.joins.get(rp.prop.via) if rp.prop.via else None
    return join is not None and join.kind == "left"


def _key_match(
    metadata: MetaData,
    outer: ColumnElement[Any],
    inner: ColumnElement[Any],
    rp: ResolvedProperty,
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



def _survives_downstream(
    metadata: MetaData, rq: ResolvedQuery, skipped: list[Edge], tag: str
) -> ColumnElement[bool] | None:
    """Restrict a pre-aggregate to grain rows the SKIPPED edges would have kept.

    `_aggregate_then_join` walks only far enough to reach the metric's grain,
    and deliberately not the fanning edges beyond it: walking those would
    replicate the grain's own rows inside the very subquery built to stop that
    (defect C5). Correct, and incomplete — not walking an edge also means not
    FILTERING by it.

    The edges are inner joins, so they restrict the population. `Track ->
    Track_InvoiceLines` means tracks that actually sold; the pre-aggregate,
    seeing none of it, computed over all 3503 tracks including the 1519 that
    never sold. Measured before this fix: 54 of 1888 name-groups wrong, one
    returning 200620 for a true 356284.

    EXISTS is the tool that filters WITHOUT replicating -- the same reason
    `_exists_clause` uses it for a dotted filter. A semi-join keeps a grain row
    if the chain matches at least once, however many times it matches, so the
    C5 property the skip was protecting is preserved exactly.

    Returns None when nothing was skipped, so the common case adds no clause.
    """
    if not skipped:
        return None

    # The chain departs from whatever object the last applied edge landed on --
    # the root when the prefix is empty.
    start_index = len(rq.path) - len(skipped)
    start_obj = rq.root if start_index == 0 else rq.path[start_index - 1].to_object
    start_table = metadata.tables[start_obj.primary]

    # The departure table is ALIASED inside the EXISTS and tied to the outer row
    # by primary key. Reusing the un-aliased table instead puts a second `track`
    # in the subquery's FROM, and `correlate()` cannot remove what `select_from`
    # put there -- the clause then reads "does ANY sold track exist", which is
    # true for every row and filters nothing. That was the first attempt.
    keys = list(start_table.primary_key.columns)
    if not keys:
        # Without a key there is no way to say "the same row", so the semi-join
        # cannot be built. Leaving the pre-aggregate unfiltered is the
        # pre-existing behaviour, which is wrong but no more wrong than before;
        # refusing here would break queries that work today.
        return None

    alias = start_table.alias(f"{tag}_start")
    inner = Scope(metadata)
    inner.register(None if start_index == 0 else start_index - 1,
                   start_obj.primary, alias)
    sub = select(literal_column("1")).select_from(alias)
    sub = _apply_edges(sub, inner, skipped, tag=tag)
    same_row = and_(*[alias.columns[k.name] == k for k in keys])
    return sub.where(same_row).exists()


def _aggregate_then_join(
    stmt: Select[Any],
    outer: Scope,
    metadata: MetaData,
    rq: ResolvedQuery,
    mp: MetricPlan,
) -> tuple[Select[Any], ColumnElement[Any]]:
    """Compute the metric at its own grain, then LEFT JOIN it back on the keys.

    The subquery always starts at the ROOT table and walks outward: a metric
    whose grain IS the root (`prefix == []`) is pre-aggregated over the root
    alone, which is reachable since Task 8 — a fanning hop DOWNSTREAM of the
    grain forces the rewrite even when nothing upstream of it fans.

    It applies `mp.subquery_edges` edges, which is the longer of "the prefix
    reaching the grain" and "far enough to carry every group key". Applying the
    downstream FANNING edges here would replicate the grain's rows inside the
    very subquery built to stop that happening — the same bug, one level down —
    so `grain.analyse` refuses the query outright when a key can only be reached
    across one (`KeyBeyondGrain`). Everything this function applies is therefore
    provably non-replicating for this metric.

    The subquery gets its OWN `Scope`: it is an independent FROM, so a table
    aliased there has nothing to do with the outer query's element for the same
    table. Positions are the full-path indices either way, so a qualified group
    key resolves to the same object in both.

    `correlate(None)`: the subquery names the same tables as the enclosing
    query, and a silently correlated FROM element would turn an independent
    pre-aggregate into a per-outer-row one. Saying so explicitly costs nothing
    and removes the question.
    """
    root_table = metadata.tables[rq.root.primary]
    inner = Scope(metadata)
    inner.register(None, rq.root.primary, root_table)

    # Columns are set at the end with `with_only_columns`: a qualified group key
    # cannot be resolved until the hop that brings it into scope has been
    # applied, so FROM has to be built first.
    sub = select(root_table).select_from(root_table).correlate(None)
    sub = _apply_object_joins(sub, inner, None, rq.root, root_table)
    sub = _apply_edges(sub, inner, rq.path[: mp.subquery_edges], tag=mp.metric.name)
    sub = _apply_filters(sub, inner, metadata, rq)
    survives = _survives_downstream(
        metadata, rq, rq.path[mp.subquery_edges:], tag=f"{mp.metric.name}_ds"
    )
    if survives is not None:
        sub = sub.where(survives)

    keys = [inner.column(rp).label(rp.name) for rp in rq.group_by]
    sub = sub.with_only_columns(*keys, _metric_column(mp.metric))
    if rq.group_by:
        sub = sub.group_by(*[inner.column(rp) for rp in rq.group_by])
    joined = sub.subquery(name=f"{mp.metric.name}_at_{mp.metric.grain}")

    matches = [
        _key_match(metadata, outer.column(rp), joined.c[rp.name], rp)
        for rp in rq.group_by
    ]
    # No keys means the subquery is a single row, so `ON true` is the whole
    # truth — and `and_()` with no clauses is deprecated, not empty-true.
    onclause = and_(*matches) if matches else true()
    # The labelled column is handed back so `compile_query` can ORDER BY this
    # metric: it lives on the joined subquery, not in the outer FROM, so the
    # caller cannot reconstruct it from the metric alone.
    value = joined.c[mp.metric.name].label(mp.metric.name)
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
        .add_columns(value)
        # The joined value is not an aggregate of the outer query, so it has to
        # be grouped. It is functionally determined by the keys already grouped
        # (one subquery row per key tuple), so this cannot split a group.
        .group_by(joined.c[mp.metric.name])
    ), value


def compile_query(rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData) -> Select[Any]:
    root_table = metadata.tables[rq.root.primary]
    scope = Scope(metadata)
    scope.register(None, rq.root.primary, root_table)

    # FROM first, columns second: a qualified group key names a traversed
    # object, so it cannot be resolved before its hop has been applied.
    stmt = select(root_table).select_from(root_table)
    stmt = _apply_object_joins(stmt, scope, None, rq.root, root_table)
    stmt = _apply_path(stmt, scope, rq)
    stmt = _apply_filters(stmt, scope, metadata, rq)

    group_cols = [scope.column(rp).label(rp.name) for rp in rq.group_by]
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
    if not group_cols and not inline_cols and not rewritten:
        raise GrainError(
            "A query must ask for at least one group_by key or metric.",
            ["add a metric", "add a group_by key"],
        )

    stmt = stmt.with_only_columns(*group_cols, *inline_cols)
    if group_cols:
        stmt = stmt.group_by(*[scope.column(rp) for rp in rq.group_by])

    # Every column this query emits, by the name the caller knows it by. Built
    # here rather than reconstructed later because a rewritten metric's column
    # belongs to a subquery only this function has a handle on.
    orderable: dict[str, ColumnElement[Any]] = {
        rp.name: col for rp, col in zip(rq.group_by, group_cols)
    }
    orderable.update({mp.metric.name: col for mp, col in zip(inline, inline_cols)})
    for mp in rewritten:
        stmt, value = _aggregate_then_join(stmt, scope, metadata, rq, mp)
        orderable[mp.metric.name] = value

    # `resolve` has already refused any key that is not emitted, so every lookup
    # here hits. Ordering happens AFTER the rewrite joins so that a rewritten
    # metric can be ordered by at all.
    if rq.order_by:
        stmt = stmt.order_by(
            *[
                orderable[ob.key].desc() if ob.desc else orderable[ob.key].asc()
                for ob in rq.order_by
            ]
        )

    # `None` means "no LIMIT clause at all", not "limit to nothing" -- calling
    # `.limit(None)` would coincidentally also emit no LIMIT in SQLAlchemy,
    # but the branch is written explicitly so that reading this function
    # doesn't require knowing that coincidence.
    return stmt if rq.limit is None else stmt.limit(rq.limit)
