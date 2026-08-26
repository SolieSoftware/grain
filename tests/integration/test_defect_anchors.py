"""Measured anchors for the five Criticals found by whole-branch review on
2026-08-18 and fixed on 2026-08-24. Every number here was taken from the loaded
database, both before the fix (the wrong one, recorded in each docstring) and
after (the assertion).

These are deliberately at the FACADE, not at the compiler: four of the five
defects produced correct-looking SQL, so a test that only inspects a statement
cannot tell whether the number it returns is right.
"""
import pytest
from decimal import Decimal

from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.errors import NonAdditiveRefused
from grain.engine.spec import Filter, Hop, OrderBy, QuerySpec

pytestmark = pytest.mark.integration

TRUE_REVENUE = Decimal("2328.60")


@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)


# --------------------------------------------------------------------- C1


def test_a_non_additive_metric_with_no_group_by_is_refused(g):
    """Before: returned 5738.28 as a single unlabelled figure, `additive: False`
    attached, against a true 2328.60. The engine did the summing itself, so
    obeying the flag literally still left the caller holding a wrong number."""
    with pytest.raises(NonAdditiveRefused) as excinfo:
        g.query(QuerySpec(
            object="Playlist", group_by=[], metrics=["revenue"],
            traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))
    assert excinfo.value.alternatives


# --------------------------------------------------------------------- C2


def test_grouping_a_non_additive_metric_by_a_non_unique_key_is_refused(g):
    """Before: `revenue` by `playlist.name` returned 10 groups, of which four
    were two playlists merged — 'Music' came back as 4215.42 for exactly
    2107.71 of distinct-track revenue. Chinook ships 'Music' x2, 'Movies' x2,
    'TV Shows' x2, 'Audiobooks' x2."""
    with pytest.raises(NonAdditiveRefused):
        g.query(QuerySpec(
            object="Playlist", group_by=["name"], metrics=["revenue"], limit=None,
            traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))


def test_grouping_by_playlist_identity_separates_the_colliding_names(g, db_engine):
    """The repaired form the refusal points at. 12 groups, not 10: the two
    playlists named 'Music' are 2107.71 each, which is exactly the distinct-track
    revenue each one holds — verified here against a hand-written query per
    playlist, so a per-group error cannot cancel out in a total."""
    result = g.query(QuerySpec(
        object="Playlist", group_by=["id", "name"], metrics=["revenue"], limit=None,
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))
    assert result.additive is False
    assert "identifies one Playlist" in result.non_additive_reason

    by_id = {row[0]: row[2] for row in result.rows}
    assert len(result.rows) == len(by_id) == 12

    with db_engine.connect() as conn:
        truth = {
            pid: value
            for pid, value in conn.execute(text("""
                select p.playlist_id,
                       round(sum(il.unit_price * il.quantity), 2)
                from playlist p
                join playlist_track pt on pt.playlist_id = p.playlist_id
                join invoice_line il on il.track_id = pt.track_id
                group by p.playlist_id
            """)).all()
        }
    mismatches = {
        pid: (by_id.get(pid), value) for pid, value in truth.items()
        if by_id.get(pid) != value
    }
    assert not mismatches, mismatches
    # The specific collision the defect was found through.
    assert by_id[1] == by_id[8] == Decimal("2107.71")


# --------------------------------------------------------------------- C3


@pytest.mark.parametrize(
    "spec",
    [
        QuerySpec(object="Album", group_by=["title"], limit=None,
                  filters=[Filter(property="Album_Tracks.genre", op="eq", value="Jazz")]),
        QuerySpec(object="Album", group_by=["title"], limit=None,
                  filters=[Filter(property="Album_Tracks.media_type", op="eq",
                                  value="MPEG audio file")]),
        QuerySpec(object="Playlist", group_by=["id", "name"], limit=None,
                  filters=[Filter(property="Playlist_Tracks.genre", op="eq",
                                  value="Jazz")]),
        QuerySpec(object="Playlist", group_by=["id", "name"], limit=None,
                  filters=[Filter(property="Playlist_Tracks.media_type", op="eq",
                                  value="MPEG audio file")]),
    ],
    ids=["album/genre", "album/media_type", "playlist/genre", "playlist/media_type"],
)
def test_the_four_shipped_spanned_filter_specs_do_not_cartesian(g, spec):
    """These are the four specs in the shipped ontology that hit C3. Each emitted
    a SQLAlchemy cartesian-product warning and ran anyway. `-W error::SAWarning`
    in pyproject.toml turns that warning into a failure, so simply executing
    them is the assertion; the row count is asserted below."""
    assert g.query(spec).rows


def test_a_jazz_filter_returns_only_albums_with_a_jazz_track(g, db_engine):
    """Before: 347 albums — every album in the database — because the EXISTS
    degenerated to "this album has any track, and some genre called Jazz exists
    anywhere". A Rock-only album passed a Jazz filter."""
    rows = g.query(QuerySpec(
        object="Album", group_by=["title"], limit=None,
        filters=[Filter(property="Album_Tracks.genre", op="eq", value="Jazz")])).rows
    with db_engine.connect() as conn:
        truth = conn.execute(text("""
            select count(distinct a.title)
            from album a
            join track t on t.album_id = a.album_id
            join genre gn on gn.genre_id = t.genre_id
            where gn.name = 'Jazz'
        """)).scalar_one()
        total_albums = conn.execute(text("select count(*) from album")).scalar_one()
    assert len(rows) == truth
    # The assertion that would have failed before: the filter must actually
    # filter, not return the whole table.
    assert len(rows) < total_albums


