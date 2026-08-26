"""The loader's two newest jobs: checking what an ontology DECLARES about
cardinality and uniqueness, and refusing a metric expression token it cannot
classify. Both are regression suites for Criticals found by whole-branch review
(C4, C5) plus the C2 declaration they depend on.
"""
import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table

from grain.engine.errors import OntologyError
from grain.engine.loader import load_ontology_from_string


@pytest.fixture
def metadata():
    """Real primary keys, because every check here is a check against them.

    A fixture with no keys would let `cardinality: many_to_one` and
    `unique: true` pass unverifiably — which is the failure mode these tests
    exist to close, so it cannot be the shape of their fixture.
    """
    md = MetaData()
    Table(
        "invoice", md,
        Column("invoice_id", Integer, primary_key=True),
        Column("total", Numeric, nullable=False),
        Column("customer_id", Integer, nullable=False),
    )
    Table(
        "invoice_line", md,
        Column("invoice_line_id", Integer, primary_key=True),
        Column("invoice_id", Integer, nullable=False),
        Column("unit_price", Numeric, nullable=False),
        Column("quantity", Integer, nullable=False),
    )
    Table(
        "customer", md,
        Column("customer_id", Integer, primary_key=True),
        Column("country", String, nullable=False),
        Column("region_id", Integer),
    )
    # A legitimate spanned table: region_id is region's PK, so at most one
    # region per customer.
    Table(
        "region", md,
        Column("region_id", Integer, primary_key=True),
        Column("name", String, nullable=False),
    )
    # NOT a legitimate spanned table: `customer_id` is not unique on note, so
    # joining it replicates the customer.
    Table(
        "note", md,
        Column("note_id", Integer, primary_key=True),
        Column("customer_id", Integer, nullable=False),
        Column("body", String, nullable=False),
    )
    return md


