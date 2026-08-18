import pytest
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def plan_for(onto, **kw):
    return analyse(resolve(QuerySpec(**kw), onto))


def test_metric_reached_through_many_to_many_is_non_additive(chinook_lite):
    """Revenue by playlist: every group correct, and the groups overlap."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
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
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
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
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
                    metrics=["revenue", "track_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    grains = {mp.metric.name: mp.metric.grain for mp in plan.metric_plans}
    assert grains == {"revenue": "invoice_line", "track_count": "track"}
    assert len(plan.metric_plans) == 2


def test_additivity_is_per_metric_not_per_query(chinook_lite):
    """A many_to_many in one metric's prefix must not mark a different metric
    non-additive by way of a shared, latching flag — additivity is computed
    from each metric's own prefix.

    Both metrics here turn out non-additive, but for the same reason, not by
    contamination: `revenue`'s grain (invoice_line) enters scope only at the
    second hop, so its prefix is [Playlist_Tracks, Track_InvoiceLines] — it
    crosses the many_to_many. `track_count`'s grain (track) enters scope at
    the *first* hop already, so its own prefix is just [Playlist_Tracks] —
    which is itself the many_to_many link. Grouping distinct track counts by
    playlist is correct per playlist, but a track that sits in more than one
    playlist is counted once in each, so the column still doesn't sum to the
    true distinct track total. There is no fanning path here whose prefix
    avoids Playlist_Tracks, so this fixture can't exercise the "different
    metric stays additive" case directly -- that's covered by
    test_additive_stays_true_without_a_many_to_many, which uses a query with
    no many_to_many on the path at all.
    """
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
                    metrics=["revenue", "track_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    by_name = {mp.metric.name: mp for mp in plan.metric_plans}

    assert by_name["revenue"].additive is False
    assert by_name["revenue"].non_additive_reason is not None
    assert "Playlist_Tracks" in by_name["revenue"].non_additive_reason

    assert by_name["track_count"].additive is False
    assert by_name["track_count"].non_additive_reason is not None
    assert "Playlist_Tracks" in by_name["track_count"].non_additive_reason

    assert plan.additive is False          # derived: any non-additive


def test_a_branching_path_cannot_be_expressed(chinook_lite):
    """Documents why chasm detection is absent: traverse is a chain, so after a
    hop you are at the target and cannot branch back to the parent."""
    from grain.engine.errors import NoPath
    with pytest.raises(NoPath):
        plan_for(chinook_lite, object="Playlist",
                 traverse=[Hop(link="Playlist_Tracks"), Hop(link="Playlist_Tracks")])
