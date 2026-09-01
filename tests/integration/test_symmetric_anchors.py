"""Measured figures, not plan shapes.

Every number here is compared against SQL written by hand in the test itself, so
a wrong symmetric result cannot hide behind the engine agreeing with itself.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration

# Playlist -> Track -> InvoiceLine. The middle hop is many_to_many, so a naive
# sum over the far end counts each line once per playlist holding its track.
FANNED = [Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]


@pytest.fixture(scope="session")
def sym(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")


@pytest.fixture(scope="session")
def sub(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine, engine_name="subquery")


def test_the_offset_is_numeric_not_float(db_engine):
    """Condition (c) depends on `1e30` being numeric. A driver or cast that made
    it double precision would silently reintroduce inexactness."""
    with db_engine.connect() as conn:
        assert conn.execute(text("select pg_typeof(1e30)::text")).scalar() == "numeric"


def test_total_revenue_through_a_fanning_join_is_exact(sym, db_engine):
    """The whole feature in one assertion. The naive sum over this path returns
    5738.28; the true total is 2328.60."""
    rows = sym.query(QuerySpec(
        object="Playlist", traverse=FANNED, metrics=["revenue"], limit=None)).rows
    (got,) = [r[0] for r in rows]
    with db_engine.connect() as conn:
        truth = conn.execute(
            text("select sum(unit_price * quantity) from invoice_line")
        ).scalar()
        naive = conn.execute(text("""
            select sum(il.unit_price * il.quantity)
            from playlist p
            join playlist_track pt on pt.playlist_id = p.playlist_id
            join track t on t.track_id = pt.track_id
            join invoice_line il on il.track_id = t.track_id
        """)).scalar()
    assert round(naive, 2) != round(truth, 2), "the join must actually fan out"
    assert round(got, 2) == round(truth, 2)


def test_revenue_by_playlist_matches_hand_written_distinct_sql(sym, db_engine):
    """Per group, against SQL that dedupes by invoice_line_id by hand."""
    got = {r[0]: round(r[1], 2) for r in sym.query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["revenue"], limit=None)).rows}
    with db_engine.connect() as conn:
        truth = {r[0]: round(r[1], 2) for r in conn.execute(text("""
            select playlist_id, sum(unit_price * quantity) from (
              select distinct p.playlist_id, il.invoice_line_id,
                     il.unit_price, il.quantity
              from playlist p
              join playlist_track pt on pt.playlist_id = p.playlist_id
              join track t on t.track_id = pt.track_id
              join invoice_line il on il.track_id = t.track_id
            ) d group by playlist_id
        """))}
    assert got == truth


def test_a_non_unique_group_key_is_answered_not_refused(sym, db_engine):
    """Defect C2's query. The subquery engine REFUSES this, because merging two
    playlists named 'Music' made it double-count everything both held. The
    encoding dedupes by invoice_line_id, so each line is counted once per group
    however many playlists in the group hold its track — the per-group number is
    correct without the unique key that refusal was buying."""
    got = {r[0]: round(r[1], 2) for r in sym.query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["name"],
        metrics=["revenue"], limit=None)).rows}
    with db_engine.connect() as conn:
        truth = {r[0]: round(r[1], 2) for r in conn.execute(text("""
            select name, sum(unit_price * quantity) from (
              select distinct p.name, il.invoice_line_id,
                     il.unit_price, il.quantity
              from playlist p
              join playlist_track pt on pt.playlist_id = p.playlist_id
              join track t on t.track_id = pt.track_id
              join invoice_line il on il.track_id = t.track_id
            ) d group by name
        """))}
    assert got == truth


def test_that_same_query_is_still_refused_by_the_subquery_engine(sub):
    """The control. The refusal is correct for an engine that would genuinely
    double-count, and must keep firing there."""
    from grain.engine.errors import NonAdditiveRefused

    with pytest.raises(NonAdditiveRefused):
        sub.query(QuerySpec(
            object="Playlist", traverse=FANNED, group_by=["name"],
            metrics=["revenue"], limit=None))


def test_overlapping_groups_are_still_reported_non_additive(sym):
    """What symmetric aggregates do NOT fix. Each playlist's figure is right and
    their total is meaningless, because a track sits on many playlists. The
    double-counting is the question, not the SQL."""
    result = sym.query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["revenue"], limit=None))
    assert result.additive is False
    assert "will not sum to the total" in result.non_additive_reason


def test_one_pass_no_subquery(sym):
    sql = sym.explain(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["revenue"], limit=None))["compiled_sql"]
    assert sql.lower().count("select") == 1
    assert "1e30" in sql


def test_the_engine_is_reported_on_the_result(sym):
    result = sym.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["customer_count"],
        limit=5))
    assert result.engine == "symmetric"


def test_an_opaque_metric_is_refused_pointing_at_the_other_engine(db_engine):
    """The symmetric engine is a specialist, not a superset."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine.ontology import Metric

    g = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")
    g.ontology.metrics["opaque"] = Metric(
        name="opaque", grain="invoice_line", type="decimal",
        expr="sum(invoice_line.quantity)")
    try:
        with pytest.raises(MetricNotSymmetric, match="subquery"):
            g.explain(QuerySpec(object="InvoiceLine", metrics=["opaque"], limit=5))
    finally:
        del g.ontology.metrics["opaque"]
