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


NOT_ON_PATH = "<not on path>"
"""Sentinel `offending_edge`: the grain is unreachable, so no edge is at fault."""


class FanOutRefused(GrainError):
    def __init__(
        self,
        metric: str,
        grain: str,
        offending_edge: str,
        alternatives: list[str],
        reachable_via: list[str] | None = None,
    ) -> None:
        hint = f" Alternatives: {', '.join(alternatives)}." if alternatives else ""
        if offending_edge == NOT_ON_PATH:
            # Nothing fanned out — the grain never enters scope at all, so the
            # repair is to extend the traversal, not to aggregate differently.
            reach = (
                f" Bring it into scope: {', '.join(reachable_via)}."
                if reachable_via
                else ""
            )
            # When `alternatives` fell back to the repairs there is nothing to
            # add — saying the same thing twice reads as two different offers.
            echo = "" if alternatives == (reachable_via or []) else hint
            message = (
                f"Metric '{metric}' has grain '{grain}', which is not reachable "
                f"along the declared traversal.{reach}{echo}"
            )
        else:
            message = (
                f"Metric '{metric}' has grain '{grain}', but the query path fans out "
                f"through '{offending_edge}'. Summing it here would multiply each "
                f"'{grain}' row by its child count.{hint}"
            )
        super().__init__(message, alternatives)
        self.metric, self.grain, self.offending_edge = metric, grain, offending_edge
        self.reachable_via: list[str] = reachable_via or []


class GuardTripped(GrainError):
    def __init__(self, limit_name: str, limit_value: int) -> None:
        super().__init__(
            f"Guard '{limit_name}' exceeded (limit {limit_value}). "
            f"Narrow the request with filters or a smaller limit.",
            [],
        )
        self.limit_name, self.limit_value = limit_name, limit_value
