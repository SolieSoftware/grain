import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table

from grain.engine.errors import OntologyError
from grain.engine.loader import load_ontology_from_string


@pytest.fixture
def metadata():
    # NOT NULL where the GOOD ontology below declares a property without
    # `nullable: true` — the loader now rejects a declaration that under-states
    # what the database says, so the two have to agree.
    md = MetaData()
    Table("invoice", md, Column("invoice_id", Integer),
          Column("total", Numeric, nullable=False), Column("customer_id", Integer))
    Table("invoice_line", md, Column("invoice_line_id", Integer),
          Column("invoice_id", Integer), Column("unit_price", Numeric),
          Column("quantity", Integer))
    Table("customer", md, Column("customer_id", Integer),
          Column("country", String, nullable=False))
    return md


GOOD = """
name: tiny
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}
  Invoice:
    primary: invoice
    properties:
      total: {column: invoice.total, type: decimal}
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


def test_bare_on_key_is_not_parsed_as_a_boolean(metadata):
    """YAML 1.1 resolves bare `on` to True. The whole join-condition schema is
    keyed on `on:`, so this must stay a string or links silently lose their
    conditions. Regression guard: do not swap in yaml.safe_load."""
    onto = load_ontology_from_string(GOOD, metadata)
    link = onto.links["Customer_Invoices"]
    assert len(link.on) == 1
    assert link.on[0].from_.qualified == "customer.customer_id"
    assert link.on[0].to.qualified == "invoice.customer_id"


def test_declaring_not_null_over_a_nullable_column_is_rejected(metadata):
    """CRITICAL-1. `compile.py` reads this flag to pick `=` over
    `IS NOT DISTINCT FROM` when rejoining a pre-aggregated metric. Under `=`, a
    NULL key silently fails to match its own group and the metric reads wrong —
    right magnitude, right sign, no error. A declaration that under-states the
    database is a wrong answer waiting for one NULL row, so it fails at load."""
    bad = GOOD.replace(
        "country: {column: customer.country, type: string}",
        "country: {column: customer.country, type: string}\n"
        "      city: {column: customer.city, type: string}",
    )
    metadata.tables["customer"].append_column(Column("city", String))  # nullable
    with pytest.raises(OntologyError, match="nullable in the database"):
        load_ontology_from_string(bad, metadata)


def test_declaring_the_column_nullable_is_accepted(metadata):
    """The rule is one-directional: a declaration may ADD nullability freely."""
    good = GOOD.replace(
        "country: {column: customer.country, type: string}",
        "country: {column: customer.country, type: string, nullable: true}",
    )
    assert load_ontology_from_string(good, metadata).objects["Customer"]


def test_a_property_through_a_left_join_may_not_declare_not_null(metadata):
    """The outer join manufactures NULLs for unmatched rows whatever the column
    itself says, so the declaration is unachievable however the DDL reads."""
    md = metadata
    Table("region", md, Column("region_id", Integer, nullable=False),
          Column("name", String, nullable=False))
    md.tables["customer"].append_column(Column("region_id", Integer))
    bad = GOOD.replace(
        """  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}""",
        """  Customer:
    primary: customer
    joins:
      region:
        to: region
        kind: left
        on: [{from: customer.region_id, to: region.region_id}]
    properties:
      country: {column: customer.country, type: string}
      region: {column: region.name, type: string, via: region}""",
    )
    with pytest.raises(OntologyError, match="left join"):
        load_ontology_from_string(bad, md)


def test_an_unqualified_column_in_a_metric_expression_is_rejected(metadata):
    """IMPORTANT-3. A bare name binds to a different column depending on the
    strategy: inline it resolves against the whole walked path, in a subquery
    against the prefix reaching the grain. Same metric, two numbers, no error.
    Full qualification is the invariant that makes rendering the expression as
    raw SQL safe at all, so it may not have a hole."""
    bad = GOOD.replace(
        "sum(invoice_line.unit_price * invoice_line.quantity)",
        "sum(unit_price * invoice_line.quantity)",
    )
    with pytest.raises(OntologyError, match="unqualified"):
        load_ontology_from_string(bad, metadata)


def test_functions_and_sql_keywords_are_not_mistaken_for_columns(metadata):
    """`count`, `distinct`, `case`/`when`/`then`/`end` are not columns. A
    qualification rule that rejected them would be unusable."""
    good = GOOD.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "count(distinct case when invoice_line.quantity > 0 '
        'then invoice_line.invoice_line_id else null end)"',
    ).replace("type: decimal\n", "type: integer\n")
    assert load_ontology_from_string(good, metadata).metrics["revenue"]


def test_a_decimal_literal_is_not_read_as_a_column(metadata):
    good = GOOD.replace(
        "sum(invoice_line.unit_price * invoice_line.quantity)",
        "sum(invoice_line.unit_price * 0.5)",
    )
    assert load_ontology_from_string(good, metadata).metrics["revenue"]


def test_a_property_named_like_a_metric_is_rejected(metadata):
    """IMPORTANT-4. A group key and a metric are labelled by their own names in
    one SELECT, and the metric subquery exposes both — equal names collide and
    SQLAlchemy raises mid-compile on a query the caller thought was legal."""
    bad = GOOD.replace(
        "country: {column: customer.country, type: string}",
        "revenue: {column: customer.country, type: string}",
    )
    with pytest.raises(OntologyError, match="same name as a declared metric"):
        load_ontology_from_string(bad, metadata)
