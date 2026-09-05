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
# 2. A quantity that was never additive — NO LONGER A LIMITATION.
# --------------------------------------------------------------------------
#
# This section used to hold `test_neither_engine_knows_a_price_is_not_a_quantity`,
# which proved that `sum(track.unit_price)` came back arithmetically perfect,
# flagged `additive: true`, and answered no question — in BOTH engines, with no
# caveat anywhere. It was the sharpest limitation here and the most dangerous,
# because nothing about the output looked wrong.
#
# It is fixed. A `quantity` declaration on the property now says whether the
# number accumulates, and the loader refuses a metric that sums one which does
# not. The test was DELETED rather than inverted, which is this repo's stated
# bar for claiming a pinned limitation is closed. What replaced it lives in
# tests/unit/test_quantity_kind.py, and the one below proves the refusal
# reaches the facade in both engines.


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_a_metric_summing_a_rate_is_refused_before_either_engine_sees_it(
    which, db_engine
):
    """The check sits in the loader, below the engine seam, so neither engine
    can be handed such a metric in the first place."""
    from grain.engine.ontology import ObjectType, Property

    g = Grain.load(CHINOOK_DIR, db_engine, engine_name=which)
    onto = g.ontology.model_copy(deep=True)
    onto.objects["Track"] = ObjectType(
        name="Track", primary="track",
        properties={"unit_price": Property(column="track.unit_price",
                                           type="decimal", quantity="rate")},
    )
    onto.metrics["price_sum"] = Metric(
        name="price_sum", grain="track", type="decimal",
        agg="sum", value="track.unit_price")

    from grain.engine.errors import OntologyError
    from grain.engine.loader import validate

    with pytest.raises(OntologyError, match="does not accumulate"):
        validate(onto, g.metadata)


# --------------------------------------------------------------------------
# 3. Aggregates with no distinct-sum rewrite.
# --------------------------------------------------------------------------

# `test_a_median_cannot_be_declared_structurally` stood here. It asserted that
# AggFunc offered no median, on the reasoning that an order statistic has no
# distinct-sum rewrite so none could be offered honestly.
#
# The reasoning was sound and the conclusion too broad. It rules out THAT
# rewrite, not every encoding: packing the value into the high digits of a
# numeric and the key into the low ones gives an orderable scalar that DISTINCT
# deduplicates correctly. Deleted rather than inverted, per this file's own bar.


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


# --------------------------------------------------------------------------
# 6. The subquery engine's pre-aggregate ignores downstream filtering.
# --------------------------------------------------------------------------

def test_the_subquery_pre_aggregate_ignores_the_downstream_join(db_engine):
    """A real defect in the DEFAULT engine, found by the differential harness
    plus the oracle, and pinned here rather than quietly fixed.

    `_aggregate_then_join` walks only enough edges to reach the metric's grain.
    That is deliberate and correct as far as it goes: applying the downstream
    FANNING edges would replicate the grain's own rows inside the very subquery
    built to stop that (defect C5).

    But not walking an edge also means not FILTERING by it. `Track ->
    Track_InvoiceLines` restricts the query to tracks that actually sold; the
    pre-aggregate, walking zero edges, computes over every track sharing the
    group key — including 1519 tracks that never sold.

    Measured: 54 of 1888 name-groups differ. For 'All My Love' the subquery
    engine returns 200620 where the answer to the question asked is 356284.

    Invisible until now because chinook's summing metrics all sit at a grain
    nothing downstream eliminates. An order statistic is sensitive to exactly
    which rows are in the set, so it surfaced immediately.

    The symmetric engine is correct here — it computes over the rows present in
    the join, because it never leaves the join.
    """
    from grain.engine.spec import Hop

    spec = QuerySpec(object="Track", traverse=[Hop(link="Track_InvoiceLines")],
                     group_by=["name"], metrics=["median_duration"], limit=None)
    sub = {r[0]: r[1] for r in Grain.load(
        CHINOOK_DIR, db_engine, engine_name="subquery").query(spec).rows}
    sym = {r[0]: r[1] for r in Grain.load(
        CHINOOK_DIR, db_engine, engine_name="symmetric").query(spec).rows}

    with db_engine.connect() as conn:
        truth = {r[0]: r[1] for r in conn.execute(text("""
            select t.name,
                   percentile_disc(0.5) within group (order by t.milliseconds)
            from (select distinct track_id, name, milliseconds from track) t
            where exists (select 1 from invoice_line il
                          where il.track_id = t.track_id)
            group by t.name"""))}

    assert set(sub) == set(sym) == set(truth), "the GROUP set is right in both"
    differing = [k for k in truth if int(sub[k]) != int(truth[k])]
    assert len(differing) == 54, "if this changes, the defect changed"
    assert all(int(sym[k]) == int(truth[k]) for k in truth), \
        "the symmetric engine computes over the joined rows and is correct"
