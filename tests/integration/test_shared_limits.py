"""What NEITHER engine can do.

The rest of the suite proves the engines are right. This proves where they are
not, and pins it, so that a later claim to have fixed one of these has to
delete a test rather than merely assert.

Every limitation here is documented in the BI literature as a hard one — the
distinct-sum rewrite behind symmetric aggregates only works for measures that
decompose into sums, and nothing rescues a measure that was never additive in
the first place. These tests are the local, measured version of that.
"""
from decimal import Decimal

import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.errors import NoPath
from grain.engine.ontology import Metric
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration

FANNED = [Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]


@pytest.fixture(scope="module")
def engines(db_engine):
    return {n: Grain.load(CHINOOK_DIR, db_engine, engine_name=n)
            for n in ("subquery", "symmetric")}


# --------------------------------------------------------------------------
# 1. Overlapping groups: correct per group, no correct total, either engine.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_neither_engine_can_total_overlapping_groups(which, engines, db_engine):
    """Every group is right and their sum is meaningless, in both engines.

    A track sits on many playlists, so its revenue belongs to each playlist that
    holds it. Summing the column double-counts — and that is a property of the
    QUESTION, not of the SQL. Symmetric aggregation fixes replication INSIDE a
    group; it has nothing to say about a row legitimately belonging to several.
    """
    result = engines[which].query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["revenue"], limit=None))
    sum_of_groups = sum(r[1] for r in result.rows if r[1] is not None)

    with db_engine.connect() as conn:
        true_total = conn.execute(
            text("select sum(unit_price * quantity) from invoice_line")).scalar()

    # Compared as Decimal. `Decimal("5738.28") == 5738.28` is False — the float
    # is not that number — and a float literal here would fail a passing test.
    assert round(sum_of_groups, 2) == Decimal("5738.28")
    assert round(true_total, 2) == Decimal("2328.60")
    assert sum_of_groups > true_total, "the limitation would have gone away"
    # The only thing either engine can do about it is say so.
    assert result.additive is False
    assert "will not sum to the total" in result.non_additive_reason


def test_both_engines_agree_on_the_unusable_total(engines):
    """They fail identically, which is worth pinning: a divergence here would
    mean one of them had started guessing."""
    totals = {}
    for name, g in engines.items():
        rows = g.query(QuerySpec(object="Playlist", traverse=FANNED,
                                 group_by=["id"], metrics=["revenue"],
                                 limit=None)).rows
        totals[name] = sum(r[1] for r in rows if r[1] is not None)
    assert totals["subquery"] == totals["symmetric"]


# --------------------------------------------------------------------------
# 2. A quantity that was never additive. The worst case: silently wrong.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_neither_engine_knows_a_price_is_not_a_quantity(which, db_engine):
    """`sum(track.unit_price)` is arithmetically correct and semantically
    meaningless, and BOTH engines report it `additive: true` with no caveat.

    This is the sharpest shared limitation, and the most dangerous, because
    nothing about the output looks wrong. grain validates the GRAIN of a metric
    — that its rows are not replicated — and has no concept of whether the
    QUANTITY is additive by nature. A price, a rate, a ratio, a temperature, a
    running balance: all of them sum cleanly and none of them should.

    The literature calls these non-additive and semi-additive measures. No
    distinct-sum rewrite helps, because the rewrite fixes double-counting and
    this was never double-counted — it was the wrong operation on the right
    rows.
    """
    g = Grain.load(CHINOOK_DIR, db_engine, engine_name=which)
    g.ontology.metrics["price_sum"] = Metric(
        name="price_sum", grain="track", type="decimal",
        agg="sum", value="track.unit_price")
    try:
        result = g.query(QuerySpec(
            object="Album", traverse=[Hop(link="Album_Tracks")],
            group_by=["title"], metrics=["price_sum"],
            filters=[], limit=None))
    finally:
        del g.ontology.metrics["price_sum"]

    by_title = {r[0]: r[1] for r in result.rows}
    with db_engine.connect() as conn:
        truth = {r[0]: r[1] for r in conn.execute(text(
            "select a.title, sum(t.unit_price) from album a"
            " join track t on t.album_id = a.album_id group by a.title"))}

    # Arithmetically flawless...
    assert by_title == truth
    # ...and presented without a single caveat, which is the problem.
    #
    # NOT asserted: that no rewrite is reported. The symmetric engine correctly
    # reports one — it encoded the sum, because Album_Tracks fans — and that
    # rewrite is about REPLICATION, which it handled. Nothing in either engine's
    # output has anything to say about the sum being the wrong operation.
    assert result.additive is True
    assert result.non_additive_reason is None


