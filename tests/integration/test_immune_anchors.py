"""Going inline is only allowed if it returns the SAME number the subquery
returned. These assert measured figures against the live database, compared
against SQL written by hand — so a wrong inline result cannot hide behind the
engine agreeing with itself.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration

BOTH_HOPS = [Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")]


@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)


def test_customer_count_by_country_matches_hand_written_sql(g):
    """`customer_count` is measured at the root and this traversal fans twice,
    so every customer row is replicated once per invoice line. The inline
    aggregate is correct anyway, because DISTINCT undoes exactly that."""
    got = {r[0]: r[1] for r in g.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["customer_count"],
        traverse=BOTH_HOPS, limit=None)).rows}
    with g.engine.connect() as conn:
        truth = {r[0]: r[1] for r in conn.execute(text("""
            select c.country, count(distinct c.customer_id)
            from customer c
            join invoice i on i.customer_id = c.customer_id
            join invoice_line il on il.invoice_id = i.invoice_id
            group by c.country
        """))}
    assert got == truth


def test_the_inline_plan_agrees_with_what_the_subquery_plan_returned(g):
    """The strongest form: the same figures the rewrite produced before this
    change. `count(distinct pk)` pre-aggregated at its own grain and
    `count(distinct pk)` computed inline over the fanned join are the same
    number, and that equality is the entire licence for going inline."""
    spec = QuerySpec(object="Customer", group_by=["country"],
                     metrics=["customer_count"], traverse=BOTH_HOPS, limit=None)
    result = g.query(spec)
    assert result.rewrites == []
    with g.engine.connect() as conn:
        pre_aggregated = {r[0]: r[1] for r in conn.execute(text("""
            select c.country, agg.n
            from customer c
            join invoice i on i.customer_id = c.customer_id
            join invoice_line il on il.invoice_id = i.invoice_id
            left join (
              select c2.country, count(distinct c2.customer_id) n
              from customer c2 group by c2.country
            ) agg on agg.country is not distinct from c.country
            group by c.country, agg.n
        """))}
    assert {r[0]: r[1] for r in result.rows} == pre_aggregated


def test_no_subquery_is_emitted_for_an_immune_metric(g):
    """The point of the change: the SQL is simpler, not just as correct."""
    sql = g.explain(QuerySpec(
        object="Customer", group_by=["country"], metrics=["customer_count"],
        traverse=BOTH_HOPS, limit=None))["compiled_sql"]
    assert "customer_count_at_customer" not in sql
    assert sql.lower().count("select") == 1


def test_a_summing_metric_still_emits_its_subquery(g):
    """The control, at the SQL level."""
    sql = g.explain(QuerySpec(
        object="Customer", group_by=["country"], metrics=["invoice_total"],
        traverse=BOTH_HOPS, limit=None))["compiled_sql"]
    assert sql.lower().count("select") > 1
