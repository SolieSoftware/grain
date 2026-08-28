"""The existing engine, behind the `Engine` protocol.

This holds what used to live inline in `api.Grain._plan` and
`api.Grain._rewrites`. Moving it here is what lets the facade stop importing
`resolve`, `analyse` and `compile_query` directly -- those are THIS engine's
internals, and a second engine has its own.
"""
from __future__ import annotations

from sqlalchemy import MetaData

from ..plan import EnginePlan, register_engine
from .compile import compile_query
from .execute import Rewrite
from .grain import GrainPlan, analyse
from .ontology import Ontology
from .resolve import ResolvedQuery, resolve
from .spec import QuerySpec


def _rewrites(plan: GrainPlan, ontology: Ontology) -> list[Rewrite]:
    return [
        Rewrite(
            metric=mp.metric.name,
            strategy=mp.strategy,
            forced_by=mp.forced_by,
            reason=f"{mp.forced_by} is {ontology.links[mp.forced_by].cardinality}",
        )
        for mp in plan.metric_plans
        if mp.forced_by
    ]


def _elements_used(rq: ResolvedQuery) -> list[str]:
    return (
        [rq.root.name]
        + [edge.link.name for edge in rq.path]
        + [metric.name for metric in rq.metrics]
    )


class SubqueryEngine:
    """Pre-aggregates a fanned metric at its own grain and LEFT JOINs it back."""

    def plan(
        self, spec: QuerySpec, ontology: Ontology, metadata: MetaData
    ) -> EnginePlan:
        rq = resolve(spec, ontology)
        plan = analyse(rq)
        stmt = compile_query(rq, plan, metadata)
        return EnginePlan(
            stmt=stmt,
            rewrites=_rewrites(plan, ontology),
            additive=plan.additive,
            non_additive_reason=plan.non_additive_reason,
            ontology_elements_used=_elements_used(rq),
            limit=rq.limit,
        )


register_engine("subquery", SubqueryEngine())
