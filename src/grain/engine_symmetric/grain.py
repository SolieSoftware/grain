"""Grain analysis for the symmetric engine.

Same question as `engine/grain.py` — what does one row mean, and when is an
aggregation over it safe — answered for an engine that computes fan-out-correct
aggregates inline instead of pre-aggregating them in a subquery. No data is read
here, so no verdict can drift as rows are added.

THREE DIFFERENCES from the subquery engine's analysis, each a consequence of
having no subquery:

1. `Strategy` is `inline | symmetric`. There is no `aggregate_then_join`, so
   there is no `subquery_edges` either: nothing walks a prefix.

2. `KeyBeyondGrain` does not exist here. It protects a pre-aggregating subquery
   from having to walk across a fan to reach a group key. Nothing pre-aggregates,
   so there is nothing to protect — this is a stated deliverable of the engine.
   The subquery engine still raises it, correctly, and a test pins that.

3. `NonAdditiveRefused` does not fire. That refusal exists because a group with
   no unique key merges two root rows and the ENGINE ITSELF then double-counts
   everything reachable from both (defects C1 and C2). The symmetric encoding
   dedupes by the grain's primary key, so each grain row is counted exactly once
   per group whether or not the group key is unique. The per-group number is
   correct without the guarantee that refusal was buying, so refusing would deny
   an answer the engine can now give. The groups may still OVERLAP, and that is
   still reported — see `_overlap`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import MetaData

from ..engine.errors import NOT_ON_PATH, FanOutRefused
from ..engine.ontology import Metric
from .resolve import Edge, ResolvedProperty, ResolvedQuery
from .symmetric import require_eligible

Strategy = Literal["inline", "symmetric"]


@dataclass(frozen=True)
class PrefixOverlap:
    """A many_to_many edge on the metric's own prefix."""

    link_name: str
    cardinality: str
    grain: str


@dataclass(frozen=True)
class GroupKeyOverlap:
    """A group key reached through a fanning link. Read from `_overlap`, which
    follows the path to the KEY rather than to the grain — a capability the
    subquery engine does not have."""

    key_name: str
    link_name: str
    cardinality: str
    grain: str


@dataclass(frozen=True)
class MetricPlan:
    metric: Metric
    strategy: Strategy
    forced_by: str | None = None
    # The facts the additivity verdict rests on. Stored so the verdict is a
    # function of the finished plan rather than a variable accumulated beside
    # it — see docs/plans/2026-09-06-derived-additivity-design.md. A deliberate
    # copy of the subquery engine's shape, not shared code: `resolve.py` here is
    # already an intentional duplicate, because a shared implementation makes
    # the differential harness blind — both engines inherit one bug and agree.
    prefix_overlap: PrefixOverlap | None = None
    group_key_overlap: GroupKeyOverlap | None = None

    # Derived, not stored. A stored verdict can disagree with the decisions it
    # describes, and three Criticals in this project are exactly that
    # disagreement. A property cannot be set at all.
    @property
    def additive(self) -> bool:
        return _additivity(self)[0]

    @property
    def non_additive_reason(self) -> str | None:
        return _additivity(self)[1]


# The fields the verdict is derived from. Adding a field that can affect whether
# a column sums to the total means adding it here AND handling it in
# `_additivity`; the census test refuses an unclassified field, and the AST check
# refuses a read this set does not list.
#
# `metric` is an input because `_additivity` dereferences it — it supplies the
# LABEL every reason string names, and nothing else about the metric reaches the
# boolean. Listing it as irrelevant would be a false claim the AST check catches.
_ADDITIVITY_INPUTS = frozenset({"prefix_overlap", "group_key_overlap", "metric"})

# Fields that provably cannot change the verdict, each with the reason. The
# reason is the record of the judgement — see the census test.
IRRELEVANT_TO_ADDITIVITY: dict[str, str] = {
    "strategy": (
        "inline versus symmetric changes the encoding, and both count every "
        "grain row once per group"
    ),
    "forced_by": (
        "names the link that forced the encoding, which is a strategy fact and "
        "not a summability one"
    ),
}


