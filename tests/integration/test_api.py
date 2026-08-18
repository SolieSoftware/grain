import pytest
from decimal import Decimal
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec
from grain.domains.chinook import CHINOOK_DIR

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)


def test_query_returns_rows_and_compiled_sql(g):
    result = g.query(QuerySpec(object="Customer", group_by=["country"],
                               metrics=["customer_count"], limit=100))
    assert result.rows
    assert "SELECT" in result.compiled_sql.upper()


def test_rewrite_is_surfaced_when_the_engine_changes_the_query(g):
    # NOTE: the brief's draft of this test used metrics=["revenue"], but
    # revenue's grain is invoice_line -- the END of this exact traversal, so
    # nothing fans out downstream of it and the correct plan is "inline" (see
    # tests/unit/test_grain.py::test_metrics_at_two_grains_each_get_their_own_plan,
    # which pins strategies == {"revenue": "inline", "customer_count":
    # "aggregate_then_join"} for this identical spec). Asserting a rewrite for
    # revenue here would assert a regression, not a requirement -- verified
    # live against the database before changing it. customer_count's grain is
    # the ROOT (customer), so the whole traversal is downstream of it and the
    # first fanning hop, Customer_Invoices, is what forces the rewrite.
    result = g.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["customer_count"], limit=100,
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")]))
    assert len(result.rewrites) == 1
    assert result.rewrites[0].metric == "customer_count"
    assert result.rewrites[0].strategy == "aggregate_then_join"
    assert result.rewrites[0].forced_by == "Customer_Invoices"


def test_no_rewrite_is_reported_when_none_happened(g):
    result = g.query(QuerySpec(object="Customer", group_by=["country"],
                               metrics=["customer_count"], limit=100))
    assert result.rewrites == []


def test_additive_flag_is_false_across_a_many_to_many(g):
    result = g.query(QuerySpec(
        object="Playlist", group_by=["name"], metrics=["revenue"], limit=100,
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))
    assert result.additive is False
    assert "many_to_many" in result.non_additive_reason


def test_explain_returns_sql_without_executing(g):
    out = g.explain(QuerySpec(object="Customer", group_by=["country"],
                              metrics=["customer_count"], limit=100))
    assert "SELECT" in out["compiled_sql"].upper()
    assert "rows" not in out


def test_row_cap_is_enforced(g):
    result = g.query(QuerySpec(object="Track", group_by=["name"], limit=5))
    assert len(result.rows) <= 5
