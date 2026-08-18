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
    """A many_to_many in one metric's prefix must not mark a sibling non-additive.

    revenue's prefix crosses Playlist_Tracks (many_to_many), so it is non-additive.
    playlist_count's grain IS the root, so its prefix is empty and it is additive.
    Under the old plan-level flag both came back non-additive, which is why this
    test must assert the sibling stays True — that assertion is the regression guard.
    """
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
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
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
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