def _additivity(plan: MetricPlan) -> tuple[bool, str | None]:
    """This engine's verdict, derived from the finished plan. Never raises.

    NEVER RAISES for the same reason the subquery engine's does not: refusing is
    a decision and belongs in `analyse`. Here nothing even tempts it — neither
    condition calls a validator, because this engine has no `NonAdditiveRefused`
    (see the module docstring, difference 3).

    TWO conditions, not the subquery engine's three, and the second is NOT its
    `separated`: `_overlap` follows the path to the GROUP KEY rather than to the
    grain, which `engine/grain.py` records as a capability the subquery engine
    lacks. That difference is deliberate — do not bring it to parity.

    There is deliberately no window condition: this engine refuses a stock with
    `MetricNotSymmetric`, so a level never reaches a plan here. Stated rather
    than omitted — a missing condition looks identical to an unconsidered one,
    and that resemblance is what this derivation exists to remove.

    Precedence, not accumulation: the prefix condition returns first, matching
    the `if additive and overlap is not None` guard this replaced.
    """
    if plan.prefix_overlap is not None:
        o = plan.prefix_overlap
        return False, (
            f"'{plan.metric.name}' is grouped across '{o.link_name}', which "
            f"is {o.cardinality}. Each group is correct "
            f"— the encoding counts every '{o.grain}' row once per "
            f"group — but one row belongs to several groups, so this "
            f"column will not sum to the total."
        )
    if plan.group_key_overlap is not None:
        g = plan.group_key_overlap
        return False, (
            f"'{plan.metric.name}' is grouped by '{g.key_name}', which is reached "
            f"through '{g.link_name}' ({g.cardinality}). "
            f"Each group is correct, but one '{g.grain}' row belongs to "
            f"every group it can reach, so this column will not sum to the "
            f"total."
        )
    return True, None


@dataclass
class GrainPlan:
    metric_plans: list[MetricPlan] = field(default_factory=list)

    @property
    def additive(self) -> bool:
        """False if ANY requested metric is non-additive."""
        return all(mp.additive for mp in self.metric_plans)

    @property
    def non_additive_reason(self) -> str | None:
        return next(
            (mp.non_additive_reason for mp in self.metric_plans if mp.non_additive_reason),
            None,
        )


def path_to_table(rq: ResolvedQuery, table: str) -> list[Edge] | None:
    """The shortest prefix of the walked path that brings `table` into scope.

    `[]` means the root's own grain; `None` means unreachable. Identical to the
    subquery engine's, and sound for the same reason: the loader refuses a
    fanning `TableJoin`, so an object's spanned tables really are all at the
    object's grain.
    """
    if table in rq.root.tables:
        return []
    for i, edge in enumerate(rq.path):
        if table in edge.to_object.tables:
            return rq.path[: i + 1]
    return None


def _in_scope_metric_names(rq: ResolvedQuery) -> list[str]:
    reachable = set(rq.root.tables)
    for edge in rq.path:
        reachable |= set(edge.to_object.tables)
    return sorted(
        name for name, m in rq.ontology.metrics.items() if m.grain in reachable
    )


def _repairs_for(rq: ResolvedQuery, grain: str) -> list[str]:
    obj = rq.ontology.object_for_table(grain)
    return [f"query {obj.name} instead"] if obj else []


def _overlap(rq: ResolvedQuery) -> tuple[ResolvedProperty, Edge] | None:
    """A group key whose position is reached THROUGH a fanning edge.

    One root row then has several values of that key, so it lands in several
    groups and the column cannot sum to the total. This follows the path to the
    GROUP KEY, which is the thing that actually decides overlap.

    The subquery engine does not have this check. It never needed one, because
    `KeyBeyondGrain` refused every query that could reach this state — a
    coincidence rather than a design, and the reason lifting that refusal had to
    wait for this engine. `distinct_employees` grouped by an ancestor's surname
    is the case: every employee sits under every ancestor above them, so the
    counts are individually right and their total is nonsense.
    """
    for rp in rq.group_by:
        if rp.edge_index is None:
            continue
        blocking = next(
            (e for e in rq.path[: rp.edge_index + 1] if e.link.fans_out), None
        )
        if blocking is not None:
            return rp, blocking
    return None


