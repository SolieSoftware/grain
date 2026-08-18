from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec


def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))


def test_selects_root_properties(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"])
    assert "FROM customer" in sql
    assert "customer.country" in sql


def test_equality_filter_becomes_a_where_clause(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="eq", value="Brazil")])
    assert "WHERE" in sql and "Brazil" in sql


def test_in_filter_renders_an_in_clause(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="in", value=["Brazil", "France"])])
    assert " IN " in sql.upper()


def test_is_null_filter_renders_is_null(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="is_null")])
    assert "IS NULL" in sql.upper()


def test_many_to_one_hop_becomes_a_join(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                traverse=[Hop(link="Customer_SupportRep")])
    assert "JOIN employee" in sql
    on_clause = sql.split("JOIN employee")[1].split("WHERE")[0]
    assert "customer.support_rep_id" in on_clause
    assert "employee.employee_id" in on_clause


def test_multiple_filters_are_all_present_in_the_where_clause(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="eq", value="Brazil"),
                         Filter(property="country", op="ne", value="France")])
    where = sql.split("WHERE")[1]
    assert "Brazil" in where
    assert "France" in where


def test_limit_is_applied(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"], limit=25)
    assert "LIMIT 25" in sql
