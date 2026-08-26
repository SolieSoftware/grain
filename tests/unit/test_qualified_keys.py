"""Qualified group keys (`Employee_Manager.last_name`) and the ancestor walk they
made necessary. This is defect I3's resolution: a traversed object's properties
were unnameable by construction, so a recursive hop could express nothing and its
only observable effect was to silently drop rows.
"""
import pytest

from grain.engine.compile import compile_query, sql_text
from grain.engine.errors import (
    AmbiguousGroupKey,
    GroupKeyNotOnPath,
    KeyBeyondGrain,
    UnknownName,
)
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

def plan(onto, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return rq, analyse(rq)


def build(onto, metadata, **kw):
    rq, p = plan(onto, **kw)
    return sql_text(compile_query(rq, p, metadata))


# ------------------------------------------------------------------ resolution


def test_a_qualified_key_resolves_to_the_traversed_object(chinook_lite):
    rq, _ = plan(chinook_lite, object="Employee",
                 group_by=["Employee_Manager.last_name"],
                 traverse=[Hop(link="Employee_Manager")])
    key = rq.group_by[0]
    assert key.object.name == "Employee"
    assert key.edge_index == 0
    assert key.qualified is True
    # The label is the full key: a qualified key labelled `last_name` would be
    # indistinguishable from the root's own property of that name.
    assert key.name == "Employee_Manager.last_name"


def test_a_bare_key_still_resolves_to_the_root(chinook_lite):
    rq, _ = plan(chinook_lite, object="Employee", group_by=["last_name"],
                 traverse=[Hop(link="Employee_Manager")])
    assert rq.group_by[0].edge_index is None
    assert rq.group_by[0].qualified is False


def test_a_key_naming_an_untraversed_link_is_refused_with_the_repair(chinook_lite):
    """The column has to be in FROM to be grouped by, and only a hop puts it
    there — so the repair is the hop, and the error says so."""
    with pytest.raises(GroupKeyNotOnPath) as excinfo:
        plan(chinook_lite, object="Employee",
             group_by=["Employee_Manager.last_name"])
    assert "add the Employee_Manager hop to traverse" in excinfo.value.alternatives


def test_a_key_naming_a_link_traversed_twice_is_ambiguous(chinook_lite):
    """A qualified key names a LINK, not a hop. Silently picking the first or the
    last would be choosing between two different numbers."""
    with pytest.raises(AmbiguousGroupKey, match="hops 1, 2"):
        plan(chinook_lite, object="Employee",
             group_by=["Employee_Manager.last_name"],
             traverse=[Hop(link="Employee_Manager"), Hop(link="Employee_Manager")])


def test_an_unknown_property_of_a_traversed_object_names_that_object(chinook_lite):
    with pytest.raises(UnknownName, match="property of Employee"):
        plan(chinook_lite, object="Employee", group_by=["Employee_Manager.nope"],
             traverse=[Hop(link="Employee_Manager")])


# ------------------------------------------------------------------ the ancestor walk


def test_the_cte_walks_upward_from_each_row(chinook_lite, lite_metadata):
    """The old CTE enumerated the hierarchy from its roots and joined each row to
    ITS OWN row in it, which is why traversing expressed nothing. This one pairs
    each starting row with each of its ancestors."""
    sql = build(chinook_lite, lite_metadata, object="Employee",
                group_by=["id", "Employee_Manager.last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert "FROM employee AS start_0 JOIN employee AS parent_0 " \
           "ON start_0.reports_to = parent_0.employee_id" in sql
    # And the outer join pairs each employee with its own ancestors, NOT with
    # itself: the giveaway the old construction left was `employee.employee_id =
    # cte.employee_id`.
    assert "employee_manager_cte_0.__grain_start = employee.employee_id" in sql
    assert "employee_manager_cte_0.employee_id = employee.employee_id" not in sql


def test_the_ancestors_own_column_is_what_a_qualified_key_reads(
    chinook_lite, lite_metadata
):
    sql = build(chinook_lite, lite_metadata, object="Employee",
                group_by=["Employee_Manager.last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert 'employee_manager_cte_0.last_name AS "Employee_Manager.last_name"' in sql
    # The employee's own last_name is NOT what got selected.
    assert "employee.last_name AS" not in sql


def test_the_cycle_guard_and_depth_bound_are_both_present(chinook_lite, lite_metadata):
    """They guard different failure modes: max_depth alone still lets a two-node
    cycle spin until the bound is hit; the path array alone still needs a floor
    for data that is simply deep."""
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["id"],
                traverse=[Hop(link="Employee_Manager", max_depth=4)])
    assert "__grain_depth < 4" in sql
    assert "ANY (employee_manager_cte_0.__grain_path)" in sql


def test_bookkeeping_columns_cannot_shadow_a_real_column(chinook_lite, lite_metadata):
    """The CTE re-exposes every column of the traversed table under its own name,
    so its own bookkeeping columns are namespaced out of the way."""
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["id"],
                traverse=[Hop(link="Employee_Manager")])
    for name in ("__grain_start", "__grain_depth", "__grain_path"):
        assert name in sql


# ------------------------------------------------------------------ grain verdicts


def test_a_recursive_traversal_fans_out_however_the_link_is_declared(chinook_lite):
    """`Employee_Manager` declares many_to_one, which is the truth about ONE hop.
    The traversal walks the closure: many ancestors per row, many rows per
    ancestor. Reading the declared cardinality here would treat a replicating
    join as safe."""
    link = chinook_lite.links["Employee_Manager"]
    assert link.cardinality == "many_to_one"
    assert link.effective_cardinality == "many_to_many"
    assert link.fans_out is True


def test_one_hop_is_exactly_the_declared_cardinality(chinook_lite):
    """`max_depth=1` is the escape hatch for "the manager" rather than "the
    management chain", and it must not drag non-additivity along with it."""
    rq, p = plan(chinook_lite, object="Employee",
                 group_by=["Employee_Manager.id"], metrics=["employee_count"],
                 traverse=[Hop(link="Employee_Manager", max_depth=1)])
    assert p.metric_plans[0].additive is True
    assert rq.path[0].link.effective_cardinality == "many_to_one"
    assert rq.path[0].link.fans_out is False


def test_a_unique_key_at_the_fanning_hop_earns_an_inline_verdict(chinook_lite):
    """The heart of it. A fanning edge downstream of the grain replicates the
    metric's rows, and the rewrite exists to stop those copies being summed
    together — but a unique key at that edge's own target scatters them into
    distinct groups, so within any group the row appears exactly once.

    This is the same shape the engine already answered in the other direction:
    `revenue` (grain at the far end) by `Playlist` is inline and non-additive.
    """
    _, p = plan(chinook_lite, object="Employee",
                group_by=["Employee_Manager.id"], metrics=["employee_count"],
                traverse=[Hop(link="Employee_Manager")])
    mp = p.metric_plans[0]
    assert mp.strategy == "inline"
    assert mp.additive is False
    assert "belongs to several groups" in mp.non_additive_reason


def test_without_the_unique_key_the_same_query_is_rewritten_then_refused(chinook_lite):
    """Grouping by the ancestor's NAME pins nothing — two managers can share a
    surname — so the copies are not separated, the rewrite is forced, and the
    rewrite cannot carry a key that lies past the fanning edge."""
    with pytest.raises(KeyBeyondGrain, match="Employee_Manager"):
        plan(chinook_lite, object="Employee",
             group_by=["Employee_Manager.last_name"], metrics=["employee_count"],
             traverse=[Hop(link="Employee_Manager")])


def test_a_key_beyond_the_grain_across_a_fanning_edge_is_refused(chinook_lite):
    """The general form: a pre-aggregate has to CARRY every group key, and
    walking far enough to reach this one would replicate the metric's rows inside
    the very subquery built to stop that."""
    with pytest.raises(KeyBeyondGrain) as excinfo:
        plan(chinook_lite, object="Customer",
             group_by=["Customer_Invoices.total"], metrics=["customer_count"],
             traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert excinfo.value.alternatives
    assert any("drop" in alt for alt in excinfo.value.alternatives)


def test_a_qualified_key_across_a_NON_fanning_hop_needs_no_rewrite(
    chinook_lite, lite_metadata
):
    """`Customer_SupportRep` is many_to_one, so nothing is replicated and the key
    is simply available. This is the ordinary case and it must stay ordinary."""
    rq, p = plan(chinook_lite, object="Customer",
                 group_by=["Customer_SupportRep.last_name"],
                 metrics=["customer_count"],
                 traverse=[Hop(link="Customer_SupportRep")])
    assert p.metric_plans[0].strategy == "inline"
    assert p.additive is True
    sql = sql_text(compile_query(rq, p, lite_metadata))
    assert 'employee.last_name AS "Customer_SupportRep.last_name"' in sql


def test_a_bare_key_query_is_planned_exactly_as_before(chinook_lite):
    """The regression guard for the whole change: a spec with no qualified key
    must reach the same verdict it always did, because `edge_index is None`
    matches no fanning position."""
    _, p = plan(chinook_lite, object="Customer", group_by=["country"],
                metrics=["customer_count"],
                traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    mp = p.metric_plans[0]
    assert mp.strategy == "aggregate_then_join"
    assert mp.forced_by == "Customer_Invoices"
    assert mp.additive is True


# ------------------------------------------------------------------ aliasing


def test_a_table_in_scope_twice_is_aliased_apart(chinook_lite, lite_metadata):
    """`employee` is in scope twice here — once as the customer's support rep,
    once as that rep's ancestor. Without separate FROM elements the same table
    lands in FROM twice and the database decides which row a column meant."""
    sql = build(chinook_lite, lite_metadata, object="Customer",
                group_by=["Customer_SupportRep.last_name",
                          "Employee_Manager.last_name"],
                traverse=[Hop(link="Customer_SupportRep"), Hop(link="Employee_Manager")])
    assert 'employee.last_name AS "Customer_SupportRep.last_name"' in sql
    assert (
        'employee_manager_cte_1.last_name AS "Employee_Manager.last_name"' in sql
    ), sql


def test_alias_names_are_deterministic_for_a_spec(chinook_lite, lite_metadata):
    """`explain()` claims the SQL you inspected is the SQL that runs, which needs
    the text — not just the verdicts — to be a pure function of the spec."""
    kw = dict(object="Employee", group_by=["id", "Employee_Manager.department"],
              traverse=[Hop(link="Employee_Manager")])
    assert build(chinook_lite, lite_metadata, **kw) == build(
        chinook_lite, lite_metadata, **kw
    )


def test_order_by_accepts_a_qualified_key(chinook_lite, lite_metadata):
    from grain.engine.spec import OrderBy

    sql = build(chinook_lite, lite_metadata, object="Employee",
                group_by=["Employee_Manager.last_name"],
                traverse=[Hop(link="Employee_Manager")],
                order_by=[OrderBy(key="Employee_Manager.last_name", desc=True)])
    assert 'ORDER BY "Employee_Manager.last_name" DESC' in sql