def analyse(rq: ResolvedQuery, metadata: MetaData) -> GrainPlan:
    """`metadata` is a parameter here where the subquery engine's `analyse`
    needs none: eligibility depends on the grain table's PRIMARY KEY, which is a
    fact about the database rather than the ontology, and it has to be settled
    before a connection is acquired."""
    plan = GrainPlan()
    overlap = _overlap(rq)

    for metric in rq.metrics:
        # Eligibility first, and before any connection is acquired: an engine
        # that silently answered via another strategy would make the
        # differential harness meaningless.
        require_eligible(metric, metadata)

        prefix = path_to_table(rq, metric.grain)
        if prefix is None:
            repairs = _repairs_for(rq, metric.grain)
            in_scope = _in_scope_metric_names(rq)
            raise FanOutRefused(
                metric.name, metric.grain, NOT_ON_PATH,
                in_scope or repairs, reachable_via=repairs,
            )

        # ANY fanning edge on the WHOLE path, not merely those downstream of the
        # grain. The subquery engine only looks downstream, and is right to: a
        # fanning edge in the prefix replicates the grain's ANCESTORS, leaving
        # the grain's own rows intact for a subquery that stops at the grain.
        #
        # Nothing stops at the grain here. A many_to_many in the prefix means one
        # grain row is reachable from several roots, and whether its copies land
        # in one group or several depends on the group key -- by `playlist.id`
        # they separate, by `playlist.name` two playlists merge and the copies
        # collide. Measured: revenue by playlist NAME returned 4215.42 for a true
        # 2107.71 while this only considered downstream edges.
        #
        # So the rule is the safe one. The encoding is a no-op when there is
        # nothing to dedupe -- summing distinct keys equals summing rows when the
        # keys are already distinct -- so applying it wherever replication is
        # POSSIBLE costs only speed, while reasoning about when replicas happen
        # to separate costs correctness when the reasoning is wrong.
        fanning = [e for e in rq.path if e.link.fans_out]

        # An immune aggregate is unaffected by replication and needs nothing.
        if metric.fanout_immune or not fanning:
            strategy: Strategy = "inline"
            forced_by = None
        else:
            strategy = "symmetric"
            forced_by = fanning[0].link.name

        # Additivity asks a different question from strategy: not "would this
        # metric's rows double-count inside a group" — the encoding settles that
        # — but "does one grain row belong to more than one group". Two ways it
        # can, and they are independent.
        # The FACTS, not the verdict. `_additivity` derives the verdict from
        # them, so a condition here cannot disagree with the claim describing it.
        prefix_overlap: PrefixOverlap | None = None
        group_key_overlap: GroupKeyOverlap | None = None

        for edge in prefix:
            # `effective_cardinality`, not `cardinality`: a recursive link
            # declares the one-hop fact while a traversal walks the closure,
            # where each row has many ancestors and each ancestor many
            # descendants. `max_depth=1` is one hop and stays additive.
            if edge.link.effective_cardinality == "many_to_many":
                prefix_overlap = PrefixOverlap(
                    link_name=edge.link.name,
                    cardinality=edge.link.effective_cardinality,
                    grain=metric.grain,
                )
                break

        # `prefix_overlap is None`, not the old `additive` flag: the precedence
        # is preserved by the early return in `_additivity`, and reading the fact
        # rather than a verdict is the point — a decision cannot disagree with
        # itself. Recording the fact unconditionally would ALSO be correct, since
        # `_additivity` returns on the prefix condition first, but it would make
        # the plan claim a second reason the caller never sees.
        if prefix_overlap is None and overlap is not None:
            rp, blocking = overlap
            group_key_overlap = GroupKeyOverlap(
                key_name=rp.name,
                link_name=blocking.link.name,
                cardinality=blocking.link.effective_cardinality,
                grain=metric.grain,
            )

        plan.metric_plans.append(
            MetricPlan(
                metric=metric,
                strategy=strategy,
                forced_by=forced_by,
                prefix_overlap=prefix_overlap,
                group_key_overlap=group_key_overlap,
            )
        )

    return plan
