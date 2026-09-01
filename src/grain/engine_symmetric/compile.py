"""Compile a symmetric plan to one `Select`. No subquery, no rejoin.

The JOIN TREE is not duplicated. `Scope`, `_apply_object_joins`, `_apply_path`
and `_apply_filters` are imported from the subquery engine, because both engines
build FROM identically — the same objects, the same links, the same recursive
CTEs, the same `EXISTS` for a dotted filter. Copying that machinery would double
the most intricate code in the project for no differential benefit: it is the
RESOLVER and the AGGREGATE that this engine needs to reason about independently,
not the mechanics of emitting a join.

Note the duck-typing this relies on. Those helpers were written against
`engine.resolve`'s `Edge` and `ResolvedProperty`; this engine passes its own
copies, which are structurally identical but distinct classes. That works at
runtime and is deliberate, but it does mean the shared helpers quietly constrain
how far the copied resolver may drift — `test_resolver_parity` is what makes
that drift visible.

The whole difference from the subquery compiler is `_metric_column`.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, Select, select
from sqlalchemy.sql.elements import ColumnElement

from ..engine.compile import (
    Scope,
    _apply_filters,
    _apply_object_joins,
    _apply_path,
)
from ..engine.errors import GrainError
from ..engine.ontology import Metric
from .grain import GrainPlan
from .resolve import ResolvedQuery
from .symmetric import symmetric_expr


def _metric_column(
    metric: Metric, metadata: MetaData, strategy: str
) -> ColumnElement[Any]:
    """Plain for an immune or unfanned aggregate, encoded otherwise.

    There is no `aggregate_then_join` branch: this engine implements only the
    taxonomy, and the planner has already refused anything it cannot serve. That
    makes it a specialist rather than a superset, which is the point — an engine
    that silently fell back would make the differential harness meaningless.
    """
    if strategy == "inline":
        from sqlalchemy import literal_column

        return literal_column(metric.sql_expr).label(metric.name)
    return symmetric_expr(metric, metadata).label(metric.name)


def compile_query(
    rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData
) -> Select[Any]:
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
    metric_cols = [
        _metric_column(mp.metric, metadata, mp.strategy) for mp in plan.metric_plans
    ]

    if not group_cols and not metric_cols:
        raise GrainError(
            "A query must ask for at least one group_by key or metric.",
            ["add a metric", "add a group_by key"],
        )

    # No equivalent of the subquery engine's "rewritten metrics with no keys to
    # join on" refusal: nothing is joined back, so a metric with no group key is
    # simply one row over the whole population — which is a correct answer here,
    # and one that engine has to refuse.
    stmt = stmt.with_only_columns(*group_cols, *metric_cols)
    if group_cols:
        stmt = stmt.group_by(*[scope.column(rp) for rp in rq.group_by])

    orderable: dict[str, ColumnElement[Any]] = {
        rp.name: col for rp, col in zip(rq.group_by, group_cols)
    }
    orderable.update(
        {mp.metric.name: col for mp, col in zip(plan.metric_plans, metric_cols)}
    )
    if rq.order_by:
        # `resolve` has already refused any key the query does not emit, so
        # every lookup here hits.
        stmt = stmt.order_by(
            *[
                orderable[ob.key].desc() if ob.desc else orderable[ob.key].asc()
                for ob in rq.order_by
            ]
        )

    # `None` means "no LIMIT clause at all", not "limit to nothing".
    return stmt if rq.limit is None else stmt.limit(rq.limit)