BASE = """
name: tiny
objects:
  Customer:
    primary: customer
    joins:
      region:
        to: region
        kind: left
        cardinality: many_to_one
        on: [{from: customer.region_id, to: region.region_id}]
    properties:
      id: {column: customer.customer_id, type: integer, unique: true}
      country: {column: customer.country, type: string}
      region: {column: region.name, type: string, via: region, nullable: true}
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


def test_the_baseline_ontology_loads(metadata):
    onto = load_ontology_from_string(BASE, metadata)
    assert onto.objects["Customer"].joins["region"].cardinality == "many_to_one"
    assert onto.objects["Customer"].properties["id"].unique is True


# --------------------------------------------------------------- C5


def test_an_object_join_without_a_cardinality_will_not_load(metadata):
    """C5. `TableJoin.cardinality` has no default ON PURPOSE.

    The defect was not a wrong default, it was the absence of the field: every
    object join was silently assumed many_to_one, so the engine's claim to decide
    from declared cardinality alone was false for a whole class of join. A
    default would restore exactly that silence, so the field is required and this
    test is what stops someone giving it one for convenience.
    """
    bad = BASE.replace("        cardinality: many_to_one\n", "")
    with pytest.raises(Exception) as excinfo:
        load_ontology_from_string(bad, metadata)
    assert "cardinality" in str(excinfo.value)


def test_a_fanning_object_join_is_refused_and_names_links_as_the_alternative(metadata):
    """C5. A fanning object join replicated the object's OWN rows at no declared
    grain — measured on a two-join fixture: `sum(thing.weight)` returned 6 for a
    true 3, reported `additive: true` with no rewrites. `joins` means 'one row of
    this object'; a fanning table is a different grain, which is what links are
    for."""
    bad = BASE.replace("cardinality: many_to_one", "cardinality: one_to_many")
    with pytest.raises(OntologyError, match="may not fan out") as excinfo:
        load_ontology_from_string(bad, metadata)
    assert "link" in str(excinfo.value)


def test_a_non_fanning_claim_must_be_backed_by_a_key_in_the_database(metadata):
    """C5. Declaring the cardinality is only half the fix — a declaration nothing
    checks is the same silent assumption with a field name attached. `note` has
    no unique constraint on customer_id, so this join fans however it is
    declared, and the database is the authority that says so."""
    bad = BASE.replace(
        """      region:
        to: region
        kind: left
        cardinality: many_to_one
        on: [{from: customer.region_id, to: region.region_id}]""",
        """      notes:
        to: note
        kind: left
        cardinality: many_to_one
        on: [{from: customer.customer_id, to: note.customer_id}]""",
    ).replace(
        "      region: {column: region.name, type: string, via: region, nullable: true}",
        "      note: {column: note.body, type: string, via: notes}",
    )
    with pytest.raises(OntologyError, match="not a primary key"):
        load_ontology_from_string(bad, metadata)


# --------------------------------------------------------------- C2's declaration


def test_unique_must_be_backed_by_a_key_in_the_database(metadata):
    """C2. `grain.analyse` refuses a non-additive query that groups by no unique
    key, so this flag decides whether a question is answerable. A wrong one
    silently re-authorises the double-count it exists to prevent."""
    bad = BASE.replace(
        "country: {column: customer.country, type: string}",
        "country: {column: customer.country, type: string, unique: true}",
    )
    with pytest.raises(OntologyError, match="unique"):
        load_ontology_from_string(bad, metadata)


def test_unique_may_not_be_declared_on_a_spanned_property(metadata):
    """A spanned table's key identifies ITS row, not this object's."""
    bad = BASE.replace(
        "region: {column: region.name, type: string, via: region, nullable: true}",
        "region: {column: region.region_id, type: integer, via: region, unique: true}",
    )
    with pytest.raises(OntologyError, match="via|join"):
        load_ontology_from_string(bad, metadata)


# --------------------------------------------------------------- C4


def test_an_uppercase_foreign_column_reference_is_rejected(metadata):
    """C4, the headline. `METRIC_COLUMN_TOKEN` and `BARE_WORD` both used to start
    `[a-z_]`, so an all-uppercase identifier matched NEITHER: not grain-checked,
    not existence-checked, rendered verbatim, folded by Postgres and run. A
    metric at invoice_line grain reading `sum(INVOICE.TOTAL)` loaded without
    complaint and returned 20848.62 against a true 2328.60 — the 8.95x
    over-count, through the one door this loader exists to guard."""
    bad = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(INVOICE.TOTAL)"',
    )
    with pytest.raises(OntologyError, match="INVOICE.TOTAL"):
        load_ontology_from_string(bad, metadata)


def test_a_mixed_case_foreign_column_reference_is_rejected_coherently(metadata):
    """Same defect, and its incoherent message: `sum(Invoice.Total)` used to
    report "references 'nvoice' unqualified" — BARE_WORD matched from the SECOND
    character. The error must name the token the author actually wrote."""
    bad = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(Invoice.Total)"',
    )
    with pytest.raises(OntologyError, match="Invoice.Total"):
        load_ontology_from_string(bad, metadata)


def test_an_uppercase_reference_to_the_grains_own_column_is_accepted(metadata):
    """Case folding must not become case rejection: SQL identifiers are
    case-insensitive, so this names the same column the lowercase form does and
    is a legal, if shouty, way to write the metric."""
    good = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(INVOICE_LINE.QUANTITY)"',
    )
    onto = load_ontology_from_string(good, metadata)
    assert onto.metrics["revenue"].expr == "sum(INVOICE_LINE.QUANTITY)"


def test_a_token_no_classifier_recognised_is_an_error_not_a_pass(metadata):
    """The general form of C4. It is enough for ONE token to match no regex for
    the whole guard to be decorative, so an unclassified token must fail rather
    than fall through to the SQL renderer."""
    bad = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(invoice_line.quantity + 2quantity)"',
    )
    with pytest.raises(OntologyError):
        load_ontology_from_string(bad, metadata)


def test_a_schema_qualified_token_is_rejected_rather_than_half_read(metadata):
    """`public.invoice_line.quantity` reads as a grain-matching `public.invoice_
    line` plus a dangling `.quantity`. Ambiguous, so refused."""
    bad = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(public.invoice_line.quantity)"',
    )
    with pytest.raises(OntologyError):
        load_ontology_from_string(bad, metadata)


def test_a_windowed_metric_loads(metadata):
    """The friction half of C4: `SQL_WORDS` omitted every window-frame keyword,
    so a perfectly legal windowed metric was rejected at load. Rejecting valid
    SQL is not safety."""
    good = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(invoice_line.quantity) over (rows between unbounded '
        'preceding and current row)"',
    )
    assert load_ontology_from_string(good, metadata).metrics["revenue"]


def test_a_float_literal_with_an_exponent_is_not_read_as_an_identifier(metadata):
    """Numbers are masked before either classifier runs, so the `e` of `1.5e3`
    can be neither a bare word nor unclassified residue."""
    good = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(invoice_line.quantity * 1.5e3)"',
    )
    assert load_ontology_from_string(good, metadata).metrics["revenue"]


def test_an_unqualified_column_is_still_rejected(metadata):
    """The original invariant, unweakened by the case-folding rewrite."""
    bad = BASE.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(quantity)"',
    )
    with pytest.raises(OntologyError, match="unqualified"):
        load_ontology_from_string(bad, metadata)
