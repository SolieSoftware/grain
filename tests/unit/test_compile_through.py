from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec


def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))


def test_through_link_joins_via_table_then_target(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Playlist", group_by=["name"],
                traverse=[Hop(link="Playlist_Tracks")])
    assert "JOIN playlist_track" in sql
    assert "JOIN track" in sql


def test_join_table_never_appears_as_a_selected_entity(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Playlist", group_by=["name"],
                traverse=[Hop(link="Playlist_Tracks")])
    assert "playlist_track." not in sql.split("FROM")[0]


def test_filter_across_a_fanning_edge_uses_exists_not_join(chinook_lite, lite_metadata):
    """Customers who bought something — each customer once, not once per line."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="Customer_Invoices.total", op="gt", value=5)])
    assert "EXISTS" in sql.upper()
    assert "JOIN invoice" not in sql


def test_filter_across_a_through_link_uses_exists_via_junction(chinook_lite, lite_metadata):
    """Playlists containing a track named 'X' — many_to_many via playlist_track,
    so the join table must appear inside the EXISTS, never as an outer JOIN."""
    sql = build(chinook_lite, lite_metadata, object="Playlist", group_by=["name"],
                filters=[Filter(property="Playlist_Tracks.name", op="eq", value="X")])
    assert "EXISTS" in sql.upper()
    outer = sql.split("WHERE")[0]
    assert "JOIN track" not in outer
    assert "JOIN playlist_track" not in outer

