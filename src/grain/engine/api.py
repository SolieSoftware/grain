"""The library IS the product. Adapters (CLI, MCP, your own chat harness) are
thin wrappers over this class and add no logic of their own.

`Grain.load` takes a DIRECTORY, not a domain module -- `engine/` must never
import `domains/` by name, so the only thing this class is allowed to know
about a domain is the path to its `ontology.yaml`. That is also what keeps
this module usable with no MCP and no CLI present: nothing here reaches for
either."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, MetaData

from .compile import compile_query, sql_text
from .execute import Result, Rewrite, execute
from .grain import GrainPlan, analyse
from .guard import GuardConfig
from .loader import load_ontology
from .ontology import Ontology
from .resolve import ResolvedQuery, resolve
from .spec import QuerySpec


class Grain:
    """Ontology + reflected schema + connection, bound together. Every public
    method takes a `QuerySpec` and either returns a `Result` or raises one of
    the typed `GrainError`s -- there is no other way in or out."""

    def __init__(
        self,
        ontology: Ontology,
        metadata: MetaData,
        engine: Engine,
        guard: GuardConfig | None = None,
    ) -> None:
        self.ontology = ontology
        self.metadata = metadata
        self.engine = engine
        self.guard = guard or GuardConfig()

    @classmethod
    def load(
        cls, domain_dir: Path | str, engine: Engine, guard: GuardConfig | None = None
    ) -> "Grain":
        """`domain_dir` is a directory holding `ontology.yaml` -- a path, not
        a Python import. `src/grain/domains/chinook` happens to also be an
        importable package (it exports `CHINOOK_DIR` for tests), but nothing
        here relies on that; a domain directory that is not a package at all
        loads exactly the same way."""
        domain_dir = Path(domain_dir)
        metadata = MetaData()
        metadata.reflect(bind=engine)
        ontology = load_ontology(domain_dir / "ontology.yaml", metadata)
        return cls(ontology, metadata, engine, guard)

    def _plan(self, spec: QuerySpec) -> tuple[ResolvedQuery, GrainPlan, Any]:
        rq = resolve(spec, self.ontology)
        plan = analyse(rq)
        stmt = compile_query(rq, plan, self.metadata)
        return rq, plan, stmt

    def _rewrites(self, plan: GrainPlan) -> list[Rewrite]:
        return [
            Rewrite(
                metric=mp.metric.name,
                strategy=mp.strategy,
                forced_by=mp.forced_by,
                reason=f"{mp.forced_by} is {self.ontology.links[mp.forced_by].cardinality}",
            )
            for mp in plan.metric_plans
            if mp.forced_by
        ]

    def describe(self, object: str | None = None) -> dict[str, Any]:
        """How the agent learns the domain, in place of the DDL. See
        `describe.py` for why the non-additivity rule is stated once rather
        than enumerated per metric x dimension."""
        from .describe import describe as _describe

        return _describe(self.ontology, object)

    def explain(self, spec: QuerySpec) -> dict[str, Any]:
        """The compiled SQL and the plan's verdicts, without ever touching the
        database. Intentionally has no `rows` key -- there are none, and a
        caller that only checked truthiness of `out.get("rows")` could
        otherwise mistake an empty list for 'nothing to show' instead of
        'never asked'."""
        rq, plan, stmt = self._plan(spec)
        return {
            "compiled_sql": sql_text(stmt),
            "rewrites": [
                {
                    "metric": r.metric,
                    "strategy": r.strategy,
                    "forced_by": r.forced_by,
                    "reason": r.reason,
                }
                for r in self._rewrites(plan)
            ],
            "additive": plan.additive,
            "non_additive_reason": plan.non_additive_reason,
            "ontology_elements_used": self._ontology_elements_used(rq),
        }

    def query(self, spec: QuerySpec) -> Result:
        rq, plan, stmt = self._plan(spec)
        rows, columns = execute(self.engine, stmt, self.guard)
        return Result(
            rows=rows,
            columns=columns,
            compiled_sql=sql_text(stmt),
            rewrites=self._rewrites(plan),
            additive=plan.additive,
            non_additive_reason=plan.non_additive_reason,
            # Exactly `limit` rows means there may be more that were never
            # fetched. Reported rather than left for the caller to infer: the
            # inference requires knowing the limit the caller may not have set
            # itself (it defaults to 100).
            limit_reached=rq.limit is not None and len(rows) == rq.limit,
            ontology_elements_used=self._ontology_elements_used(rq),
        )

    @staticmethod
    def _ontology_elements_used(rq: ResolvedQuery) -> list[str]:
        return (
            [rq.root.name]
            + [edge.link.name for edge in rq.path]
            + [metric.name for metric in rq.metrics]
        )
