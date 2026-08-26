"""Measured anchors for I3: traversing a recursive link. Every number here comes
from the loaded chinook, checked against hand-written recursive SQL rather than
against an expectation invented in the abstract.

The hierarchy is 8 employees in one connected tree — Adams(1) at the top, Edwards(2)
and Mitchell(6) under him, Peacock/Park/Johnson(3,4,5) under Edwards, King/Callahan
(7,8) under Mitchell. That shape is why the old construction's row-dropping was
invisible here: nothing in this data is an orphan except the single root.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.compile import compile_query
from grain.engine.errors import KeyBeyondGrain
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, OrderBy, QuerySpec

pytestmark = pytest.mark.integration

ANCESTORS_SQL = """
with recursive anc as (
  select e.employee_id as start_id, p.employee_id as anc_id, 1 as depth
  from employee e join employee p on e.reports_to = p.employee_id
  union all
  select a.start_id, p.employee_id, a.depth + 1
  from anc a
  join employee c on c.employee_id = a.anc_id
  join employee p on c.reports_to = p.employee_id
  where a.depth < 10)
select anc_id, count(distinct start_id) from anc group by anc_id
"""


@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)


def test_headcount_under_each_manager_matches_hand_written_recursive_sql(g, db_engine):
    """The question the old construction could not ask. It joined each employee to
    ITS OWN row in a top-down hierarchy CTE, so the group key resolved to the same
    employee and traversing returned the same 8 rows as not traversing."""
    result = g.query(QuerySpec(
        object="Employee",
        group_by=["Employee_Manager.id", "Employee_Manager.last_name"],
        metrics=["employee_count"],
        traverse=[Hop(link="Employee_Manager")],
        order_by=[OrderBy(key="employee_count", desc=True)],
        limit=None,
    ))
    with db_engine.connect() as conn:
        truth = {row[0]: row[1] for row in conn.execute(text(ANCESTORS_SQL)).all()}
    assert {row[0]: row[2] for row in result.rows} == truth
    # Adams has all 7 others below him; Edwards 3; Mitchell 2.
    assert [(row[0], row[2]) for row in result.rows] == [(1, 7), (2, 3), (6, 2)]


def test_the_ancestor_walk_is_inline_and_non_additive_not_rewritten(g):
    """`employee_count` is at the ROOT's grain and the recursive hop is downstream
    of it, so the naive rule forces a rewrite — and a rewrite cannot carry a key
    that lies past a fanning edge. What makes it answerable is that the group key
    pins one Employee at that hop, so each copy of an employee row lands in a
    different ancestor's group and appears exactly once inside it.

    The mirror image of `revenue` by `Playlist`, which the engine already answered
    this way: correct per group, meaningless as a total."""
    result = g.query(QuerySpec(
        object="Employee", group_by=["Employee_Manager.id"],
        metrics=["employee_count"], traverse=[Hop(link="Employee_Manager")],
        limit=None,
    ))
    assert result.rewrites == []
    assert result.additive is False
    assert "belongs to several groups" in result.non_additive_reason
    # 7 + 3 + 2 = 12 against a true headcount of 8: the total is exactly what the
    # flag says it is, and the per-group numbers are exactly right.
    assert sum(row[1] for row in result.rows) == 12


def test_the_same_query_without_a_pinning_key_is_refused(g):
    """Grouping by the ancestor's NAME pins nothing — two managers may share a
    surname — so the copies are not separated and there is no correct rendering."""
    with pytest.raises(KeyBeyondGrain, match="Employee_Manager"):
        g.query(QuerySpec(
            object="Employee", group_by=["Employee_Manager.last_name"],
            metrics=["employee_count"], traverse=[Hop(link="Employee_Manager")],
            limit=None,
        ))


def test_max_depth_one_gives_direct_reports_and_is_additive_again(g, db_engine):
    """`max_depth=1` is one hop, which IS the declared many_to_one — so it neither
    fans out nor overlaps, and the column sums to something meaningful: the 7
    employees who have a manager."""
    result = g.query(QuerySpec(
        object="Employee", group_by=["Employee_Manager.id"],
        metrics=["employee_count"],
        traverse=[Hop(link="Employee_Manager", max_depth=1)], limit=None,
    ))
    assert result.additive is True
    assert result.non_additive_reason is None
    with db_engine.connect() as conn:
        truth = {
            row[0]: row[1] for row in conn.execute(text("""
                select reports_to, count(*) from employee
                where reports_to is not null group by reports_to
            """)).all()
        }
    assert {row[0]: row[1] for row in result.rows} == truth
    assert sum(row[1] for row in result.rows) == 7


def test_a_qualified_key_reads_the_ancestor_not_the_row_itself(g):
    """The wrong-entity half of I3, measured. Peacock(3) reports to Edwards(2),
    who reports to Adams(1) — so Peacock's ancestors are Edwards and Adams, and
    never Peacock."""
    result = g.query(QuerySpec(
        object="Employee", group_by=["Employee_Manager.last_name"],
        filters=[Filter(property="last_name", op="eq", value="Peacock")],
        traverse=[Hop(link="Employee_Manager")], limit=None,
    ))
    assert sorted(row[0] for row in result.rows) == ["Adams", "Edwards"]


def test_a_row_with_no_ancestor_is_dropped_like_every_other_many_to_one_hop(g):
    """Adams is the top of the tree, so he has no ancestor and no row here. That
    is what `Customer_SupportRep` does to a customer with no rep, and the rule is
    published in `describe()` rather than left to be discovered."""
    result = g.query(QuerySpec(
        object="Employee", group_by=["id", "Employee_Manager.id"],
        traverse=[Hop(link="Employee_Manager")], limit=None,
    ))
    starts = {row[0] for row in result.rows}
    assert 1 not in starts
    assert len(starts) == 7


def test_a_cycle_in_the_data_terminates_and_never_repeats_an_ancestor(
    db_engine, chinook_metadata, chinook_ontology
):
    """The data test the cycle guard never had. `max_depth` alone would terminate
    but would walk a 3-cycle over and over, yielding the same ancestor at several
    depths; the path array is what makes each (row, ancestor) pair appear once.

    The cycle is created inside a transaction and rolled back, so the fixture
    database is left exactly as it was — chinook has no cycle of its own, which
    is why this guard was only ever proven by reading the SQL.
    """
    spec = QuerySpec(
        object="Employee", group_by=["id", "Employee_Manager.id"],
        traverse=[Hop(link="Employee_Manager", max_depth=10)], limit=None,
    )
    rq = resolve(spec, chinook_ontology)
    stmt = compile_query(rq, analyse(rq), chinook_metadata)

    with db_engine.connect() as conn:
        transaction = conn.begin()
        try:
            # 1 -> 3 -> 2 -> 1. Adams now reports to Peacock.
            conn.execute(text("update employee set reports_to = 3 where employee_id = 1"))
            rows = conn.execute(stmt).all()
        finally:
            transaction.rollback()

    pairs = [(row[0], row[1]) for row in rows]
    assert pairs
    assert len(pairs) == len(set(pairs)), "an ancestor was visited twice for one row"
    # The cycle really was in place: Adams has ancestors only because of it.
    assert 1 in {start for start, _ in pairs}

    with db_engine.connect() as conn:
        restored = conn.execute(
            text("select reports_to from employee where employee_id = 1")
        ).scalar_one()
    assert restored is None, "the cycle must not survive the test"
