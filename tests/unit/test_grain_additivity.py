import pytest
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def plan_for(onto, **kw):
    return analyse(resolve(QuerySpec(**kw), onto))


def test_metric_reached_through_many_to_many_is_non_additive(chinook_lite):
    """Revenue by playlist: every group correct, and the groups overlap."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["id", "name"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    assert plan.additive is False
    assert "many_to_many" in plan.non_additive_reason
    assert "Playlist_Tracks" in plan.non_additive_reason


def test_non_additive_query_is_still_answered_not_refused(chinook_lite):
    """Refusing would be the worse answer — the per-group numbers are wanted.

    No FanOutRefused is raised, and a strategy is produced. Nothing is
    traversed after invoice_line enters scope (it's the last hop), so the
    fan-out rule — which looks downstream of the grain, per Task 8 — finds
    nothing to rewrite and this comes back "inline". That's correct: each
    invoice_line row is joined exactly once per playlist it belongs to. The
    non-additivity is a separate, orthogonal fact about the group-by keys
    overlapping (see test_metric_reached_through_many_to_many_is_non_additive),
    not about this metric's own strategy.
    """
    plan = plan_for(chinook_lite, object="Playlist", group_by=["id", "name"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    assert plan.metric_plans[0].strategy == "inline"
    assert plan.additive is False


def test_additive_stays_true_without_a_many_to_many(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert plan.additive is True
    assert plan.non_additive_reason is None


def test_metrics_at_different_grains_are_isolated_from_each_other(chinook_lite):
    """The property that makes chasm detection unnecessary: each metric is
    planned independently at its own grain, so two fanning branches can never
    multiply against one another."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["id", "name"],
                    metrics=["revenue", "track_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    grains = {mp.metric.name: mp.metric.grain for mp in plan.metric_plans}
    assert grains == {"revenue": "invoice_line", "track_count": "track"}
    assert len(plan.metric_plans) == 2


def test_additivity_is_per_metric_not_per_query(chinook_lite):
    """A many_to_many in one metric's prefix must not mark a sibling non-additive.

    revenue's prefix crosses Playlist_Tracks (many_to_many), so it is non-additive.
    playlist_count's grain IS the root, so its prefix is empty and it is additive.
    Under the old plan-level flag both came back non-additive, which is why this
    test must assert the sibling stays True — that assertion is the regression guard.
    """
    plan = plan_for(chinook_lite, object="Playlist", group_by=["id", "name"],
                    metrics=["revenue", "playlist_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    by_name = {mp.metric.name: mp for mp in plan.metric_plans}
    assert by_name["revenue"].additive is False
    assert by_name["revenue"].non_additive_reason is not None
    assert by_name["playlist_count"].additive is True          # <-- the regression guard
    assert by_name["playlist_count"].non_additive_reason is None
    assert plan.additive is False      # derived: False if ANY metric is non-additive


def test_two_metrics_can_be_non_additive_for_their_own_reasons(chinook_lite):
    """revenue and track_count both cross Playlist_Tracks in their own prefix —
    each is non-additive on its own merits, not because one contaminated the
    other. (track_count's grain, track, enters scope at Playlist_Tracks itself,
    so its own prefix is [Playlist_Tracks] — no need to reach as far as
    Track_InvoiceLines for this one.)"""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["id", "name"],
                    metrics=["revenue", "track_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    for mp in plan.metric_plans:
        assert mp.additive is False
        assert "Playlist_Tracks" in mp.non_additive_reason


def test_a_branching_path_cannot_be_expressed(chinook_lite):
    """Documents why chasm detection is absent: traverse is a chain, so after a
    hop you are at the target and cannot branch back to the parent."""
    from grain.engine.errors import NoPath
    with pytest.raises(NoPath):
        plan_for(chinook_lite, object="Playlist",
                 traverse=[Hop(link="Playlist_Tracks"), Hop(link="Playlist_Tracks")])


def test_non_additive_with_no_group_by_is_refused(chinook_lite):
    """C1. `additive: False` used to be the ONLY response here, and it is not a
    response a caller can act on: with no group_by there is one group, the engine
    itself summed every root row into it, and the flag's own message said each
    group was correct. Measured on the live database, this returned 5738.28 as a
    single unlabelled figure against a true 2328.60.

    Aggregate-then-join cannot rescue it — the many_to_many is in the prefix
    reaching the grain, so a subquery grouped at that grain replicates the same
    rows — which is why refusing is the only correct rendering.
    """
    from grain.engine.errors import NonAdditiveRefused

    with pytest.raises(NonAdditiveRefused) as excinfo:
        plan_for(chinook_lite, object="Playlist", group_by=[], metrics=["revenue"],
                 traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    # The error must name a legal next move, not merely say no.
    assert excinfo.value.alternatives
    assert any("id" in alt for alt in excinfo.value.alternatives)
    assert "Playlist_Tracks" in str(excinfo.value)


def test_non_additive_grouped_by_a_non_unique_key_is_refused(chinook_lite):
    """C2. The per-group guarantee assumes one group is one root row. Chinook
    ships duplicate playlist NAMES, so grouping by name merged two playlists and
    double-counted every track they shared: 4215.42 returned for 2107.71 of
    distinct-track revenue, while `describe()` published "each group is still
    correct" as a domain fact."""
    from grain.engine.errors import NonAdditiveRefused

    with pytest.raises(NonAdditiveRefused, match="do not identify one row"):
        plan_for(chinook_lite, object="Playlist", group_by=["name"],
                 metrics=["revenue"],
                 traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])


def test_a_unique_key_alongside_a_non_unique_one_is_enough(chinook_lite):
    """One key that identifies the root row is sufficient — the others only add
    labels to a group that is already one playlist. This is the form an agent
    should be steered to, so it must not be refused."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name", "id"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    assert plan.additive is False
    assert "identifies one Playlist" in plan.non_additive_reason


def test_an_additive_query_may_group_by_anything(chinook_lite):
    """The uniqueness requirement is scoped to NON-additive queries. Revenue by
    country is ordinary aggregation over disjoint groups and grouping it by a
    non-unique key is exactly what a group-by is for — requiring uniqueness here
    would be a bug, not a safeguard."""
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert plan.additive is True
