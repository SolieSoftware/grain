"""A stock, measured against hand-computed figures.

Every number here is derivable by eye from `inventory.sql`. Requires that seed:

    GRAIN_DATABASE_URL=... uv run python tools/seed_inventory.py

Why this file carries more weight than the other anchor files: the symmetric
engine refuses a stock by design, so the differential harness — two engines
disagreeing — cannot see this path at all. What replaces it is `tools/oracle.py`,
which computes the same answers in pure Python from raw rows and shares no SQL
with either engine. `test_the_oracle_agrees_...` below is that check, and it is
the strongest thing said about stock anywhere in the suite.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook_inventory import INVENTORY_DIR
from grain.engine.api import Grain
from grain.engine.spec import QuerySpec

pytestmark = pytest.mark.integration

LEVEL = QuerySpec(object="Inventory", metrics=["inventory_level"], limit=None)


@pytest.fixture(scope="module")
def seeded(db_engine):
    """The seed is opt-in — a domain pack must never create schema as a side
    effect of being loaded — so an unseeded database is a supported state and
    these tests skip rather than fail."""
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "select count(*) from information_schema.tables"
            " where table_name = 'daily_inventory'")).scalar()
    if not rows:
        pytest.skip("run tools/seed_inventory.py first")


@pytest.fixture(scope="module")
def g(db_engine, seeded):
    return Grain.load(INVENTORY_DIR, db_engine, engine_name="subquery")


@pytest.fixture(scope="module")
def oracle_db(db_engine, seeded):
    """Imported inside the fixture, not at module scope: `oracle.py` reads
    GRAIN_DATABASE_URL at import time, and module-level import would turn a
    missing URL into a collection error instead of the skip the rest of the
    suite gives."""
    from oracle import Db

    with db_engine.connect() as conn:
        return Db(conn)


def test_the_naive_sum_differs_from_the_level(db_engine, seeded):
    """The control. Summing across dates gives 1153; the level is 111. Without
    this gap every other test here could pass on an engine that never windowed
    at all."""
    with db_engine.connect() as conn:
        naive = conn.execute(text(
            "select sum(units_on_hand) from daily_inventory")).scalar()
        level = conn.execute(text("""
            select sum(units_on_hand) from daily_inventory
            where as_of_date = (select max(as_of_date) from daily_inventory)
        """)).scalar()
    assert naive == 1153
    assert level == 111


def test_the_global_level_is_the_latest_instant(g):
    """No group_by: the window partitions over the whole population, so this is
    the level at 2026-01-03 = 4 + 7 + 100."""
    assert int(g.query(LEVEL).rows[0][0]) == 111


def test_every_row_tied_at_the_boundary_counts(g):
    """Three tracks share the latest date. A window picking ONE row per
    partition — `row_number() = 1` rather than `t = max(t) over (...)` — would
    return one of 4, 7 or 100 here, all of which are smaller than the largest
    single row. Seeded deliberately: with no tie at the boundary that
    regression passes every other test in this file."""
    level = int(g.query(LEVEL).rows[0][0])
    assert level == 111
    assert level > 100, "the boundary holds three rows, and all three count"


def test_a_track_absent_at_the_boundary_does_not_contribute(g):
    """Track 4 was last recorded on 01-01. A `last` window over the whole
    population must drop it: keeping each track's own last row instead would
    give 4 + 7 + 100 + 999 = 1110."""
    level = int(g.query(LEVEL).rows[0][0])
    assert level == 111
    assert level != 1110


def test_grouping_by_the_time_dimension_makes_the_window_a_no_op(g):
    """Each group holds one instant already, so every date's own total appears:
    01-01 = 10+3+999 = 1012, 01-02 = 25+5 = 30, 01-03 = 4+7+100 = 111."""
    rows = {str(r[0]): int(r[1]) for r in g.query(QuerySpec(
        object="Inventory", group_by=["as_of"], metrics=["inventory_level"],
        limit=None)).rows}
    assert rows == {"2026-01-01": 1012, "2026-01-02": 30, "2026-01-03": 111}


def test_the_boundary_is_per_group_not_global(g):
    """Grouped by track, each group windows to ITS OWN latest instant — so
    track 4, absent from the global boundary, keeps its own last level of 999.
    A window whose `partition by` were dropped would give one global boundary
    and lose that row entirely."""
    rows = {int(r[0]): int(r[1]) for r in g.query(QuerySpec(
        object="Inventory", group_by=["track"], metrics=["inventory_level"],
        limit=None)).rows}
    assert rows == {1: 4, 2: 7, 3: 100, 4: 999}


def _opening_level():
    """`choice: first`, declared on the fly. The shipped pack has no use for an
    opening level; the field would otherwise never be read by a test against
    real data, and a default nothing exercises is a default nobody knows
    works."""
    from grain.engine.ontology import Metric, OverTime

    return Metric(
        name="opening_level", grain="daily_inventory", type="integer",
        agg="sum", value="daily_inventory.units_on_hand", quantity="stock",
        over_time=OverTime(dimension="as_of", choice="first"))


def test_choice_first_windows_to_the_earliest_instant(g):
    """Same construction, other end: 01-01 = 10 + 3 + 999 = 1012."""
    g.ontology.metrics["opening_level"] = _opening_level()
    try:
        result = g.query(QuerySpec(object="Inventory",
                                   metrics=["opening_level"], limit=None))
    finally:
        del g.ontology.metrics["opening_level"]
    assert int(result.rows[0][0]) == 1012


def test_two_windowed_stocks_in_one_query_are_refused(g):
    """The second window's filter would apply to rows the first had dropped."""
    from grain.engine.errors import GrainError

    g.ontology.metrics["opening_level"] = _opening_level()
    try:
        with pytest.raises(GrainError, match="at most one stock"):
            g.query(QuerySpec(object="Inventory",
                              metrics=["inventory_level", "opening_level"],
                              limit=None))
    finally:
        del g.ontology.metrics["opening_level"]


