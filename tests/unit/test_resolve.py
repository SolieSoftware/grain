import pytest

from grain.engine.errors import NoPath, UnknownName
from grain.engine.resolve import resolve, suggest
from grain.engine.spec import Hop, QuerySpec


def test_resolves_root_and_direct_path(chinook_lite):
    rq = resolve(
        QuerySpec(object="Customer", traverse=[Hop(link="Customer_Invoices")]),
        chinook_lite,
    )
    assert rq.root.name == "Customer"
    assert [e.link.name for e in rq.path] == ["Customer_Invoices"]
    assert "invoice" in rq.tables_in_scope


def test_unknown_object_suggests_nearest(chinook_lite):
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Custommer"), chinook_lite)
    assert "Customer" in exc.value.alternatives


def test_unknown_metric_suggests_nearest(chinook_lite):
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Customer", metrics=["revenu"]), chinook_lite)
    assert "revenue" in exc.value.alternatives


def test_suggestion_is_itself_a_valid_name(chinook_lite):
    """An error that suggests an invalid name wastes the retry it was meant to save."""
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Customer", metrics=["revenu"]), chinook_lite)
    for candidate in exc.value.alternatives:
        assert candidate in chinook_lite.metrics


def test_hop_that_does_not_start_at_current_object_raises_no_path(chinook_lite):
    with pytest.raises(NoPath):
        resolve(
            QuerySpec(object="Customer", traverse=[Hop(link="Invoice_Lines")]),
            chinook_lite,
        )


def test_fanning_edges_reports_only_multiplying_hops(chinook_lite):
    rq = resolve(
        QuerySpec(
            object="Customer",
            traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        ),
        chinook_lite,
    )
    assert [e.link.name for e in rq.fanning_edges] == ["Customer_Invoices", "Invoice_Lines"]


def test_many_to_one_hop_does_not_fan(chinook_lite):
    rq = resolve(
        QuerySpec(object="Customer", traverse=[Hop(link="Customer_SupportRep")]),
        chinook_lite,
    )
    assert rq.fanning_edges == []


def test_suggest_orders_by_closeness():
    assert suggest("revenu", ["revenue", "units_sold", "track_count"])[0] == "revenue"
