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
    hardcoded figure."""
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
