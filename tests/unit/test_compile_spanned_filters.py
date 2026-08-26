"""A dotted filter on a property the target object reaches through one of its
OWN joins. Regression suite for C3, which turned four of the shipped chinook
specs into cartesian products, and for I1's ORDER BY.
"""
import pytest

from grain.engine.compile import compile_query, sql_text
from grain.engine.errors import UnknownName
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, OrderBy, QuerySpec


def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))


def _exists_body(sql: str) -> str:
    """Just the EXISTS subquery, so an assertion about it cannot be satisfied by
    something the OUTER query happens to contain."""
    assert "EXISTS" in sql, sql
    return sql.split("EXISTS", 1)[1]


# ------------------------------------------------------------------ C3


def test_a_filter_on_a_spanned_property_joins_that_table_inside_the_exists(
    chinook_lite, lite_metadata
):
    """C3. `target` used to be taken from the FILTERED PROPERTY's table — the
    spanned one — so the link's own join columns landed in a subquery whose FROM
    lacked the table they name, and SQLAlchemy added it implicitly and unjoined:

        WHERE EXISTS (SELECT 1 FROM genre, track
                      WHERE album.album_id = track.album_id
                        AND genre.name = 'Jazz')

    which means "this album has any track, and some genre called Jazz exists
    anywhere". It returned 347 of 347 albums for a filter 13 should match, and a
    Rock-only album passed a Jazz filter.
    """
    sql = build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        filters=[Filter(property="Customer_SupportRep.department", op="eq", value="Sales")],
    )
    body = _exists_body(sql)
    # The link's own table is the subquery's FROM, and the spanned table is
    # reached from it by the object join's real condition.
    assert "FROM employee" in body
    assert "employee.department_id = department" in body
    # A comma-separated FROM list is the shape of the cartesian this fixes.
    assert "FROM department, employee" not in body
    assert "FROM employee, department" not in body


def test_the_predicate_binds_to_the_spanned_column_not_the_primary(
    chinook_lite, lite_metadata
):
    """The filter must constrain the column it names. Binding the predicate to
    the primary table would compile a filter on `department.name` into one on
    some column of `employee`."""
    sql = build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        filters=[Filter(property="Customer_SupportRep.department", op="eq", value="Sales")],
    )
    body = _exists_body(sql)
    assert "Sales" in body
    predicate = body[body.index("Sales") - 40:body.index("Sales")]
    assert "department" in predicate, predicate


def test_a_spanned_filter_still_correlates_to_the_root(chinook_lite, lite_metadata):
    """The EXISTS has to stay correlated, or it degenerates to a global
    existence test that is true for every root row."""
    body = _exists_body(build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        filters=[Filter(property="Customer_SupportRep.department", op="eq", value="Sales")],
    ))
    assert "customer.support_rep_id" in body


def test_a_filter_on_the_targets_own_column_is_unchanged(chinook_lite, lite_metadata):
    """The non-spanned path must keep working exactly as before: no spanned join
    appears, because the property lives on the target's primary table."""
    body = _exists_body(build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        filters=[Filter(property="Customer_Invoices.total", op="gt", value=10)],
    ))
    assert "FROM invoice" in body
    assert "department" not in body


def test_a_self_referential_spanned_filter_keeps_its_alias(chinook_lite, lite_metadata):
    """The aliasing that stops `employees whose manager is named X` compiling to
    `employees who are their own manager` must survive the C3 rewrite."""
    body = _exists_body(build(
        chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
        filters=[Filter(property="Employee_Manager.last_name", op="eq", value="Adams")],
    ))
    assert "employee_1" in body


# ------------------------------------------------------------------ I1


def test_order_by_reaches_the_sql(chinook_lite, lite_metadata):
    """I1. `order_by` was accepted and never read. With `limit` defaulting to
    100, "top 10 countries by revenue" returned 10 arbitrary countries —
    correctly computed, and missing the actual top one."""
    sql = build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        metrics=["revenue"],
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        order_by=[OrderBy(key="revenue", desc=True)], limit=10,
    )
    assert "ORDER BY revenue DESC" in sql
    assert "LIMIT 10" in sql


def test_a_rewritten_metric_can_be_ordered_by(chinook_lite, lite_metadata):
    """A rewritten metric's column lives on the joined subquery, so ordering has
    to happen after the rewrite join is applied — the one ordering case that can
    silently produce invalid SQL if wired in the wrong place."""
    sql = build(
        chinook_lite, lite_metadata, object="Customer", group_by=["country"],
        metrics=["invoice_total"],
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        order_by=[OrderBy(key="invoice_total", desc=True)],
    )
    assert "ORDER BY invoice_total DESC" in sql


def test_ordering_by_a_group_key_ascends_by_default(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                order_by=[OrderBy(key="country")])
    assert "ORDER BY country ASC" in sql


def test_an_order_by_key_the_query_does_not_emit_is_refused(chinook_lite):
    """Silently ignoring an unhonourable key is what made I1 invisible. A key
    naming a metric the query never asked for cannot be honoured, so it is a
    typed error at the door with the emitted names as suggestions."""
    with pytest.raises(UnknownName, match="order_by key"):
        resolve(
            QuerySpec(object="Customer", group_by=["country"], metrics=["revenue"],
                      traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                      order_by=[OrderBy(key="units_sold")]),
            chinook_lite,
        )
