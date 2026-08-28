"""A fan-out-immune aggregate needs no rewrite, however the path fans.

The planner decided strategy from path cardinality alone and never looked at the
metric, so `count(distinct customer.customer_id)` was pushed through a
pre-aggregating subquery it could never need: DISTINCT already collapses the
replicas the join created.
"""
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def _plan(spec, onto):
    return analyse(resolve(spec, onto))


def test_count_distinct_across_a_fanning_link_stays_inline(chinook_lite):
    """`distinct_customers` is measured at the root, and `Customer_Invoices` is
    one_to_many, so every customer row is replicated once per invoice. DISTINCT
    on the key undoes exactly that."""
    plan = _plan(QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices")],
        group_by=["country"],
        metrics=["distinct_customers"],
    ), chinook_lite)
    (mp,) = plan.metric_plans
    assert mp.strategy == "inline"
    assert mp.forced_by is None


def test_a_summing_metric_across_a_fanning_link_is_still_rewritten(chinook_lite):
    """The control. Immunity is a property of the aggregate, not of the path --
    if this also went inline it would be a regression of C5, not a feature."""
    plan = _plan(QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        group_by=["country"],
        metrics=["invoice_total"],
    ), chinook_lite)
    (mp,) = plan.metric_plans
    assert mp.strategy == "aggregate_then_join"
    assert mp.forced_by == "Invoice_Lines"


def test_immunity_does_not_make_overlapping_groups_additive(chinook_lite):
    """Immunity is about replication INSIDE a group. Overlapping groups are a
    separate fact about the path, and a count(distinct) is just as affected: a
    track on two playlists is counted in both, so the column still will not sum
    to the total."""
    plan = _plan(QuerySpec(
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        group_by=["id"],
        metrics=["distinct_tracks"],
    ), chinook_lite)
    (mp,) = plan.metric_plans
    assert mp.strategy == "inline"
    assert plan.additive is False


def test_immunity_does_not_lift_the_key_beyond_grain_refusal(chinook_lite):
    """Immunity changes which SQL is emitted, never which queries are answerable.

    Lifting `KeyBeyondGrain` here would be defensible on its own terms -- an
    immune metric builds no subquery for the key to be beyond -- but that
    refusal is also the only thing preventing a wrong ADDITIVE verdict:
    `distinct_employees` grouped by an ancestor's surname puts one employee in every
    ancestor group above them, and the additivity loop iterates the path to the
    GRAIN, which is empty for a root-measured metric. So it would report
    `additive: true` for a column that cannot sum to the total.

    Lifting it belongs with the overlap fix, in the symmetric engine.
    """
    import pytest

    from grain.engine.errors import KeyBeyondGrain

    with pytest.raises(KeyBeyondGrain):
        _plan(QuerySpec(
            object="Employee",
            traverse=[Hop(link="Employee_Manager")],
            group_by=["Employee_Manager.last_name"],
            metrics=["distinct_employees"],
        ), chinook_lite)
