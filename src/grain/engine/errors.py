"""Typed failures. Every error names a legal next move — an error that only
says 'no' costs the caller a turn and teaches it nothing."""
from __future__ import annotations


class GrainError(Exception):
    """Base for every grain failure."""

    def __init__(self, message: str, alternatives: list[str] | None = None) -> None:
        super().__init__(message)
        self.alternatives: list[str] = alternatives or []


class OntologyError(GrainError):
    """Raised at load time: the ontology does not match the database."""


class UnknownName(GrainError):
    def __init__(self, kind: str, name: str, suggestions: list[str]) -> None:
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"Unknown {kind} '{name}'.{hint}", suggestions)
        self.kind, self.name = kind, name


class NoPath(GrainError):
    def __init__(self, from_object: str, to_object: str, available: list[str]) -> None:
        hint = f" Links from {from_object}: {', '.join(available)}." if available else ""
        super().__init__(
            f"No declared link connects {from_object} to {to_object}.{hint}", available
        )
        self.from_object, self.to_object = from_object, to_object


class AmbiguousPath(GrainError):
    def __init__(self, from_object: str, to_object: str, candidates: list[str]) -> None:
        super().__init__(
            f"More than one link connects {from_object} to {to_object}: "
            f"{', '.join(candidates)}. Name the one you mean.",
            candidates,
        )
        self.from_object, self.to_object = from_object, to_object


class FanOutRefused(GrainError):
    def __init__(
        self, metric: str, grain: str, offending_edge: str, alternatives: list[str]
    ) -> None:
        hint = f" Alternatives: {', '.join(alternatives)}." if alternatives else ""
        super().__init__(
            f"Metric '{metric}' has grain '{grain}', but the query path fans out "
            f"through '{offending_edge}'. Summing it here would multiply each "
            f"'{grain}' row by its child count.{hint}",
            alternatives,
        )
        self.metric, self.grain, self.offending_edge = metric, grain, offending_edge


class GuardTripped(GrainError):
    def __init__(self, limit_name: str, limit_value: int) -> None:
        super().__init__(
            f"Guard '{limit_name}' exceeded (limit {limit_value}). "
            f"Narrow the request with filters or a smaller limit.",
            [],
        )
        self.limit_name, self.limit_value = limit_name, limit_value
