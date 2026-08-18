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


def test_dotted_filter_on_an_untraversed_link_does_not_cartesian(chinook_lite, lite_metadata):
    """A non-fanning dotted filter used to take the plain-WHERE path, leaving its
    table unjoined in FROM. SQLAlchemy then inferred it, producing a silent
    cartesian product — 24 rows where 10 were correct. Every dotted filter now
    goes through EXISTS, which carries its own join condition.

    NOTE on the SAWarning: SQLAlchemy's cartesian-product linter only warns
    when a statement is compiled through an actual `Connection.execute()` —
    `Connection._execute_clauseelement` ORs in `WARN_LINTING` on top of the
    dialect's `compiler_linting`, which a bare `stmt.compile()` (what this
    database-free test does, via `sql_text`) never sets. Verified directly:
    compiling the *old*, buggy `FROM customer, employee` shape with
    `stmt.compile(dialect=create_engine("postgresql+psycopg://...").dialect,
    compile_kwargs={"literal_binds": True})` still raises no warning — only a
    live `Connection` does. So "no SAWarning" isn't assertable here without a
    database; the structural assertions below (no bare comma-join, EXISTS
    present) are the database-free proxy for it.
    """
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="Customer_SupportRep.last_name",
                                op="eq", value="Peacock")])
    assert "EXISTS" in sql.upper()
    assert "FROM customer, employee" not in " ".join(sql.split())


def test_filtering_on_a_link_that_is_also_traversed_still_compiles(chinook_lite,
                                                                   lite_metadata):
    """The EXISTS names `invoice`, and so does the traversal — SQLAlchemy's
    auto-correlation then stripped `invoice` out of the EXISTS, leaving it with
    no FROM at all and raising InvalidRequestError at compile time. The EXISTS
    now states its correlation explicitly, so only the root is correlatable."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                traverse=[Hop(link="Customer_Invoices")],
                filters=[Filter(property="Customer_Invoices.total", op="gt", value=5)])
    exists = sql.upper().split("EXISTS")[1]
    assert "FROM INVOICE" in exists  # the target stays inside the EXISTS
    assert "FROM CUSTOMER" not in exists  # the root does not: it is correlated



def test_filtering_across_a_self_referential_link_aliases_the_target(chinook_lite,
                                                                     lite_metadata):
    """`Employee_Manager` goes employee -> employee. Both sides of the link name
    the same table, so correlating the root stripped the EXISTS of its only FROM
    and every column in it bound to the OUTER row: `employees whose manager is
    named X` compiled to `employees who ARE their own manager`. Wrong rows, no
    error. The target is aliased, so the inner row has an identity of its own."""
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                filters=[Filter(property="Employee_Manager.last_name",
                                op="eq", value="Peacock")])
    exists = sql.upper().split("EXISTS")[1]
    assert "FROM EMPLOYEE AS EMPLOYEE_1" in exists  # the alias, and a real FROM
    # The correlated (outer) side keeps the bare table; only the inner side is
    # aliased. Comparing employee.reports_to to employee.employee_id would ask
    # for self-managing employees.
    assert "EMPLOYEE.REPORTS_TO = EMPLOYEE_1.EMPLOYEE_ID" in exists
    assert "EMPLOYEE_1.LAST_NAME = 'PEACOCK'" in exists


def test_a_non_self_referential_exists_is_still_unaliased(chinook_lite, lite_metadata):
    """The alias exists to separate two references to ONE table. Where there is
    only one, it would be noise."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="Customer_Invoices.total", op="gt", value=5)])
    assert "AS invoice_1" not in sql