# --------------------------------------------------------------------- C4


def test_an_uppercase_foreign_reference_cannot_reach_the_database(db_engine):
    """Before: this loaded without complaint and returned 20848.62 against a
    true 2328.60 — the 8.95x over-count, reintroduced through the loader."""
    from sqlalchemy import MetaData

    from grain.engine.errors import OntologyError
    from grain.engine.loader import load_ontology_from_string

    md = MetaData()
    md.reflect(bind=db_engine)
    smuggled = """
name: smuggle
objects:
  Invoice:
    primary: invoice
    properties:
      total: {column: invoice.total, type: decimal}
  InvoiceLine:
    primary: invoice_line
    properties:
      quantity: {column: invoice_line.quantity, type: integer}
links:
  Invoice_Lines:
    from: Invoice
    to: InvoiceLine
    kind: direct
    on: [{from: invoice.invoice_id, to: invoice_line.invoice_id}]
    cardinality: one_to_many
metrics:
  smuggled:
    grain: invoice_line
    expr: "sum(INVOICE.TOTAL)"
    type: decimal
"""
    with pytest.raises(OntologyError, match="INVOICE.TOTAL"):
        load_ontology_from_string(smuggled, md)


# --------------------------------------------------------------------- C5


def test_the_shipped_ontology_declares_cardinality_on_every_object_join(g):
    """C5's positive form. Every object join now carries a cardinality that the
    loader checked against the database's own keys, which is what makes "every
    verdict is decided from declared cardinality alone" true rather than
    aspirational."""
    joins = [
        (obj.name, name, join)
        for obj in g.ontology.objects.values()
        for name, join in obj.joins.items()
    ]
    assert joins, "the chinook pack must exercise object joins at all"
    for object_name, join_name, join in joins:
        assert join.cardinality is not None, f"{object_name}.{join_name}"
        assert join.fans_out is False, f"{object_name}.{join_name}"


# --------------------------------------------------------------------- I1


def test_top_countries_by_revenue_are_the_actual_top_countries(g, db_engine):
    """Before: `order_by` was accepted and never read, so this returned 10
    arbitrary countries — omitting USA, the largest at 523.06 — correctly
    computed, with no truncation flag to say the result was partial."""
    result = g.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["revenue"],
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        order_by=[OrderBy(key="revenue", desc=True)], limit=10))
    with db_engine.connect() as conn:
        truth = conn.execute(text("""
            select c.country, round(sum(il.unit_price * il.quantity), 2) as revenue
            from customer c
            join invoice i on i.customer_id = c.customer_id
            join invoice_line il on il.invoice_id = i.invoice_id
            group by c.country
            order by revenue desc
            limit 10
        """)).all()
    assert [tuple(r) for r in result.rows] == [tuple(r) for r in truth]
    assert result.rows[0][0] == "USA"
    assert result.limit_reached is True


def test_a_complete_result_is_not_flagged_as_truncated(g):
    """The flag has to discriminate, or it is noise: 24 countries under no limit
    is a complete answer."""
    result = g.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["revenue"],
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        limit=None))
    assert len(result.rows) == 24
    assert result.limit_reached is False
    assert sum(row[1] for row in result.rows) == TRUE_REVENUE


# --------------------------------------------------------------------- I2


def test_the_dotted_filter_population_rule_is_published(g):
    """I2 is a documentation fix, so its test is that the documentation exists
    and reaches the agent. The behaviour itself is intended: a dotted filter
    selects objects, not rows. Left unpublished, 'invoice_total where the invoice
    is over 10' returns 2328.60 over 412 invoices where a caller reading the spec
    expects 942.32 over 64."""
    rules = g.describe()["rules"]
    assert "dotted_filters" in rules
    assert "population" in rules["dotted_filters"].lower()
    # And the repair the rule names must actually work.
    row_filtered = g.query(QuerySpec(
        object="Invoice", metrics=["invoice_total"],
        filters=[Filter(property="total", op="gt", value=10)])).rows
    assert row_filtered[0][0] == Decimal("942.32")


# --------------------------------------------------------------------- I6


def test_the_sql_explain_shows_is_the_sql_query_runs(g):
    """I6. `explain()` and `query()` each resolve, analyse and compile
    independently, so nothing structurally binds the statement a caller
    inspected to the statement that ran — which is the whole provenance claim.

    What actually holds it together is that planning reads only the spec and the
    ontology, never the data or the clock, so the same spec compiles to the same
    bytes every time. That is a property worth pinning rather than assuming: it
    is what makes `explain()` an audit of the query that will run rather than of
    a query like it.
    """
    spec = QuerySpec(
        object="Customer", group_by=["country"], metrics=["revenue", "invoice_total"],
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        order_by=[OrderBy(key="revenue", desc=True)], limit=5,
    )
    explained = g.explain(spec)
    ran = g.query(spec)
    assert explained["compiled_sql"] == ran.compiled_sql
    assert explained["additive"] == ran.additive
    assert [r["forced_by"] for r in explained["rewrites"]] == [
        r.forced_by for r in ran.rewrites
    ]
    # And twice more, to catch a plan that depends on anything but its inputs.
    assert g.explain(spec)["compiled_sql"] == explained["compiled_sql"]
