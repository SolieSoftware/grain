from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))


def test_recursive_link_emits_a_recursive_cte(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert "RECURSIVE" in sql.upper()


def test_depth_bound_is_present(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager", max_depth=3)])
    assert "3" in sql


def test_recursive_target_does_not_duplicate_object_joins(chinook_lite, lite_metadata):
    """Employee declares a `department` join (see conftest's lite fixture). The
    recursive link's target is Employee itself, already in scope as the root —
    so `_apply_edge` must not call `_apply_object_joins` a second time for it.
    If it did, `JOIN department` would appear twice in the compiled SQL; the
    root's own object-joins pass already accounts for the single legitimate
    occurrence.
    """
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert sql.count("JOIN department") == 1
