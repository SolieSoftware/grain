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

from ..plan import EnginePlan, engine_names, get_engine
from ..engine_symmetric import adapter as _symmetric  # noqa: F401  -- registers "symmetric"
from . import adapter  # noqa: F401  -- import registers the "subquery" engine
from .compile import sql_text
from .execute import Result, execute
from .guard import GuardConfig
from .loader import load_ontology
from .ontology import Ontology
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
        engine_name: str = "subquery",
    ) -> None:
        self.ontology = ontology
        self.metadata = metadata
        self.engine = engine
        self.guard = guard or GuardConfig()
        # Held as a NAME, not an instance: the name is what a caller passes,
        # what the CLI flag carries, and what the Result reports back. Resolved
        # per call so a registry populated by a later import still works.
        if engine_name not in engine_names():
            get_engine(engine_name)  # raises, naming the legal engines
        self.engine_name = engine_name

    @classmethod
    def load(
        cls,
        domain_dir: Path | str,
        engine: Engine,
        guard: GuardConfig | None = None,
        engine_name: str = "subquery",
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
        return cls(ontology, metadata, engine, guard, engine_name)

    def _plan(self, spec: QuerySpec) -> EnginePlan:
        """Everything from the spec to the SQL belongs to the engine. This
        facade reads only `EnginePlan`, never an engine's own types -- each
        engine has its own resolver, so those types are not shared."""
        return get_engine(self.engine_name).plan(spec, self.ontology, self.metadata)

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
        ep = self._plan(spec)
        return {
            "engine": self.engine_name,
            "compiled_sql": sql_text(ep.stmt),
            "rewrites": [
                {
                    "metric": r.metric,
                    "strategy": r.strategy,
                    "forced_by": r.forced_by,
                    "reason": r.reason,
                }
                for r in ep.rewrites
            ],
            "additive": ep.additive,
            "non_additive_reason": ep.non_additive_reason,
            "ontology_elements_used": ep.ontology_elements_used,
        }

    def query(self, spec: QuerySpec) -> Result:
        ep = self._plan(spec)
        rows, columns = execute(self.engine, ep.stmt, self.guard)
        return Result(
            rows=rows,
            columns=columns,
            compiled_sql=sql_text(ep.stmt),
            rewrites=ep.rewrites,
            additive=ep.additive,
            non_additive_reason=ep.non_additive_reason,
            # Exactly `limit` rows means there may be more that were never
            # fetched. Reported rather than left for the caller to infer: the
            # inference requires knowing the limit the caller may not have set
            # itself (it defaults to 100).
            limit_reached=ep.limit is not None and len(rows) == ep.limit,
            ontology_elements_used=ep.ontology_elements_used,
            engine=self.engine_name,
        )
