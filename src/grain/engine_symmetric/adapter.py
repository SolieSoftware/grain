"""The symmetric engine, behind the `Engine` protocol."""
from __future__ import annotations

from sqlalchemy import MetaData

from ..engine.execute import Rewrite
from ..engine.ontology import Ontology
from ..engine.spec import QuerySpec
from ..plan import EnginePlan, register_engine
from .compile import compile_query
from .grain import GrainPlan, analyse
from .resolve import ResolvedQuery, resolve


def _rewrites(plan: GrainPlan, ontology: Ontology) -> list[Rewrite]:
    """A symmetric metric IS a rewrite: the emitted aggregate is not the one the
    ontology declares. Reported for the same reason the subquery engine reports
    its own — a caller that wants to know the engine changed the query should
    not have to diff the SQL to find out."""
    return [
        Rewrite(
            metric=mp.metric.name,
            strategy=mp.strategy,
            forced_by=mp.forced_by,
            reason=(
                f"{mp.forced_by} is "
                f"{ontology.links[mp.forced_by].effective_cardinality}, so "
                f"'{mp.metric.name}' is computed as a symmetric aggregate keyed "
                f"on {mp.metric.grain}"
            ),
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


class SymmetricEngine:
    """Computes fan-out-correct aggregates inline, in a single pass."""

    def plan(
        self, spec: QuerySpec, ontology: Ontology, metadata: MetaData
    ) -> EnginePlan:
        rq = resolve(spec, ontology)
        plan = analyse(rq, metadata)
        stmt = compile_query(rq, plan, metadata)
        return EnginePlan(
            stmt=stmt,
            rewrites=_rewrites(plan, ontology),
            additive=plan.additive,
            non_additive_reason=plan.non_additive_reason,
            ontology_elements_used=_elements_used(rq),
            limit=rq.limit,
        )


register_engine("symmetric", SymmetricEngine())
