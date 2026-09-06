"""Behaviour tests for `_window_to_boundary`/`_reaggregate` against the real
chinook database. `invoice.total` is treated as though it were a stock for the
purposes of this file only -- it genuinely is not (chinook has no daily
balance or level), but the window MECHANISM is agnostic to what the number
means, and `invoice`/`invoice_date`/`total` are the cheapest real columns to
exercise it against before Task 7 seeds actual stock data.

Every expected number here is computed by an independent raw-SQL oracle in
this file, not hardcoded -- so these tests need no seeded fixtures and will
not go stale as chinook's own rows change. That is deliberate: numeric anchors
against seeded stock data are a later task; this file is what makes the
*mechanism* (not yet any specific dataset) verifiable now.
"""
import pytest
from sqlalchemy import text

from grain.engine.compile import compile_query
from grain.engine.grain import analyse
from grain.engine.ontology import Metric, ObjectType, Ontology, Property
from grain.engine.resolve import resolve
from grain.engine.spec import QuerySpec

pytestmark = pytest.mark.integration


def _onto() -> Ontology:
    props = {
        "when": Property(column="invoice.invoice_date", type="datetime", time_grain="day"),
        # Property name deliberately equal to its own column name -- this is
        # the exact shape that used to raise SQLAlchemy's ambiguous-label
        # error when `_window_to_boundary` re-exposed group keys via
        # `add_columns` on top of a full-row `select(root_table)`.
        "total": Property(column="invoice.total", type="decimal", quantity="flow"),
    }
    metric = Metric(name="level", grain="invoice", type="decimal", agg="sum",
                    value="invoice.total", quantity="stock",
                    over_time={"dimension": "when", "choice": "last"})
    return Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice", properties=props)},
        metrics={"level": metric},
    )


def _run(db_engine, chinook_metadata, **kw):
    rq = resolve(QuerySpec(**kw), _onto())
    stmt = compile_query(rq, analyse(rq), chinook_metadata)
    with db_engine.connect() as conn:
        return conn.execute(stmt).all()


def test_the_global_level_matches_an_oracle_query(db_engine, chinook_metadata):
    """No group_by: the window partitions over the whole population, so the
    level is the sum of `total` on whichever invoice(s) share the single
    latest `invoice_date` -- computed independently here, not asserted as a
    hardcoded figure.

    WEAK ON ITS OWN, and measured to be: chinook holds exactly one invoice at
    the latest date and one at the earliest, so this and the `first` test below
    each sum a SINGLE row. A `row_number()`-style regression -- one arbitrary
    row per partition instead of every row tied at the boundary -- would pass
    both. `test_grouping_by_the_time_dimension_itself_matches_an_oracle` is the
    one that can fail for that reason; these two pin the ends of the window."""
    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"])
    with db_engine.connect() as conn:
        expected = conn.execute(text(
            "select sum(total) from invoice where invoice_date = "
            "(select max(invoice_date) from invoice)"
        )).scalar()
    assert expected is not None
    assert rows[0][0] == expected


def test_choice_first_matches_an_oracle_query(db_engine, chinook_metadata):
    """Same construction, the other end of the window: the earliest date
    instead of the latest."""
    onto = _onto()
    onto.metrics["level"] = Metric(
        name="level", grain="invoice", type="decimal", agg="sum",
        value="invoice.total", quantity="stock",
        over_time={"dimension": "when", "choice": "first"},
    )
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    stmt = compile_query(rq, analyse(rq), chinook_metadata)
    with db_engine.connect() as conn:
        got = conn.execute(stmt).all()[0][0]
        expected = conn.execute(text(
            "select sum(total) from invoice where invoice_date = "
            "(select min(invoice_date) from invoice)"
        )).scalar()
    assert expected is not None
    assert got == expected


def test_grouping_by_a_key_that_shares_its_own_column_name_matches_an_oracle(
    db_engine, chinook_metadata,
):
    """The duplicate-label case. Before the fix, compiling this raised
    SQLAlchemy's own `InvalidRequestError: Label name total is being renamed
    to an anonymous label due to disambiguation` -- caught only by executing a
    real query, never by a string check on the emitted SQL. This checks not
    just that it compiles, but that every group's number is right: per
    `total`-group, the level windows to that group's own latest instant."""
    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"],
                group_by=["total"], limit=None)
    got = {r[0]: r[1] for r in rows}

    with db_engine.connect() as conn:
        oracle_rows = conn.execute(text("""
            select t.total, sum(t.total)
            from invoice t
            join (
                select total, max(invoice_date) as boundary
                from invoice
                group by total
            ) b on b.total = t.total and b.boundary = t.invoice_date
            group by t.total
        """)).all()
    expected = {r[0]: r[1] for r in oracle_rows}

    assert got == expected
    # The group_by must be doing something real, not silently collapsing --
    # chinook has far more than one distinct invoice total.
    assert len(got) > 1


