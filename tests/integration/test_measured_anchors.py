"""Every value here was measured against the loaded database on 2026-08-17.
These are regressions against reality, not expectations invented in the abstract."""
import pytest
from decimal import Decimal
from sqlalchemy import text
from grain.engine.compile import compile_query
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec
from grain.engine.errors import FanOutRefused

pytestmark = pytest.mark.integration


def run(db_engine, chinook_metadata, onto, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    stmt = compile_query(rq, analyse(rq), chinook_metadata)
    with db_engine.connect() as conn:
        return conn.execute(stmt).all()


def test_revenue_at_line_grain_is_2328_60(db_engine, chinook_metadata, chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="InvoiceLine",
               metrics=["revenue"])
    assert rows[0][-1] == Decimal("2328.60")


def test_invoice_total_at_invoice_grain_is_also_2328_60(db_engine, chinook_metadata,
                                                        chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Invoice",
               metrics=["invoice_total"])
    assert rows[0][-1] == Decimal("2328.60")


def test_revenue_by_country_totals_2328_60(db_engine, chinook_metadata, chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["revenue"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sum(r[-1] for r in rows) == Decimal("2328.60")


def test_the_inflated_figure_is_unreachable(db_engine, chinook_metadata, chinook_ontology):
    """20848.62 must not be producible through any spec the engine accepts."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["invoice_total"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sum(r[-1] for r in rows) == Decimal("2328.60")
    assert sum(r[-1] for r in rows) != Decimal("20848.62")


def test_metric_whose_grain_is_off_path_refuses(chinook_ontology):
    with pytest.raises(FanOutRefused):
        analyse(resolve(QuerySpec(object="Genre", metrics=["revenue"]), chinook_ontology))


# The 24 country totals, read off the database on 2026-08-18 with a hand-written
# query that never joins invoice_line:
#   select customer.country, sum(invoice.total) from customer
#   join invoice on customer.customer_id = invoice.customer_id group by 1
REVENUE_BY_COUNTRY = {
    "Argentina": "37.62", "Australia": "37.62", "Austria": "42.62",
    "Belgium": "37.62", "Brazil": "190.10", "Canada": "303.96",
    "Chile": "46.62", "Czech Republic": "90.24", "Denmark": "37.62",
    "Finland": "41.62", "France": "195.10", "Germany": "156.48",
    "Hungary": "45.62", "India": "75.26", "Ireland": "45.62",
    "Italy": "37.62", "Netherlands": "40.62", "Norway": "39.62",
    "Poland": "37.62", "Portugal": "77.24", "Spain": "37.62",
    "Sweden": "38.62", "USA": "523.06", "United Kingdom": "112.86",
}


def _truth_by_country(conn):
    """The same numbers, recomputed from the database rather than trusted."""
    rows = conn.execute(text(
        "select customer.country, sum(invoice.total) from customer "
        "join invoice on customer.customer_id = invoice.customer_id group by 1"
    )).all()
    return {country: total for country, total in rows}


def test_every_country_is_right_not_just_the_total(db_engine, chinook_metadata,
                                                   chinook_ontology):
    """A total is a weak assertion: value moved BETWEEN countries still sums to
    2328.60. The rewrite has to be right group by group, which is what the
    fan-out actually breaks."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["invoice_total"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    got = {country: total for country, total in rows}
    assert got == {k: Decimal(v) for k, v in REVENUE_BY_COUNTRY.items()}
    with db_engine.connect() as conn:
        assert got == _truth_by_country(conn)


def test_revenue_by_country_is_right_group_by_group(db_engine, chinook_metadata,
                                                    chinook_ontology):
    """The inline strategy gets the same per-group answer as the rewritten one:
    revenue at line grain equals invoice_total at invoice grain, country by
    country, not merely in total."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["revenue"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert {c: t for c, t in rows} == {k: Decimal(v) for k, v in REVENUE_BY_COUNTRY.items()}


def test_no_metric_column_comes_back_null(db_engine, chinook_metadata, chinook_ontology):
    """Nothing is coalesced any more, so a key that fails to rejoin shows as
    NULL instead of as a plausible zero. Assert none does."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["invoice_total", "customer_count"],
               limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert rows
    assert all(value is not None for row in rows for value in row)


def test_a_nullable_group_key_still_rejoins_when_the_data_has_nulls(
    db_engine, chinook_metadata, chinook_ontology
):
    """CRITICAL-1, against real NULLs. Chinook has no NULL country, so one is
    introduced inside a rolled-back transaction: under `=` that customer's
    invoices would drop out of the answer entirely, and the total would come
    back short of 2328.60 with no error anywhere."""
    rq = resolve(QuerySpec(object="Customer", group_by=["country"],
                           metrics=["invoice_total"], limit=100,
                           traverse=[Hop(link="Customer_Invoices"),
                                     Hop(link="Invoice_Lines")]), chinook_ontology)
    stmt = compile_query(rq, analyse(rq), chinook_metadata)
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("update customer set country = null where country = 'Brazil'"))
            rows = conn.execute(stmt).all()
            by_country = {c: t for c, t in rows}
            assert None in by_country, "the NULL group vanished entirely"
            assert by_country[None] == Decimal(REVENUE_BY_COUNTRY["Brazil"])
            assert sum(t for t in by_country.values()) == Decimal("2328.60")
        finally:
            trans.rollback()


def test_filtering_across_a_self_referential_link_asks_the_right_question(
    db_engine, chinook_metadata, chinook_ontology
):
    """CRITICAL-2. Without an alias inside the EXISTS this compiled to
    `employee.reports_to = employee.employee_id` — 'employees who are their own
    manager', which is nobody. It must be the three people who report to
    Edwards: Peacock, Park and Johnson."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Employee",
               group_by=["last_name"],
               filters=[Filter(property="Employee_Manager.last_name",
                               op="eq", value="Edwards")])
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select distinct e.last_name from employee e join employee m "
            "on e.reports_to = m.employee_id where m.last_name = 'Edwards'"
        )).all()
    assert sorted(r[0] for r in rows) == ["Johnson", "Park", "Peacock"]
    assert sorted(r[0] for r in rows) == sorted(r[0] for r in truth)
