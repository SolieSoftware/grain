import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Numeric, String, Table

from grain.engine.loader import load_ontology_from_string

TINY_YAML = """
name: chinook_lite
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string, nullable: true}
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
    joins:
      department:
        to: department
        kind: left
        on: [{from: employee.department_id, to: department.department_id}]
    properties:
      last_name: {column: employee.last_name, type: string}
  Track:
    primary: track
    properties:
      name: {column: track.name, type: string}
  Playlist:
    primary: playlist
    properties:
      name: {column: playlist.name, type: string, nullable: true}
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
  Track_InvoiceLines:
    from: Track
    to: InvoiceLine
    kind: direct
    on: [{from: track.track_id, to: invoice_line.track_id}]
    cardinality: one_to_many
  Playlist_Tracks:
    from: Playlist
    to: Track
    kind: through
    via: playlist_track
    on_from: [{from: playlist.playlist_id, to: playlist_track.playlist_id}]
    on_to: [{from: playlist_track.track_id, to: track.track_id}]
    cardinality: many_to_many
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
  track_count:
    grain: track
    expr: "count(distinct track.track_id)"
    type: integer
  playlist_count:
    grain: playlist
    expr: "count(distinct playlist.playlist_id)"
    type: integer
"""


@pytest.fixture(scope="session")
def lite_metadata():
    md = MetaData()
    # Nullability mirrors the real chinook schema, column for column. It is not
    # decoration: `_key_is_nullable` reads the REFLECTED flag to choose between
    # `=` and `IS NOT DISTINCT FROM` when rejoining a pre-aggregated metric, and
    # the loader refuses any declaration that under-states it. A fixture where
    # everything defaulted to nullable would exercise only one of those branches
    # and would misreport which properties may legally declare `nullable: false`.
    Table(
        "customer",
        md,
        Column("customer_id", Integer, nullable=False),
        Column("country", String),  # genuinely nullable in chinook
        Column("support_rep_id", Integer),
    )
    Table(
        "invoice",
        md,
        Column("invoice_id", Integer, nullable=False),
        Column("customer_id", Integer, nullable=False),
        Column("total", Numeric, nullable=False),
        Column("invoice_date", DateTime, nullable=False),
    )
    Table(
        "invoice_line",
        md,
        Column("invoice_line_id", Integer, nullable=False),
        Column("invoice_id", Integer, nullable=False),
        Column("unit_price", Numeric, nullable=False),
        Column("quantity", Integer, nullable=False),
        Column("track_id", Integer, nullable=False),
    )
    Table(
        "employee",
        md,
        Column("employee_id", Integer, nullable=False),
        Column("reports_to", Integer),
        Column("last_name", String, nullable=False),
        Column("department_id", Integer),
    )
    Table(
        "department",
        md,
        Column("department_id", Integer, nullable=False),
        Column("name", String, nullable=False),
    )
    Table("track", md, Column("track_id", Integer, nullable=False),
          Column("name", String, nullable=False), Column("album_id", Integer))
    Table("playlist", md, Column("playlist_id", Integer, nullable=False),
          Column("name", String))  # playlist.name is nullable in chinook
    Table("playlist_track", md, Column("playlist_id", Integer, nullable=False),
          Column("track_id", Integer, nullable=False))
    return md


@pytest.fixture(scope="session")
def chinook_lite(lite_metadata):
    return load_ontology_from_string(TINY_YAML, lite_metadata)
