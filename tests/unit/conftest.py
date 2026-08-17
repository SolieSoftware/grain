import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Numeric, String, Table

from grain.engine.loader import load_ontology_from_string

TINY_YAML = """
name: chinook_lite
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}
  Invoice:
    primary: invoice
    properties:
      total: {column: invoice.total, type: decimal}
  InvoiceLine:
    primary: invoice_line
    properties:
      quantity: {column: invoice_line.quantity, type: integer}
  Employee:
    primary: employee
    properties:
      last_name: {column: employee.last_name, type: string}
links:
  Customer_Invoices:
    from: Customer
    to: Invoice
    kind: direct
    on: [{from: customer.customer_id, to: invoice.customer_id}]
    cardinality: one_to_many
  Invoice_Lines:
    from: Invoice
    to: InvoiceLine
    kind: direct
    on: [{from: invoice.invoice_id, to: invoice_line.invoice_id}]
    cardinality: one_to_many
  Customer_SupportRep:
    from: Customer
    to: Employee
    kind: direct
    on: [{from: customer.support_rep_id, to: employee.employee_id}]
    cardinality: many_to_one
  Employee_Manager:
    from: Employee
    to: Employee
    kind: recursive
    on: [{from: employee.reports_to, to: employee.employee_id}]
    cardinality: many_to_one
    max_depth: 10
metrics:
  revenue:
    grain: invoice_line
    expr: "sum(invoice_line.unit_price * invoice_line.quantity)"
    type: decimal
  invoice_total:
    grain: invoice
    expr: "sum(invoice.total)"
    type: decimal
  customer_count:
    grain: customer
    expr: "count(distinct customer.customer_id)"
    type: integer
"""


@pytest.fixture(scope="session")
def lite_metadata():
    md = MetaData()
    Table(
        "customer",
        md,
        Column("customer_id", Integer),
        Column("country", String),
        Column("support_rep_id", Integer),
    )
    Table(
        "invoice",
        md,
        Column("invoice_id", Integer),
        Column("customer_id", Integer),
        Column("total", Numeric),
        Column("invoice_date", DateTime),
    )
    Table(
        "invoice_line",
        md,
        Column("invoice_line_id", Integer),
        Column("invoice_id", Integer),
        Column("unit_price", Numeric),
        Column("quantity", Integer),
    )
    Table(
        "employee",
        md,
        Column("employee_id", Integer),
        Column("reports_to", Integer),
        Column("last_name", String),
    )
    return md


@pytest.fixture(scope="session")
def chinook_lite(lite_metadata):
    return load_ontology_from_string(TINY_YAML, lite_metadata)
