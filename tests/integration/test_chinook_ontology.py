import pytest

pytestmark = pytest.mark.integration

# Fixtures chinook_metadata / chinook_ontology come from tests/conftest.py (below).


def test_all_ten_objects_declared(chinook_ontology):
    onto = chinook_ontology
    assert set(onto.objects) == {
        "Artist", "Album", "Track", "Genre", "MediaType",
        "Playlist", "Customer", "Invoice", "InvoiceLine", "Employee",
    }


def test_all_three_link_kinds_present(chinook_ontology):
    onto = chinook_ontology
    kinds = {l.kind for l in onto.links.values()}
    assert kinds == {"direct", "through", "recursive"}


def test_two_distinct_routes_from_customer(chinook_ontology):
    onto = chinook_ontology
    names = {l.name for l in onto.links_from("Customer")}
    assert {"Customer_Invoices", "Customer_SupportRep"} <= names


def test_metrics_declare_three_distinct_grains(chinook_ontology):
    onto = chinook_ontology
    grains = {m.grain for m in onto.metrics.values()}
    assert {"invoice_line", "invoice", "track", "customer"} == grains


def test_track_object_spans_three_tables(chinook_ontology):
    onto = chinook_ontology
    assert set(onto.objects["Track"].tables) == {"track", "genre", "media_type"}


def test_playlist_track_join_table_is_hidden(chinook_ontology):
    onto = chinook_ontology
    """playlist_track must not appear as an object — it is a physical artifact."""
    assert all(o.primary != "playlist_track" for o in onto.objects.values())
    assert onto.links["Playlist_Tracks"].via == "playlist_track"
