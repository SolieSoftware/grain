"""The seam between the facade and an engine.

An engine owns everything from a `QuerySpec` to a compiled `Select`: its own
resolver, its own grain analysis, its own compiler. That is deliberate. A bug in
a SHARED resolver would be invisible to a differential test, because both
engines would inherit it and agree on the same wrong answer -- and two engines
cross-checking each other is worth more here than the duplication costs.

The consequence is that `ResolvedQuery` and `GrainPlan` are per-engine classes.
So nothing outside an engine may depend on them, and every engine hands back
this one flat, engine-agnostic value instead. `EnginePlan` is the entire
contract. If a field of it ever needs an engine-owned type, the seam has leaked
and the fix is to flatten the value, not to widen the import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import MetaData, Select

from .engine.execute import Rewrite
from .engine.ontology import Ontology
from .engine.spec import QuerySpec


@dataclass(frozen=True)
class EnginePlan:
    """One engine's complete answer to "what SQL, and what should the caller be
    told about it".

    `limit` is carried because `Result.limit_reached` needs the limit the query
    was actually planned with, which the caller may not have set itself -- it
    defaults to 100.
    """

    stmt: Select[Any]
    rewrites: list[Rewrite]
    additive: bool
    non_additive_reason: str | None
    ontology_elements_used: list[str]
    limit: int | None


class Engine(Protocol):
    """Implemented once per engine. The only method the facade calls."""

    def plan(
        self, spec: QuerySpec, ontology: Ontology, metadata: MetaData
    ) -> EnginePlan: ...


_ENGINES: dict[str, Engine] = {}


def register_engine(name: str, engine: Engine) -> None:
    """Registration is at import time, once per engine module.

    A duplicate name is refused rather than overwritten: two engines silently
    sharing a name would make `Result.engine` a lie, and comparing engines is
    the reason any of this exists.
    """
    if name in _ENGINES:
        raise ValueError(f"engine '{name}' is already registered")
    _ENGINES[name] = engine


def get_engine(name: str) -> Engine:
    try:
        return _ENGINES[name]
    except KeyError:
        legal = ", ".join(sorted(_ENGINES)) or "none registered"
        raise KeyError(f"unknown engine '{name}'. Legal engines: {legal}") from None


def engine_names() -> frozenset[str]:
    """A function, not a constant: the registry is populated by imports, so a
    module-level frozenset would capture whatever happened to be registered
    first and then quietly disagree with `get_engine`."""
    return frozenset(_ENGINES)
