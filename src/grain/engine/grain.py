"""The one thing the database cannot tell us: what one row means, and therefore
when an aggregation over it is safe. Decidable from declared cardinality alone —
no data is read here, which is why the verdict cannot drift as rows are added."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .errors import FanOutRefused
from .ontology import Metric
from .resolve import Edge, ResolvedQuery

Strategy = Literal["inline", "aggregate_then_join"]


@dataclass(frozen=True)
class MetricPlan:
    metric: Metric
    strategy: Strategy
    forced_by: str | None = None


@dataclass
class GrainPlan:
    metric_plans: list[MetricPlan] = field(default_factory=list)
    additive: bool = True
    non_additive_reason: str | None = None


def path_to_table(rq: ResolvedQuery, table: str) -> list[Edge] | None:
    """The shortest prefix of the walked path that brings `table` into scope.

    Returns [] when the table belongs to the root object, and None when the
    table is not reachable along the declared traversal at all.
    """
    if table in rq.root.tables:
        return []
    prefix: list[Edge] = []
    for edge in rq.path:
        prefix.append(edge)
        if table in edge.to_object.tables or table == edge.link.via:
            return list(prefix)
    return None


def _in_scope_metric_names(rq: ResolvedQuery) -> list[str]:
    return sorted(
        name
        for name, metric in rq.ontology.metrics.items()
        if path_to_table(rq, metric.grain) is not None
    )


def analyse(rq: ResolvedQuery) -> GrainPlan:
    plan = GrainPlan()

    for metric in rq.metrics:
        prefix = path_to_table(rq, metric.grain)
        if prefix is None:
            raise FanOutRefused(
                metric.name,
                metric.grain,
                "<not on path>",
                _in_scope_metric_names(rq),
            )

        fanning = [e for e in prefix if e.link.fans_out]
        if not fanning:
            plan.metric_plans.append(MetricPlan(metric=metric, strategy="inline"))
        else:
            plan.metric_plans.append(
                MetricPlan(
                    metric=metric,
                    strategy="aggregate_then_join",
                    forced_by=fanning[0].link.name,
                )
            )

    return plan
