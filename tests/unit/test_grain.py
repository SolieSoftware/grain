import pytest
from grain.engine.errors import FanOutRefused
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def plan_for(onto, **spec_kwargs):
    return analyse(resolve(QuerySpec(**spec_kwargs), onto))


def test_metric_at_root_grain_is_inline(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"])
    assert plan.metric_plans[0].strategy == "inline"
    assert plan.metric_plans[0].forced_by is None


def test_metric_at_the_finest_grain_aggregates_inline(chinook_lite):
    """Nothing is traversed after invoice_line enters scope, so each of its rows
    appears exactly once in the join and inline is correct. The upstream fan-out
    replicates invoices, not invoice_lines."""
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    mp = plan.metric_plans[0]
    assert mp.strategy == "inline"
    assert mp.forced_by is None


def test_metric_behind_only_many_to_one_stays_inline(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"],
                    traverse=[Hop(link="Customer_SupportRep")])
    assert plan.metric_plans[0].strategy == "inline"


def test_coarser_metric_on_a_fanned_path_is_rewritten_not_returned_raw(chinook_lite):
    """invoice_total lives at invoice grain; the path reaches invoice_line."""
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["invoice_total"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert plan.metric_plans[0].strategy == "aggregate_then_join"


def test_metric_whose_grain_is_off_path_is_refused(chinook_lite):
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Customer", metrics=["revenue"])
    assert exc.value.metric == "revenue"
    assert exc.value.grain == "invoice_line"


def test_refusal_suggests_a_metric_that_is_in_scope(chinook_lite):
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Customer", metrics=["revenue"])
    assert "customer_count" in exc.value.alternatives
    for name in exc.value.alternatives:
        assert name in chinook_lite.metrics


def test_metrics_at_two_grains_each_get_their_own_plan(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue", "customer_count"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    strategies = {mp.metric.name: mp.strategy for mp in plan.metric_plans}
    assert strategies == {"revenue": "inline", "customer_count": "aggregate_then_join"}


def test_analyse_is_total_over_every_metric(chinook_lite):
    """No metric may reach the end of analysis without a verdict."""
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"])
    assert len(plan.metric_plans) == 1
    assert all(mp.strategy in ("inline", "aggregate_then_join") for mp in plan.metric_plans)


def test_fanning_edge_after_the_grain_forces_a_rewrite(chinook_lite):
    """The grain is the ROOT here, so the prefix reaching it is empty — but a
    fanning hop downstream still replicates its rows. Getting this wrong emits
    sum(invoice.total) over invoice_line and returns 20848.62 for a true 2328.60."""
    plan = plan_for(chinook_lite, object="Invoice", metrics=["invoice_total"],
                    traverse=[Hop(link="Invoice_Lines")])
    assert plan.metric_plans[0].strategy == "aggregate_then_join"
    assert plan.metric_plans[0].forced_by == "Invoice_Lines"


def test_fanning_edge_before_the_grain_leaves_it_inline(chinook_lite):
    """Customer_Invoices replicates customers, not invoices; with nothing
    traversed after invoice enters scope, invoice_total is safe inline."""
    plan = plan_for(chinook_lite, object="Customer", metrics=["invoice_total"],
                    traverse=[Hop(link="Customer_Invoices")])
    assert plan.metric_plans[0].strategy == "inline"
    assert plan.metric_plans[0].forced_by is None


def test_refusal_always_names_a_legal_next_move(chinook_lite):
    """No metric is in scope from Employee, so the metric-name suggestions are
    empty and the refusal must fall back to a repair the caller can act on."""
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Employee", metrics=["customer_count"])
    assert exc.value.alternatives
    assert "query Customer instead" in exc.value.alternatives


def test_unreachable_grain_is_not_reported_as_a_fan_out(chinook_lite):
    """Nothing fanned out — the grain never enters scope — so the message must
    say so and name the hop that would bring it in."""
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Customer", metrics=["revenue"])
    msg = str(exc.value)
    assert "not reachable" in msg
    assert "fans out" not in msg
    assert "add the Customer_Invoices hop" in exc.value.reachable_via
