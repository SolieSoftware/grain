import pytest
from pydantic import ValidationError
from grain.engine.ontology import ColumnRef, LinkType, Metric, Ontology, ObjectType, Property

def test_column_ref_parses_dotted_name():
    ref = ColumnRef.parse("track.album_id")
    assert ref.table == "track" and ref.column == "album_id"
    assert ref.qualified == "track.album_id"

def test_column_ref_rejects_undotted_name():
    with pytest.raises(ValueError):
        ColumnRef.parse("album_id")

def test_column_ref_rejects_a_non_string():
    with pytest.raises(ValueError):
        ColumnRef.parse(123)

def test_fans_out_is_true_only_for_multiplying_cardinalities():
    def link(card):
        return LinkType(
            name="L", **{"from": "A"}, to="B", kind="direct", cardinality=card,
            on=[{"from": "a.b_id", "to": "b.id"}],
        )
    assert link("one_to_many").fans_out is True
    assert link("many_to_many").fans_out is True
    assert link("many_to_one").fans_out is False
    assert link("one_to_one").fans_out is False

def test_through_link_requires_via():
    with pytest.raises(ValidationError):
        LinkType(
            name="L", **{"from": "Playlist"}, to="Track", kind="through",
            cardinality="many_to_many",
            on_from=[{"from": "playlist.playlist_id", "to": "playlist_track.playlist_id"}],
            on_to=[{"from": "playlist_track.track_id", "to": "track.track_id"}],
        )

def test_direct_link_requires_on():
    with pytest.raises(ValidationError):
        LinkType(name="L", **{"from": "A"}, to="B", kind="direct", cardinality="many_to_one")

def test_recursive_link_requires_on():
    with pytest.raises(ValidationError):
        LinkType(name="L", **{"from": "Employee"}, to="Employee", kind="recursive",
                 cardinality="many_to_one")

def test_link_constructs_by_alias_and_by_field_name():
    """The YAML loader passes `from`; Python callers pass `from_`. Both must work."""
    common = dict(name="L", to="B", kind="direct", cardinality="many_to_one",
                  on=[{"from": "a.b_id", "to": "b.id"}])
    assert LinkType(**{"from": "A"}, **common).from_ == "A"
    assert LinkType(from_="A", **common).from_ == "A"

def test_links_from_returns_only_outbound_links():
    onto = Ontology(
        name="t",
        objects={"A": ObjectType(name="A", primary="a",
                                 properties={"id": Property(column="a.id", type="integer")})},
        links={
            "A_B": LinkType(name="A_B", **{"from": "A"}, to="B", kind="direct",
                            cardinality="many_to_one", on=[{"from": "a.b_id", "to": "b.id"}]),
            "B_A": LinkType(name="B_A", **{"from": "B"}, to="A", kind="direct",
                            cardinality="one_to_many", on=[{"from": "b.id", "to": "a.b_id"}]),
        },
        metrics={},
    )
    assert [l.name for l in onto.links_from("A")] == ["A_B"]
