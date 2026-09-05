"""Order statistics, measured against the unfanned truth.

Every figure is compared with `percentile_disc` over the un-joined table, so a
wrong encoding cannot hide behind the engine agreeing with itself. The naive
figure must DIFFER from the truth or the test proves nothing.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration

FANNED = [Hop(link="Playlist_Tracks")]


@pytest.fixture(scope="module")
def engines(db_engine):
    return {n: Grain.load(CHINOOK_DIR, db_engine, engine_name=n)
            for n in ("subquery", "symmetric")}


def test_the_join_really_corrupts_the_median(db_engine):
    """The control. Without this the rest of the module proves nothing."""
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.5) within group (order by milliseconds)"
            " from track")).scalar()
        naive = conn.execute(text(
            "select percentile_disc(0.5) within group (order by t.milliseconds)"
            " from track t join playlist_track pt on pt.track_id = t.track_id"
        )).scalar()
    assert truth == 255634
    assert naive != truth, "the join must actually fan out"


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_median_over_a_fanning_join_is_exact(which, engines, db_engine):
    """Both engines, by completely different routes: one pre-aggregates at the
    grain and calls percentile_disc, the other sorts an encoded array."""
    result = engines[which].query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["median_duration"], limit=None))
    # Every playlist reaches a different subset of tracks, so compare the one
    # group that provably contains all of them.
    got = max(r[1] for r in result.rows)
    with db_engine.connect() as conn:
        truth = conn.execute(text("""
            select max(m) from (
              select percentile_disc(0.5) within group (order by t.milliseconds) m
              from playlist p
              join playlist_track pt on pt.playlist_id = p.playlist_id
              join track t on t.track_id = pt.track_id
              group by p.playlist_id) s""")).scalar()
    assert int(got) == int(truth)


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_p90_over_a_fanning_join_is_exact(which, engines, db_engine):
    result = engines[which].query(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["p90_duration"], limit=None))
    got = max(r[1] for r in result.rows)
    with db_engine.connect() as conn:
        truth = conn.execute(text("""
            select max(m) from (
              select percentile_disc(0.9) within group (order by t.milliseconds) m
              from playlist p
              join playlist_track pt on pt.playlist_id = p.playlist_id
              join track t on t.track_id = pt.track_id
              group by p.playlist_id) s""")).scalar()
    assert int(got) == int(truth)


def test_a_decimal_valued_order_statistic_is_exact(db_engine):
    """The case an integer-only design would have refused. unit_price is
    NUMERIC(10,2) and the scale comes from reflection, so nothing rounds."""
    from grain.engine.ontology import Metric

    g = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")
    g.ontology.metrics["median_price"] = Metric(
        name="median_price", grain="invoice_line", type="decimal",
        agg="median", value="invoice_line.unit_price")
    try:
        result = g.query(QuerySpec(
            object="InvoiceLine", metrics=["median_price"], limit=None))
    finally:
        del g.ontology.metrics["median_price"]
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.5) within group (order by unit_price)"
            " from invoice_line")).scalar()
    assert float(result.rows[0][0]) == float(truth)


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_grouped_medians_match_group_by_group(which, engines, db_engine):
    """A single figure can be right by luck; several hundred cannot."""
    got = {r[0]: int(r[1]) for r in engines[which].query(QuerySpec(
        object="Album", traverse=[Hop(link="Album_Tracks")],
        group_by=["title"], metrics=["median_duration"], limit=None)).rows}
    with db_engine.connect() as conn:
        truth = {r[0]: int(r[1]) for r in conn.execute(text("""
            select a.title,
                   percentile_disc(0.5) within group (order by t.milliseconds)
            from album a join track t on t.album_id = a.album_id
            group by a.title"""))}
    assert got == truth


def test_the_symmetric_plan_emits_no_subquery(engines):
    sql = engines["symmetric"].explain(QuerySpec(
        object="Playlist", traverse=FANNED, group_by=["id"],
        metrics=["median_duration"], limit=None))["compiled_sql"]
    assert sql.lower().count("select") == 1
    assert "array_agg" in sql.lower()


def test_the_two_engines_emit_genuinely_different_sql(engines):
    """What makes their agreement evidence rather than tautology.

    NOT asserted: that the subquery engine emits a subquery. With the grain at
    the END of the path nothing fans downstream of it, so that engine correctly
    goes inline and calls percentile_disc directly — which my first draft of
    this test assumed away. The claim that matters is that the two arrive at the
    same number by different routes, not that either takes a particular one.
    """
    spec = QuerySpec(object="Playlist", traverse=FANNED, group_by=["id"],
                     metrics=["median_duration"], limit=None)
    sym = engines["symmetric"].explain(spec)["compiled_sql"]
    sub = engines["subquery"].explain(spec)["compiled_sql"]

    assert "array_agg" in sym.lower()
    assert "array_agg" not in sub.lower()
    assert "percentile_disc" in sub.lower()
    assert "percentile_disc" not in sym.lower()
    assert sym != sub

    rows_sym = engines["symmetric"].query(spec).rows
    rows_sub = engines["subquery"].query(spec).rows
    assert {r[0]: int(r[1]) for r in rows_sym} == {r[0]: int(r[1]) for r in rows_sub}


def test_the_subquery_engine_refuses_a_median_with_no_group_key(engines):
    """A pre-existing over-broad refusal, recorded rather than worked around.

    `NonAdditiveRefused` says "summing it double-counts", which is exactly right
    for a sum: pre-aggregating revenue over a many-to-many with no grouping
    really would return 5738.28 for a true 2328.60. It is not right for an
    aggregate that never accumulates — a median over the distinct tracks
    reachable is well defined however many playlists reach them, and the
    symmetric engine returns it correctly.

    It predates order statistics: `count_distinct` is refused in the same shape.
    Narrowing it means touching the C1/C2 machinery, so it is left alone here
    and recorded as a real divergence instead.
    """
    from grain.engine.errors import NonAdditiveRefused

    spec = QuerySpec(object="Playlist", traverse=FANNED,
                     metrics=["median_duration"], limit=None)
    with pytest.raises(NonAdditiveRefused):
        engines["subquery"].query(spec)
    assert engines["symmetric"].query(spec).rows[0][0] is not None
