"""Every aggregate in the taxonomy, executed against the database.

The chinook pack only declares `sum` and `count_distinct`, so the other three
branches of `symmetric_expr` were reachable only through unit tests that
inspected the SQL string — they had never actually run. A branch that compiles
is not a branch that returns the right number.

Each case is run over a deliberately fanning join
(`invoice_line -> track -> playlist_track`, which turns 2240 rows into 5572) and
compared against the same aggregate over the unfanned table.
"""
import pytest
from sqlalchemy import func, select, text

from grain.engine.ontology import Metric
from grain.engine_symmetric.symmetric import symmetric_expr

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def fanned(chinook_metadata):
    """invoice_line joined out to playlist_track — 2240 rows become 5572."""
    md = chinook_metadata
    il, tr, pt = md.tables["invoice_line"], md.tables["track"], md.tables["playlist_track"]
    return il.join(tr, tr.c.track_id == il.c.track_id).join(
        pt, pt.c.track_id == tr.c.track_id
    )


def test_the_join_really_does_fan_out(db_engine, chinook_metadata, fanned):
    """The control for every case below. If this stops fanning, the rest of this
    module proves nothing."""
    with db_engine.connect() as conn:
        rows = conn.execute(select(func.count()).select_from(fanned)).scalar()
        true_rows = conn.execute(text("select count(*) from invoice_line")).scalar()
    assert rows > true_rows


def _run(conn, md, fanned, agg, typ, value):
    m = Metric(name=f"t_{agg}", grain="invoice_line", type=typ, agg=agg, value=value)
    return conn.execute(
        select(symmetric_expr(m, md).label("v")).select_from(fanned)
    ).scalar()


def test_sum_recovers_the_unfanned_total(db_engine, chinook_metadata, fanned):
    with db_engine.connect() as conn:
        got = _run(conn, chinook_metadata, fanned, "sum", "integer",
                   "invoice_line.quantity")
        truth = conn.execute(text("select sum(quantity) from invoice_line")).scalar()
        naive = conn.execute(
            select(func.sum(chinook_metadata.tables["invoice_line"].c.quantity))
            .select_from(fanned)).scalar()
    assert naive != truth
    assert got == truth


def test_count_counts_rows_not_replicas(db_engine, chinook_metadata, fanned):
    """`count(*)` over a fanned join counts replicas; the symmetric form counts
    distinct grain rows."""
    with db_engine.connect() as conn:
        got = _run(conn, chinook_metadata, fanned, "count", "integer",
                   "invoice_line.quantity")
        truth = conn.execute(text("select count(*) from invoice_line")).scalar()
        naive = conn.execute(select(func.count()).select_from(fanned)).scalar()
    assert naive != truth
    assert got == truth


def test_avg_is_the_unfanned_mean(db_engine, chinook_metadata, fanned):
    """The case most likely to be quietly wrong: the numerator dedupes but the
    divisor must dedupe too, or the mean is computed over replicas."""
    with db_engine.connect() as conn:
        got = _run(conn, chinook_metadata, fanned, "avg", "decimal",
                   "invoice_line.unit_price")
        truth = conn.execute(text("select avg(unit_price) from invoice_line")).scalar()
        naive = conn.execute(
            select(func.avg(chinook_metadata.tables["invoice_line"].c.unit_price))
            .select_from(fanned)).scalar()
    assert round(naive, 10) != round(truth, 10), "the fan must move the naive mean"
    assert round(got, 10) == round(truth, 10)


@pytest.mark.parametrize("agg,sql", [("min", "min"), ("max", "max")])
def test_min_and_max_are_immune_and_correct(agg, sql, db_engine, chinook_metadata,
                                            fanned):
    """Immune to replication, so these take the plain aggregate — but "immune"
    is a claim about a multiset, and it is worth one execution to confirm."""
    with db_engine.connect() as conn:
        got = _run(conn, chinook_metadata, fanned, agg, "integer",
                   "invoice_line.quantity")
        truth = conn.execute(
            text(f"select {sql}(quantity) from invoice_line")).scalar()
    assert got == truth


def test_an_immune_aggregate_emits_no_encoding(chinook_metadata):
    """Cost, not correctness: encoding a min would be wasted DISTINCT sorting."""
    m = Metric(name="m", grain="invoice_line", type="integer", agg="min",
               value="invoice_line.quantity")
    assert "1e30" not in str(symmetric_expr(m, chinook_metadata))
