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


class GroupKeyNotOnPath(GrainError):
    """A qualified group key names a link the traversal does not walk.

    `group_by: ["Employee_Manager.last_name"]` asks for a column of the object
    that hop lands on, so the hop has to be in `traverse`. Naming the missing hop
    is the whole repair, and it is the one an agent can act on without a turn
    spent guessing.
    """

    def __init__(self, key: str, link: str, traversed: list[str]) -> None:
        alternatives = [f"add the {link} hop to traverse"]
        alternatives += [f"use {name}.<property> instead" for name in traversed]
        hint = f" Traversed: {', '.join(traversed)}." if traversed else ""
        super().__init__(
            f"Group key '{key}' names the link '{link}', which this query does not "
            f"traverse.{hint} Alternatives: {', '.join(alternatives)}.",
            alternatives,
        )
        self.key, self.link = key, link


class AmbiguousGroupKey(GrainError):
    """One link, walked twice, named once.

    A qualified key names a LINK, not a position, so when the same
    self-referential link appears at two hops there is no answer to which one it
    means. Guessing (first, last) would be a silent choice between two different
    numbers.
    """

    def __init__(self, key: str, link: str, positions: list[int]) -> None:
        alternatives = [
            f"traverse {link} once",
            "group by a property of the query's own object instead",
        ]
        hops = ", ".join(str(p + 1) for p in positions)
        super().__init__(
            f"Group key '{key}' names '{link}', which this query traverses "
            f"{len(positions)} times (hops {hops}). A qualified key names a link, "
            f"not a hop, so it cannot say which one is meant.",
            alternatives,
        )
        self.key, self.link, self.positions = key, link, positions


class KeyBeyondGrain(GrainError):
    """A pre-aggregated metric cannot carry a group key that lies past a fanning
    edge downstream of its own grain.

    Aggregate-then-join computes the metric at its grain in a subquery and joins
    it back on the group keys, so the subquery has to CARRY every key. When a key
    sits beyond the grain across a fanning edge, extending the subquery far
    enough to reach it replicates the metric's own rows inside the very subquery
    built to stop that — the same over-count, one level down.

    This is a refusal of the 'our mechanism cannot express this' kind rather than
    'the question is unanswerable', and is the class the symmetric-aggregates
    decision in the improvements note is waiting to be counted. Count these.
    """

    def __init__(self, metric: str, grain: str, key: str, edge: str) -> None:
        alternatives = [
            f"drop the '{key}' group key",
            f"use a metric whose grain reaches '{key}'",
            f"remove the {edge} hop",
        ]
        super().__init__(
            f"Metric '{metric}' is at '{grain}' grain and must be pre-aggregated "
            f"here, but the group key '{key}' lies beyond that grain across "
            f"'{edge}', which fans out. Carrying the key into the pre-aggregate "
            f"would replicate the metric's own rows. Alternatives: "
            f"{', '.join(alternatives)}.",
            alternatives,
        )
        self.metric, self.grain, self.key, self.edge = metric, grain, key, edge


class NonAdditiveRefused(GrainError):
    """A non-additive query with no correct rendering at all.

    Non-additivity is normally SURFACED rather than refused (decision R2): the
    per-group numbers are what the question asked for, and the flag says the
    total is not. That defence rests on one unstated condition — that a group is
    one row of the root object. Two ways it fails, and in both the engine itself
    does the wrong summing, so there is nothing left for a caller to obey:

    - No `group_by` at all. There is one group, it holds every root row, and the
      many_to_many replicates rows into it. `revenue` by nothing over
      `Playlist_Tracks` returned 5738.28 against a true 2328.60 (defect C1).
    - A `group_by` on no unique key. Two root rows sharing a key value merge,
      and rows reachable from both are counted twice inside that one group.
      Chinook's two playlists named 'Music' returned 4215.42 for exactly
      2107.71 of distinct-track revenue (defect C2).

    Aggregate-then-join does not rescue either case: the many_to_many is in the
    prefix reaching the grain, so the subquery over-counts identically.
    """

    def __init__(
        self, metric: str, grain: str, link: str, alternatives: list[str], reason: str
    ) -> None:
        hint = f" Alternatives: {', '.join(alternatives)}." if alternatives else ""
        super().__init__(
            f"Metric '{metric}' is reached through '{link}', which is many_to_many, "
            f"so one '{grain}' row belongs to more than one group. {reason}{hint}",
            alternatives,
        )
        self.metric, self.grain, self.link = metric, grain, link


class GuardTripped(GrainError):
    def __init__(
        self, limit_name: str, limit_value: int, alternatives: list[str] | None = None
    ) -> None:
        # `alternatives` was omitted here originally, which made this the one
        # place the invariant in this module's docstring -- every error names a
        # legal next move -- was knowingly false. The advice was in the message
        # but not in the machine-readable field a caller can act on.
        alternatives = alternatives or [
            "add a filter to narrow the population",
            "set a smaller limit",
            "add a group_by key so the database collapses rows before they are fetched",
        ]
        super().__init__(
            f"Guard '{limit_name}' exceeded (limit {limit_value}). "
            f"Narrow the request with filters or a smaller limit.",
            alternatives,
        )
        self.limit_name, self.limit_value = limit_name, limit_value
