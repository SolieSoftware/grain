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


def test_the_ancestor_gets_its_own_copy_of_a_spanned_table(chinook_lite, lite_metadata):
    """Employee declares a `department` join (see conftest's lite fixture), and
    the ancestor is a DIFFERENT ROW from the employee — so it needs its own copy
    of every table Employee spans, aliased apart.

    This inverts an earlier rule. `_apply_edge` used to skip object joins for a
    recursive target, on the reasoning that "that object's spanned tables are
    already joined". That was true only because the old CTE joined each row to
    its own hierarchy row, so the target WAS the source row (defect I3). Now that
    the target is the ancestor, reusing the employee's `department` would report
    the employee's department under the manager's name — the same wrong-entity
    bug one table further out.
    """
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert sql.count("JOIN department") == 2, sql
    # One un-aliased, for the employee; one aliased, for the ancestor.
    assert "JOIN department AS department_hop1" in sql


def test_a_spanned_property_of_the_ancestor_binds_to_the_ancestors_row(
    chinook_lite, lite_metadata
):
    """The point of the copy above: `Employee_Manager.department` must read the
    ancestor's department. It binds to the aliased table, which is joined to the
    CTE — not to the root's `employee.department_id`."""
    sql = build(chinook_lite, lite_metadata, object="Employee",
                group_by=["id", "Employee_Manager.department"],
                traverse=[Hop(link="Employee_Manager")])
    assert 'department_hop1.name AS "Employee_Manager.department"' in sql, sql
    assert (
        "employee_manager_cte_0.department_id = department_hop1.department_id" in sql
    ), sql