# -- the cases `_window_to_boundary`'s docstring claims and nothing exercised --

def _latest_tied_date(conn):
    """The most recent `invoice_date` shared by more than one invoice.

    Measured, not hardcoded: what these tests need is a boundary holding
    SEVERAL rows, and which date that is is chinook's business, not theirs.
    """
    return conn.execute(text(
        "select invoice_date from invoice group by invoice_date "
        "having count(*) > 1 order by invoice_date desc limit 1"
    )).scalar()


def test_grouping_by_the_time_dimension_itself_matches_an_oracle(
    db_engine, chinook_metadata,
):
    """The third case the docstring claims and nothing exercised: the group keys
    ARE the partition, so grouping by the time axis gives each group exactly one
    instant and the window is a no-op.

    It is also the only test here whose boundary holds MORE THAN ONE ROW. Every
    other one reaches a boundary of exactly one invoice -- measured, not assumed
    -- so `row_number() over (...) = 1`, one arbitrary row per partition instead
    of every row tied at the boundary, would satisfy them all. A tied date makes
    that regression a strictly smaller number, which is asserted on its own line
    below so it fails legibly."""
    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"],
                group_by=["when"], limit=None)
    got = {r[0]: r[1] for r in rows}

    with db_engine.connect() as conn:
        expected = {r[0]: r[1] for r in conn.execute(text(
            "select invoice_date, sum(total) from invoice group by invoice_date"
        )).all()}
        tied = _latest_tied_date(conn)
        assert tied is not None, "chinook must hold at least one tied date"
        largest_single = conn.execute(text(
            "select max(total) from invoice where invoice_date = :d"
        ), {"d": tied}).scalar()

    assert got == expected
    assert len(got) > 1
    assert got[tied] > largest_single, "every row tied at the instant must count"


def test_a_filter_moves_the_boundary(db_engine, chinook_metadata):
    """The second case the docstring claims. The window has to run over the
    FILTERED population: a filter that removes the invoices at the global latest
    date moves the boundary back to an earlier one, so a window applied before
    the filter -- or over the whole table -- gives a different number.

    Filtered on `total` rather than on the time axis because grain's
    `FilterScalar` is `str | int | float | bool`, with no date member, and the
    compiler casts a string bind to VARCHAR: `invoice_date < $1::VARCHAR` has no
    Postgres operator. A time-valued filter is simply not expressible today,
    which is a gap in the spec rather than in the window."""
    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"],
                filters=[{"property": "total", "op": "gte", "value": 5}])

    with db_engine.connect() as conn:
        expected = conn.execute(text(
            "select sum(total) from invoice where total >= 5 and invoice_date = "
            "(select max(invoice_date) from invoice where total >= 5)"
        )).scalar()
        unfiltered_boundary = conn.execute(text(
            "select max(invoice_date) from invoice"
        )).scalar()
        filtered_boundary = conn.execute(text(
            "select max(invoice_date) from invoice where total >= 5"
        )).scalar()

    assert expected is not None
    assert rows[0][0] == expected
    # Without this the test would pass even if the filter were dropped entirely.
    assert filtered_boundary < unfiltered_boundary


def test_an_empty_population_yields_no_level_rather_than_a_number(
    db_engine, chinook_metadata,
):
    """A window over nothing. `max(t) over (...)` has no rows to pick from, the
    boundary filter matches nothing, and the outer `sum` runs over an empty set.

    Pinned because 'no level' and 'a level of 0' are different answers and only
    one is honest: NULL says the population is empty, 0 asserts a level for a
    population that does not exist."""
    with db_engine.connect() as conn:
        biggest = conn.execute(text("select max(total) from invoice")).scalar()

    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"],
                filters=[{"property": "total", "op": "gt", "value": float(biggest)}])

    assert len(rows) == 1
    assert rows[0][0] is None


def test_an_empty_population_yields_no_groups_when_grouped(
    db_engine, chinook_metadata,
):
    """The grouped counterpart, where the honest answer is a different shape:
    with a group_by there are no groups at all, so no rows -- not one NULL."""
    with db_engine.connect() as conn:
        biggest = conn.execute(text("select max(total) from invoice")).scalar()

    rows = _run(db_engine, chinook_metadata, object="Invoice", metrics=["level"],
                group_by=["when"], limit=None,
                filters=[{"property": "total", "op": "gt", "value": float(biggest)}])
    assert rows == []