def test_the_symmetric_engine_refuses_it_end_to_end(db_engine, seeded):
    """Stated in the design, not discovered: the window needs a window function
    inside a subquery and that engine is one pass. Pinned end to end, through
    the facade, because a refusal that only held at the `require_eligible` unit
    boundary would still let some other path serve the metric quietly — and an
    engine that silently switched strategy is what would make the differential
    harness meaningless."""
    from grain.engine.errors import MetricNotSymmetric

    sym = Grain.load(INVENTORY_DIR, db_engine, engine_name="symmetric")
    with pytest.raises(MetricNotSymmetric, match="subquery"):
        sym.query(LEVEL)


@pytest.mark.parametrize("group_by,group_prop", [
    (None, None),
    (["as_of"], ("daily_inventory", "as_of_date")),
    (["track"], ("daily_inventory", "track_id")),
])
@pytest.mark.parametrize("metric", ["inventory_level", "opening_level"])
def test_the_oracle_agrees_with_the_engine(g, oracle_db, group_by, group_prop, metric):
    """THE check for stock. `tools/oracle.py` collapses to the boundary instant
    and sums, in Python, over raw rows — no SQL, no window function, nothing
    shared with the engine that could carry the same misconception into both.

    With the symmetric engine refusing stock outright, this stands in for the
    differential harness. The design accepted losing that cross-check only
    because the oracle was the stated mitigation."""
    from oracle import answer

    g.ontology.metrics.setdefault("opening_level", _opening_level())
    try:
        rows = g.query(QuerySpec(object="Inventory", group_by=group_by or [],
                                 metrics=[metric], limit=None)).rows
    finally:
        g.ontology.metrics.pop("opening_level", None)

    truth = answer(oracle_db, obj="Inventory", links=[],
                   group_props=[group_prop] if group_prop else [],
                   metric_name=metric)
    got = {tuple(r[:-1]): int(r[-1]) for r in rows}
    assert got == {k: int(v) for k, v in truth.items()}
    assert got, "an empty comparison would agree with anything"
