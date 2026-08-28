"""The one thing the database cannot tell us: what one row means, and therefore
when an aggregation over it is safe. Decidable from declared cardinality alone —
no data is read here, which is why the verdict cannot drift as rows are added."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .errors import NOT_ON_PATH, FanOutRefused, KeyBeyondGrain, NonAdditiveRefused
from .ontology import Metric, ObjectType
from .resolve import Edge, ResolvedProperty, ResolvedQuery

Strategy = Literal["inline", "aggregate_then_join"]


@dataclass(frozen=True)
class MetricPlan:
    metric: Metric
    strategy: Strategy
    forced_by: str | None = None
    additive: bool = True
    non_additive_reason: str | None = None
    # How many edges of the path a pre-aggregating subquery must apply: enough to
    # reach the metric's grain, and enough to carry every qualified group key it
    # has to rejoin on. Decided here, with the rest of the verdicts, rather than
    # recomputed in `compile` — a subquery that walked a different distance from
    # the one this analysis reasoned about would silently invalidate the verdict.
    subquery_edges: int = 0


@dataclass
class GrainPlan:
    metric_plans: list[MetricPlan] = field(default_factory=list)

    @property
    def additive(self) -> bool:
        """False if ANY requested metric is non-additive. Callers that need to
        know which one must read metric_plans."""
        return all(mp.additive for mp in self.metric_plans)

    @property
    def non_additive_reason(self) -> str | None:
        """The first reason, for a single-line caveat. Full detail per metric."""
        return next(
            (mp.non_additive_reason for mp in self.metric_plans if mp.non_additive_reason),
            None,
        )


def path_to_table(rq: ResolvedQuery, table: str) -> list[Edge] | None:
    """The shortest prefix of the walked path that brings `table` into scope.

    Returns [] when the table belongs to the root object, and None when the
    table is not reachable along the declared traversal at all.

    `[]` means 'at the root's own grain', and that is sound only because an
    object's spanned tables are all at the object's grain — an invariant the
    loader now ENFORCES (a fanning `TableJoin` is refused at load) rather than
    assumes. It used to be assumed, which is how a metric whose grain was a
    joined table got treated as root-grain and summed over replicated rows
    (defect C5).
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


def _root_grain_metrics(rq: ResolvedQuery) -> list[str]:
    """Metrics measured at the root's own grain, which no traversal can fan."""
    return sorted(
        name
        for name, metric in rq.ontology.metrics.items()
        if metric.grain == rq.root.primary
    )


def _pinned_by_a_unique_key(rq: ResolvedQuery, index: int) -> ResolvedProperty | None:
    """The group key, if any, that identifies ONE row of the object hop `index`
    lands on.

    A fanning edge downstream of the metric's grain replicates that metric's rows
    — once per child — and the aggregate-then-join rewrite exists because those
    copies would otherwise be summed together. But if a group key pins the row at
    that edge's own target, each copy lands in a DIFFERENT group, and inside any
    one group the metric's row appears exactly once. The inline aggregate is then
    correct per group, and the honest verdict is `non-additive`, not `rewrite`.

    That is the same shape the engine already answers in the other direction:
    `revenue` (grain at the far end) by `Playlist` is inline and non-additive, so
    `employee_count` (grain at the root) by ancestor should be too.

    The key must sit at the fanning edge's OWN position, not merely somewhere
    beyond it. A key further along determines the row at the fanning edge only
    when every edge in between is functional in reverse (`one_to_many` or
    `one_to_one`), and getting that wrong is silent. The counterexample:
    `Playlist -> Playlist_Tracks (many_to_many) -> Track_Album (many_to_one)`
    grouped by a unique Album key. One album does not determine which track, so a
    playlist holding two tracks from the same album appears TWICE inside that
    album's group. Requiring the key at the fanning position itself needs no such
    reasoning and so cannot be got wrong; the wider rule is a deliberate
    non-goal until something measures that it is needed.

    Existing specs are unaffected by construction: a bare group key has
    `edge_index is None`, which matches no position, so nothing that compiled to
    a rewrite before compiles to anything else now.
    """
    return next(
        (rp for rp in rq.group_by if rp.edge_index == index and rp.prop.unique),
        None,
    )


