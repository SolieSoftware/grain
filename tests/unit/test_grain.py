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


def test_metric_behind_a_fanning_edge_is_rewritten(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    mp = plan.metric_plans[0]
    assert mp.strategy == "aggregate_then_join"
    assert mp.forced_by == "Customer_Invoices"


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
    assert strategies == {"revenue": "aggregate_then_join", "customer_count": "inline"}


def test_analyse_is_total_over_every_metric(chinook_lite):
    """No metric may reach the end of analysis without a verdict."""
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"])
    assert len(plan.metric_plans) == 1
    assert all(mp.strategy in ("inline", "aggregate_then_join") for mp in plan.metric_plans)
