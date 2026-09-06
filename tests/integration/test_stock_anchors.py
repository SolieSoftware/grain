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
from grain.engine.spec import Hop, QuerySpec

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


# -- the verdict attached to the column -------------------------------------
#
# Every figure above is correct. What was wrong was the CLAIM: a windowed stock
# reported `additive: true`, and the wrong number arrived one honest sum()
# later, computed by the agent the flag was written for. See the comment in
# `engine/grain.py` for why neither standing safety net could see it -- the
# oracle agrees per group; it is the total that is wrong.


def test_a_grouped_level_is_not_additive(g):
    """1110 (by track) is not the ungrouped 111, and 1153 (by as_of) is exactly
    the naive across-time sum the control test pins as wrong. Both are one
    sum() away from the rows, so the column must not claim to be summable."""
    for key, total in [(["track"], 1110), (["as_of"], 1153)]:
        result = g.query(QuerySpec(object="Inventory", group_by=key,
                                   metrics=["inventory_level"], limit=None))
        assert sum(int(r[1]) for r in result.rows) == total
        assert result.additive is False, f"group_by={key} claims to be summable"
        assert "inventory_level" in result.non_additive_reason
        assert "level at no instant" in result.non_additive_reason


def test_an_ungrouped_level_is_additive(g):
    """One global boundary instant, one figure, nothing to add it to. Marking
    THIS non-additive would cry wolf on the only whole answer there is."""
    result = g.query(LEVEL)
    assert int(result.rows[0][0]) == 111
    assert result.additive is True
    assert result.non_additive_reason is None


def test_the_agent_is_told_not_to_total_a_grouped_level(g):
    """Through the agent's own tool rather than by reading the plan: the caveat
    is attached by CODE and only when `additive` is False, and `prompt.py`
    tells the model to total otherwise. So the flag being wrong ended in a
    sanctioned "total inventory: 1153", and this pins that path closed."""
    from grain.agent import tools

    text, is_error = tools.run(g, {"object": "Inventory", "group_by": ["as_of"],
                                   "metrics": ["inventory_level"]})
    assert not is_error
    assert "NOT ADDITIVE" in text
    assert "do NOT add them together" in text


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


# -- a stock across a fan ----------------------------------------------------
#
# The pack declares Track and InvoiceLine of its own so these shapes exist at
# all. Until it did, every query reaching `daily_inventory` was rooted on it
# with no traversal, and the interaction most likely to be wrong -- a window
# against a replicating join -- had no measured anchor.
#
# The result is that no wrong number can be constructed here, and the reason is
# worth pinning rather than assuming: the guard fires on STRATEGY, not on
# arithmetic. A fan beyond the metric's grain forces aggregate_then_join, which
# a window cannot reach, so it is refused; and the only way to avoid that
# strategy is to pin the fanning edge by a unique key, which leaves exactly one
# grain row per group -- so the window and the replication never meet. Anyone
# changing that guard should find out here.

FAN = [Hop(link="Inventory_Track"), Hop(link="Track_InvoiceLines")]


def test_a_stock_across_an_unpinned_fan_is_refused(g):
    """`Track_InvoiceLines` fans beyond `daily_inventory`, forcing
    aggregate-then-join, which pre-aggregates in a subquery built fresh from the
    root and so cannot carry the window."""
    from grain.engine.errors import GrainError

    with pytest.raises(GrainError, match="aggregate-then-join") as exc:
        g.query(QuerySpec(object="Inventory", traverse=FAN,
                          metrics=["inventory_level"], limit=None))
    assert any(a.startswith("group_by ") for a in exc.value.alternatives)


