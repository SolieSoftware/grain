import pytest
from sqlalchemy import event

from grain.engine.api import Grain
from grain.engine.errors import GuardTripped
from grain.engine.guard import GuardConfig
from grain.engine.spec import Hop, QuerySpec
from grain.domains.chinook import CHINOOK_DIR

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)


@pytest.fixture(scope="session")
def g_tight_cap(db_engine):
    """Same domain, a `row_cap` small enough that an ordinary un-grouped
    query on this data exceeds it -- used to prove the cap is reachable
    through the real facade, not just at the guard layer in isolation."""
    return Grain.load(CHINOOK_DIR, db_engine, guard=GuardConfig(row_cap=5))


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
    # Grouped by `id` as well as `name`, because chinook ships duplicate playlist
    # NAMES: grouping by name alone merges two playlists into one group and
    # double-counts every track they share, which the engine now refuses rather
    # than flags (defect C2, proven in tests/integration/test_defect_anchors.py).
    result = g.query(QuerySpec(
        object="Playlist", group_by=["id", "name"], metrics=["revenue"], limit=100,
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))
    assert result.additive is False
    assert "many_to_many" in result.non_additive_reason


def test_explain_returns_sql_without_executing(g):
    """`"rows" not in out` alone is vacuous -- the dict literal in
    `explain()` guarantees that regardless of whether a query ran. Attach a
    `before_cursor_execute` listener on the real engine and prove it never
    fires: that's a statement about behaviour, not about a dict's shape."""
    executed: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(g.engine, "before_cursor_execute", _record)
    try:
        out = g.explain(QuerySpec(object="Customer", group_by=["country"],
                                  metrics=["customer_count"], limit=100))
    finally:
        event.remove(g.engine, "before_cursor_execute", _record)

    assert executed == []
    assert "SELECT" in out["compiled_sql"].upper()
    assert "rows" not in out


def test_spec_limit_bounds_the_result(g):
    """Proves the SQL LIMIT clause bounds the result -- this is `QuerySpec.
    limit` doing its job, not the guard's `row_cap` (10_000 by default,
    nowhere near 5). Covered here for the facade; the guard's own row_cap
    behaviour is proven directly in tests/integration/test_guard_enforcement.py."""
    result = g.query(QuerySpec(object="Track", group_by=["name"], limit=5))
    assert len(result.rows) <= 5


def test_row_cap_is_reachable_through_the_real_facade(g_tight_cap):
    """`limit=None` now legally requests every row with no SQL LIMIT at all,
    which is exactly the case the guard exists to catch -- this is the same
    scenario as test_guard_enforcement.py's direct test, but proven
    end-to-end through `Grain.query()` rather than by calling `execute()`
    in isolation."""
    with pytest.raises(GuardTripped) as exc:
        g_tight_cap.query(QuerySpec(object="Track", group_by=["name"], limit=None))
    assert exc.value.limit_name == "row_cap"
