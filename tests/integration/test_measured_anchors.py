"""Every value here was measured against the loaded database on 2026-08-17.
These are regressions against reality, not expectations invented in the abstract."""
import pytest
from decimal import Decimal
from grain.engine.compile import compile_query
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec
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