def _unique_properties(obj: ObjectType, prefix: str = "") -> list[str]:
    """The names an agent can legally group a non-additive query by, on `obj`."""
    return sorted(
        f"{prefix}{name}" for name, prop in obj.properties.items() if prop.unique
    )


def _require_identifying_keys(
    rq: ResolvedQuery, metric: Metric, link: str
) -> list[ResolvedProperty]:
    """A non-additive query must group by something that identifies one root row.

    Non-additivity is surfaced rather than refused (R2) on the strength of one
    claim: every group is correct on its own, and only the total is meaningless.
    That claim has a precondition nothing used to check — that a group IS one row
    of the root object. Both ways it can fail produce a wrong number that the
    engine computed itself, with `additive: false` attached as though the caller
    could do something about it:

    - `group_by` empty (C1): one group holding every root row. `revenue` over
      `Playlist_Tracks` returned 5738.28 for a true 2328.60, unlabelled.
    - `group_by` on no unique key (C2): two root rows sharing a value merge, and
      anything reachable from both is counted twice inside that single group.
      Chinook's two 'Music' playlists returned 4215.42 for 2107.71.

    Neither is rescued by aggregate-then-join: the many_to_many sits in the
    prefix reaching the grain, so a subquery grouped at that grain replicates
    the same rows in the same way.
    """
    identifying = [rp for rp in rq.group_by if rp.prop.unique]
    if identifying:
        return identifying

    # A group key identifies one row of the object IT names, which need not be
    # the root: grouping by an ancestor's key makes each group one ancestor, and
    # that is just as sound. So the offer covers the root and every object the
    # path lands on, qualified the way the caller would have to write it.
    declared = _unique_properties(rq.root)
    for edge in rq.path:
        declared += _unique_properties(edge.to_object, prefix=f"{edge.link.name}.")

    alternatives = (
        [f"group_by a property that identifies one row: {', '.join(declared)}"]
        if declared
        else [f"declare a unique property on {rq.root.name} to group by"]
    )
    alternatives += [
        f"use {name}, which is measured at {rq.root.primary} grain and does not "
        f"cross {link}"
        for name in _root_grain_metrics(rq)
    ]
    if not rq.group_by:
        reason = (
            "With no group_by there is a single group holding every "
            f"{rq.root.name}, so summing it double-counts."
        )
    else:
        grouped = ", ".join(rp.name for rp in rq.group_by)
        reason = (
            f"The group_by keys ({grouped}) do not identify one row of the object "
            f"they name, so two rows sharing a value merge into one group and "
            f"everything reachable from both is counted twice inside it."
        )
    raise NonAdditiveRefused(metric.name, metric.grain, link, alternatives, reason)


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
        fanning = [
            (len(prefix) + offset, edge)
            for offset, edge in enumerate(downstream)
            if edge.link.fans_out
        ]
        # A fanning edge whose own target is pinned by a unique group key scatters
        # its copies into distinct groups instead of piling them into one, so it
        # needs no rewrite. See `_pinned_by_a_unique_key`.
        separated = [(i, e) for i, e in fanning if _pinned_by_a_unique_key(rq, i)]
        unpinned = [(i, e) for i, e in fanning if not _pinned_by_a_unique_key(rq, i)]
        # A fan-out-immune aggregate cannot be changed by replication, so no
        # edge -- fanning or not -- forces a rewrite for it. The aggregate alone
        # settles this, which is why it overrides the path entirely.
        #
        # STRATEGY ONLY. Additivity below is untouched: `count(distinct x)`
        # grouped across a many_to_many still produces overlapping groups, and a
        # track on two playlists is still counted in both. Immunity is about
        # replication INSIDE a group; overlap is a property of the path.
        immune = metric.fanout_immune
        strategy: Strategy = (
            "inline" if immune or not unpinned else "aggregate_then_join"
        )
        forced_by = (
            None if immune else (unpinned[0][1].link.name if unpinned else None)
        )

        # A qualified group key lives at a position on the path, so a
        # pre-aggregating subquery has to walk far enough to carry it. If that is
        # further than the grain and anything in between fans out, walking there
        # would replicate this metric's own rows inside the subquery built to
        # stop that — refuse instead, naming what to drop.
        reach = [rp for rp in rq.group_by if rp.edge_index is not None]
        needed = max([rp.edge_index + 1 for rp in reach], default=0)
        subquery_edges = max(len(prefix), needed)
        # Gated on `unpinned` -- would a rewrite have been forced? -- NOT on the
        # final strategy, so immunity cannot lift this refusal.
        #
        # It is tempting to lift it: an immune metric builds no subquery, so a
        # key beyond its grain costs it nothing. But this refusal is also, and
        # accidentally, the only thing preventing a WRONG ADDITIVE VERDICT.
        # `employee_count` grouped by an ancestor's surname puts one employee in
        # every ancestor group above them, so the column cannot sum to the
        # total -- yet the additivity loop below iterates `prefix`, which is
        # EMPTY for a metric measured at the root, so it never sees the
        # many_to_many and would report `additive: true`.
        #
        # Lifting the refusal therefore requires fixing overlap detection to
        # follow the path to the GROUP KEY rather than to the grain. That is the
        # symmetric engine's job, where lifting this is a stated deliverable.
        # Until then immunity changes only which SQL is emitted, never which
        # queries are answerable.
        if unpinned and needed > len(prefix):
            blocking = next(
                (e for e in rq.path[len(prefix):needed] if e.link.fans_out), None
            )
            if blocking is not None:
                key = next(rp for rp in reach if rp.edge_index + 1 > len(prefix))
                raise KeyBeyondGrain(
                    metric.name, metric.grain, key.name, blocking.link.name
                )

        # Additivity is orthogonal to strategy and to fan-out: it asks whether
        # the *group-by keys* (which live on the root) overlap, not whether
        # this metric's own rows would double-count. A many_to_many anywhere
        # on the path from the grain back to the root — `prefix` — means the
        # same grain row can belong to more than one group, so the column
        # will not sum to the total even though every group is correct. This
        # is a property of THIS metric's own prefix, not of the query as a
        # whole — a different metric's grain can sit on an entirely different,
        # all-one-to-many prefix and remain perfectly additive.
        additive = True
        non_additive_reason: str | None = None
        for edge in prefix:
            # `effective_cardinality`, not `cardinality`: a recursive link
            # declares the one-hop fact (many_to_one — one manager) while a
            # traversal walks the closure, where each row has many ancestors and
            # each ancestor many descendants. That is many_to_many, and it makes
            # the ancestor groups overlap in exactly the way this branch exists
            # to catch. `Hop(max_depth=1)` is one hop and stays non-additive-free.
            if edge.link.effective_cardinality == "many_to_many":
                additive = False
                # Surfacing this instead of refusing it is only defensible while
                # the per-group numbers ARE correct, and that holds only when one
                # group is one row of the object being grouped. Where it does
                # not, the engine itself does the wrong summing and no flag can
                # rescue the caller.
                identifying = _require_identifying_keys(rq, metric, edge.link.name)
                keys = ", ".join(rp.name for rp in identifying)
                subject = identifying[0].object.name
                non_additive_reason = (
                    f"'{metric.name}' is grouped across '{edge.link.name}', which is "
                    f"{edge.link.effective_cardinality}. Each group is correct — "
                    f"'{keys}' identifies one {subject} — but the groups overlap, so "
                    f"this column will not sum to the total."
                )
                break

        if additive and separated:
            # Inline was only correct because a unique key scattered this
            # metric's replicated rows into distinct groups. That makes every
            # group right and the TOTAL meaningless: one row is counted in every
            # group it belongs to. Saying so is not optional — it is the whole
            # reason this verdict is allowed to be inline.
            index, edge = separated[0]
            pin = _pinned_by_a_unique_key(rq, index)
            additive = False
            non_additive_reason = (
                f"'{metric.name}' is counted once per {edge.to_object.name} reached "
                f"through '{edge.link.name}', which is "
                f"{edge.link.effective_cardinality}. Each group is correct — "
                f"'{pin.name}' identifies one {edge.to_object.name} — but one "
                f"'{metric.grain}' row belongs to several groups, so this column "
                f"will not sum to the total."
            )

        plan.metric_plans.append(
            MetricPlan(
                metric=metric,
                strategy=strategy,
                forced_by=forced_by,
                additive=additive,
                non_additive_reason=non_additive_reason,
                subquery_edges=subquery_edges,
            )
        )

    return plan