def test_the_alternative_the_refusal_names_actually_resolves_it(g):
    """Checked by USING the advice, not by matching its text — the distinction
    that a recursive-traversal bug in this same guard turned on. Pinned by the
    fanning edge's own unique key, each group holds one snapshot row, and each
    windows to its own track's latest instant: 4, 7, 7, 100, 999 over five
    invoice lines of tracks 1-4."""
    from grain.engine.errors import GrainError

    with pytest.raises(GrainError) as exc:
        g.query(QuerySpec(object="Inventory", traverse=FAN,
                          metrics=["inventory_level"], limit=None))
    key = next(a for a in exc.value.alternatives if a.startswith("group_by ")).split("'")[1]
    assert key == "Track_InvoiceLines.id"

    rows = g.query(QuerySpec(object="Inventory", traverse=FAN, group_by=[key],
                             metrics=["inventory_level"], limit=None)).rows
    assert {int(r[0]): int(r[1]) for r in rows} == {579: 4, 1: 7, 1154: 7,
                                                   1728: 100, 2: 999}


def test_a_stock_reached_from_the_other_side_of_the_fan(g):
    """Rooted on Track instead, the fan is Track -> Inventory, which lands ON
    the metric's grain rather than beyond it — so no pin is needed and each
    track windows to its own latest snapshot. Track 4, absent from the global
    boundary, keeps its 999."""
    rows = g.query(QuerySpec(object="Track", traverse=[Hop(link="Track_Inventory")],
                             group_by=["name"], metrics=["inventory_level"],
                             limit=None)).rows
    assert sorted(int(r[1]) for r in rows) == [4, 7, 100, 999]
    assert {r[0] for r in rows} == {
        "For Those About To Rock (We Salute You)", "Balls to the Wall",
        "Fast As a Shark", "Restless and Wild"}


@pytest.mark.parametrize("root,links,group_prop,group_by", [
    ("Inventory", ["Inventory_Track", "Track_InvoiceLines"],
     ("invoice_line", "invoice_line_id"), ["Track_InvoiceLines.id"]),
    ("Track", ["Track_Inventory"], ("track", "name"), ["name"]),
])
def test_the_oracle_agrees_across_a_fan(g, oracle_db, root, links, group_prop, group_by):
    """The figures above are hand-computable, but hand-computing is the weaker
    check and this repo has a scar from writing comparison SQL that fanned. The
    oracle replicates the join in Python, dedupes grain rows on their COMPOSITE
    key, then windows — so it sees the replication these shapes exist to test
    and removes it independently of anything the engine does."""
    from oracle import answer

    rows = g.query(QuerySpec(object=root, traverse=[Hop(link=x) for x in links],
                             group_by=group_by, metrics=["inventory_level"],
                             limit=None)).rows
    truth = answer(oracle_db, obj=root, links=links, group_props=[group_prop],
                   metric_name="inventory_level")
    assert {(r[0],): int(r[1]) for r in rows} == {k: int(v) for k, v in truth.items()}


# -- the oracle's own dedup key ----------------------------------------------

def test_the_oracle_dedupes_on_the_whole_composite_key(oracle_db):
    """`daily_inventory` is keyed (track_id, as_of_date). Registering
    `track_id` alone in the oracle's `PK` would not fail — it would keep one row
    per track and answer a smaller question, and on data where each track's
    physically-last row happens to be its boundary row it would still return
    111 and look right.

    So the assertion is on the count of RETAINED rows, which no arrangement of
    the data can make accidentally correct: eight snapshots in, eight distinct
    grain rows out. A narrowed key gives four, below."""
    from oracle import distinct_grain_rows, walk

    groups = distinct_grain_rows(walk(oracle_db, "daily_inventory", []),
                                 "daily_inventory", [])
    assert len(groups[()]) == 8


def test_a_narrowed_key_would_silently_answer_a_smaller_question(oracle_db, monkeypatch):
    """The negative half, and the reason the positive one is phrased as a
    count: with the key narrowed the oracle still returns a number, still
    returns it for every group, and raises nothing."""
    import oracle

    monkeypatch.setitem(oracle.PK, "daily_inventory", "track_id")
    groups = oracle.distinct_grain_rows(oracle.walk(oracle_db, "daily_inventory", []),
                                        "daily_inventory", [])
    assert len(groups[()]) == 4
    assert oracle.answer(oracle_db, "Inventory", [], [], "inventory_level")[()] is not None
