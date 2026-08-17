import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table

from grain.engine.errors import OntologyError
from grain.engine.loader import load_ontology_from_string


@pytest.fixture
def metadata():
    md = MetaData()
    Table("invoice", md, Column("invoice_id", Integer), Column("total", Numeric),
          Column("customer_id", Integer))
    Table("invoice_line", md, Column("invoice_line_id", Integer),
          Column("invoice_id", Integer), Column("unit_price", Numeric),
          Column("quantity", Integer))
    Table("customer", md, Column("customer_id", Integer), Column("country", String))
    return md


GOOD = """
name: tiny
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}
links:
  Customer_Invoices:
    from: Customer
    to: Invoice
    kind: direct
    on: [{from: customer.customer_id, to: invoice.customer_id}]
    cardinality: one_to_many
metrics:
  revenue:
    grain: invoice_line
    expr: "sum(invoice_line.unit_price * invoice_line.quantity)"
    type: decimal
"""


def test_good_ontology_loads(metadata):
    onto = load_ontology_from_string(GOOD, metadata)
    assert onto.name == "tiny"
    assert onto.metrics["revenue"].grain == "invoice_line"
    assert onto.links["Customer_Invoices"].fans_out is True


def test_unknown_column_fails_at_load_naming_the_column(metadata):
    bad = GOOD.replace("customer.country", "customer.contry")
    with pytest.raises(OntologyError, match="contry"):
        load_ontology_from_string(bad, metadata)


def test_unknown_table_fails_at_load_naming_the_table(metadata):
    bad = GOOD.replace("grain: invoice_line", "grain: invoice_lines")
    with pytest.raises(OntologyError, match="invoice_lines"):
        load_ontology_from_string(bad, metadata)


def test_metric_referencing_a_foreign_table_is_rejected(metadata):
    bad = GOOD.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(invoice.total)"',
    )
    with pytest.raises(OntologyError, match="invoice.total"):
        load_ontology_from_string(bad, metadata)


def test_link_naming_an_undeclared_object_is_rejected(metadata):
    bad = GOOD.replace("to: Invoice", "to: Invoicee")
    with pytest.raises(OntologyError, match="Invoicee"):
        load_ontology_from_string(bad, metadata)
