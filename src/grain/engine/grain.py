"""The one thing the database cannot tell us: what one row means, and therefore
when an aggregation over it is safe. Decidable from declared cardinality alone —
no data is read here, which is why the verdict cannot drift as rows are added."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .errors import NOT_ON_PATH, FanOutRefused
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


def _hops_reaching(rq: ResolvedQuery, table: str) -> list[str]:
    """First hops of a declared route that would bring `table` into scope.

    A breadth-first walk of the declared links outward from every in-scope
    object, reporting the hop the caller would have to add first. Reads only
    what the links say — no rows are consulted to decide reachability.
    """
    onto = rq.ontology
    starts = {rq.root.name} | {edge.to_object.name for edge in rq.path}
    reaching: set[str] = set()
    for first in onto.links.values():
        if first.from_ not in starts:
            continue
        seen = {first.from_}
        frontier = [first]
        while frontier:
            link = frontier.pop(0)
            target = onto.objects.get(link.to)
            if target is None or link.to in seen:
                continue
            if table in target.tables or table == link.via:
                reaching.add(first.name)
                break
            seen.add(link.to)
            frontier.extend(onto.links_from(link.to))
    return sorted(reaching)


def _repairs_for(rq: ResolvedQuery, grain: str) -> list[str]:
    """Legal next moves when no metric at all is answerable in this scope.

    An error that only says 'no' costs the caller a turn and teaches it nothing,
    so this must not come back empty while any move exists.
    """
    repairs = [f"add the {name} hop" for name in _hops_reaching(rq, grain)]
    owner = rq.ontology.object_for_table(grain)
    if owner is not None and owner.name != rq.root.name:
        repairs.append(f"query {owner.name} instead")
    repairs += [f"remove the {edge.link.name} hop" for edge in rq.fanning_edges]
    return repairs


def analyse(rq: ResolvedQuery) -> GrainPlan:
    plan = GrainPlan()

    for metric in rq.metrics:
        prefix = path_to_table(rq, metric.grain)
        if prefix is None:
            repairs = _repairs_for(rq, metric.grain)
            in_scope = _in_scope_metric_names(rq)
            raise FanOutRefused(
                metric.name,
                metric.grain,
                NOT_ON_PATH,
                in_scope or repairs,
                reachable_via=repairs,
            )

        # Edges traversed AFTER the grain enters scope are what replicate its
        # rows. Edges before it replicate the grain's ancestors, which leaves
        # this metric's own rows intact and so is harmless for it.
        downstream = rq.path[len(prefix):]
        fanning = [e for e in downstream if e.link.fans_out]
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

        # Additivity is orthogonal to strategy and to fan-out: it asks whether
        # the *group-by keys* (which live on the root) overlap, not whether
        # this metric's own rows would double-count. A many_to_many anywhere
        # on the path from the grain back to the root — `prefix` — means the
        # same grain row can belong to more than one group, so the column
        # will not sum to the total even though every group is correct.
        for edge in prefix:
            if edge.link.cardinality == "many_to_many" and plan.additive:
                plan.additive = False
                plan.non_additive_reason = (
                    f"'{metric.name}' is grouped across '{edge.link.name}', which is "
                    f"many_to_many. Each group is correct, but the groups overlap — "
                    f"this column will not sum to the total."
                )

    return plan
