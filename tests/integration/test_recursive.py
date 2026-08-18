import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def test_employee_hierarchy_is_three_levels(db_engine):
    """Anchor from the spec's §3: 1 -> 2 -> 5."""
    sql = text("""
        with recursive h as (
          select employee_id, reports_to, 1 as lvl from employee where reports_to is null
          union all
          select e.employee_id, e.reports_to, h.lvl + 1
          from employee e join h on e.reports_to = h.employee_id
        )
        select lvl, count(*) from h group by lvl order by lvl
    """)
    with db_engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(sql)] == [(1, 1), (2, 2), (3, 5)]