# --------------------------------------------------------------------------
# 3. Aggregates with no distinct-sum rewrite.
# --------------------------------------------------------------------------

def test_a_median_cannot_be_declared_structurally():
    """The taxonomy's hard boundary, enforced by the type.

    A median has no equivalent distinct-sum rewrite — you cannot recover the
    middle value of a set from sums of encoded keys — so `AggFunc` does not
    offer one. Refusing at declaration beats accepting it and being wrong.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Metric(name="m", grain="track", type="decimal", agg="median",
               value="track.milliseconds")


def test_a_median_is_answerable_only_by_the_subquery_engine(db_engine):
    """Declared as an opaque `expr` a median IS computable — by pre-aggregating
    at the grain, which is exactly what the subquery engine does. The symmetric
    engine has no subquery and no encoding for it, so it refuses.

    This one is asymmetric rather than shared, and is recorded here because it
    is the clearest case where the DEFAULT engine is strictly more capable.
    """
    from grain.engine.errors import MetricNotSymmetric

    spec = QuerySpec(object="Album", traverse=[Hop(link="Album_Tracks")],
                     group_by=["title"], metrics=["median_ms"], limit=5)
    declaration = Metric(
        name="median_ms", grain="track", type="integer",
        expr="percentile_cont(0.5) within group (order by track.milliseconds)")

    sub = Grain.load(CHINOOK_DIR, db_engine, engine_name="subquery")
    sub.ontology.metrics["median_ms"] = declaration
    assert sub.query(spec).rows, "the subquery engine should answer this"

    sym = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")
    sym.ontology.metrics["median_ms"] = declaration
    with pytest.raises(MetricNotSymmetric):
        sym.query(spec)


# --------------------------------------------------------------------------
# 4. Branching traversal — the chasm trap is not expressible.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_neither_engine_can_traverse_two_branches(which, engines):
    """`traverse` is a PATH, not a tree, so two independent children of one root
    cannot both be reached — the shape the literature calls a chasm trap.

    Both engines refuse, which is the safe outcome: a chasm trap joined naively
    produces a cartesian product between the branches. But note WHAT they refuse
    with. The second hop is read as continuing from the first, so the error
    reports that Employee has no link to Invoice — true, and not what was asked.
    The caller asked for a branch from Customer and is told about a chain.
    """
    with pytest.raises(NoPath) as exc:
        engines[which].query(QuerySpec(
            object="Customer",
            traverse=[Hop(link="Customer_SupportRep"),
                      Hop(link="Customer_Invoices")],
            group_by=["country"], metrics=["invoice_total"], limit=5))
    assert "Employee" in str(exc.value)


# --------------------------------------------------------------------------
# 5. Ordering is not implied by limit.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_a_limit_without_an_order_returns_arbitrary_rows(which, engines):
    """Both engines honour `limit` without `order_by` and return whichever rows
    the planner produced first — legal SQL, and rarely what a caller means.

    `limit_reached` is the only defence, and it says "there may be more", not
    "these are not the top ones". Two engines can legitimately return different
    rows for the identical spec, which is precisely why this is pinned: it is
    the one place their outputs may differ without either being wrong.
    """
    spec = QuerySpec(object="Album", traverse=[Hop(link="Album_Tracks")],
                     group_by=["title"], metrics=["track_count"], limit=3)
    result = engines[which].query(spec)
    assert len(result.rows) == 3
    assert result.limit_reached is True
