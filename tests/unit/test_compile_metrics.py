import re

import pytest

from grain.engine.compile import compile_query, sql_text
from grain.engine.errors import FanOutRefused
from grain.engine.grain import analyse
from grain.engine.ontology import LinkType
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec

BOTH_HOPS = [Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")]


def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))


def subquery_of(sql: str) -> str:
    """The body of the first metric subquery, without the query around it."""
    return sql.split("LEFT OUTER JOIN (", 1)[1].rsplit(") AS ", 1)[0]


@pytest.fixture(scope="session")
def lite_with_employee_customers(chinook_lite):
    """chinook_lite plus a fanning link out of Employee.

    The fixture ontology can only leave Employee by the recursive
    `Employee_Manager` link, so without this there is no way to build a metric
    whose *prefix* crosses a recursive edge — the exact shape that makes two
    recursive CTEs land in one statement. Added here rather than in the shared
    fixture so no existing test's scope changes.
    """
    link = LinkType.model_validate(
        {
            "name": "Employee_Customers",
            "from": "Employee",
            "to": "Customer",
            "kind": "direct",
            "cardinality": "one_to_many",
            "on": [{"from": "employee.employee_id", "to": "customer.support_rep_id"}],
        }
    )
    return chinook_lite.model_copy(
        update={"links": {**chinook_lite.links, "Employee_Customers": link}}
    )


def test_inline_metric_aggregates_without_a_subquery(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["customer_count"])
    assert "count(distinct customer.customer_id)" in sql.lower()
    assert sql.lower().count("select") == 1


def test_metric_at_the_finest_grain_needs_no_subquery(chinook_lite, lite_metadata):
    """revenue's grain is the LAST thing the path reaches, so nothing traversed
    after it replicates its rows and inline is correct (see grain.py's
    `test_metric_at_the_finest_grain_aggregates_inline`). A rewrite here would
    be needless work, not a wrong number — but the emitter must not invent one."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["revenue"], traverse=BOTH_HOPS)
    assert sql.lower().count("select") == 1
    assert "left outer join" not in sql.lower()


def test_fanned_metric_is_computed_in_its_own_subquery(chinook_lite, lite_metadata):
    """invoice_total sits at invoice grain with `Invoice_Lines` traversed after
    it — the shape that returns 20848.62 when compiled inline.

    (The brief reached for `revenue` here; since Task 8 corrected the rule to
    look only DOWNSTREAM of the grain, revenue on this same path is inline and
    correct, so it can no longer stand in for a fanned metric. See the test
    above, which pins that.)
    """
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS)
    assert sql.lower().count("select") >= 2
    assert "left outer join" in sql.lower() or "left join" in sql.lower()
    assert "sum(invoice.total)" in subquery_of(sql)


def test_two_grains_produce_two_subqueries(chinook_lite, lite_metadata):
    """invoice_total (invoice grain) and customer_count (root grain) both have a
    fanning hop downstream of them, so each gets its own pre-aggregate: two
    subquery GROUP BYs plus the outer one."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total", "customer_count"], traverse=BOTH_HOPS)
    assert sql.lower().count("group by") >= 3


def test_subquery_applies_only_the_prefix_reaching_the_grain(chinook_lite, lite_metadata):
    """The whole point of the rewrite: `Invoice_Lines` is DOWNSTREAM of invoice
    grain, so it must not appear inside the subquery. Applying it there would
    replicate invoice rows inside the very subquery built to stop that — the
    same 8.95x overstatement, one level down."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS)
    inner = subquery_of(sql)
    assert "invoice_line" not in inner
    assert "invoice_line" in sql  # the outer query still walks the declared path


def test_root_grain_metric_pre_aggregates_over_the_root_table(chinook_lite, lite_metadata):
    """Task 8 made aggregate_then_join reachable for a metric AT the root: the
    prefix is empty and every fanning hop is downstream. The subquery must hang
    off the root table itself, not off a joined-in child."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["customer_count"], traverse=BOTH_HOPS)
    inner = subquery_of(sql)
    assert "FROM customer" in inner
    assert "invoice" not in inner
    assert "count(distinct customer.customer_id)" in inner


def test_a_filter_reaches_the_metric_subquery(chinook_lite, lite_metadata):
    """The pre-aggregate must run over the SAME population as the outer query.
    Left unfiltered it would compute each group's total over every row in the
    table and then join that untouched number back onto a filtered key set."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS,
                filters=[Filter(property="country", op="eq", value="Brazil")])
    assert "Brazil" in subquery_of(sql)


def test_a_dotted_filter_survives_inside_the_metric_subquery(chinook_lite, lite_metadata):
    """The EXISTS names `invoice` and so does the subquery's own prefix. Left to
    auto-correlate, SQLAlchemy strips `invoice` out of the EXISTS and the whole
    statement fails to compile — so this asserts the EXISTS keeps its FROM."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS,
                filters=[Filter(property="Customer_Invoices.total", op="gt", value=5)])
    inner = subquery_of(sql).upper()
    assert "EXISTS" in inner
    assert "FROM INVOICE" in inner.split("EXISTS")[1]


def test_a_rewritten_metric_with_no_keys_to_join_on_is_refused(chinook_lite, lite_metadata):
    """No group_by and no inline metric leaves nothing to join the pre-aggregate
    back on to, and no correct SQL to emit. Refuse, naming the repair."""
    with pytest.raises(FanOutRefused) as exc:
        build(chinook_lite, lite_metadata, object="Customer", metrics=["customer_count"],
              traverse=BOTH_HOPS)
    assert exc.value.metric == "customer_count"
    assert exc.value.alternatives


def test_an_inline_sibling_gives_a_keyless_rewrite_something_to_hang_on(
    chinook_lite, lite_metadata
):
    """With an inline metric present there IS a column list, so the keyless
    pre-aggregate is joined on `true` — one row against one row."""
    sql = build(chinook_lite, lite_metadata, object="Customer",
                metrics=["revenue", "customer_count"], traverse=BOTH_HOPS)
    assert "ON true" in sql
    assert "customer_count_at_customer.customer_count AS customer_count" in sql


def test_a_rejoined_metric_is_never_wrapped_in_coalesce(chinook_lite, lite_metadata):
    """The outer key set is a subset of the subquery's, so a miss can only be a
    key that failed to match one it should have. `coalesce(..., 0)` would repaint
    that as a plausible number; NULL leaves the fault visible."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS)
    assert "coalesce" not in sql.lower()


def test_recursive_edge_in_a_metric_prefix_gets_its_own_cte_name(
    lite_with_employee_customers, lite_metadata
):
    """Both the outer path and the metric's prefix cross `Employee_Manager`, so
    two recursive CTEs are built for one link. Every CTE in a statement is
    hoisted into the same top-level WITH, so identical names would make Postgres
    reject the whole statement as a duplicate CTE name."""
    # `id` is in the group_by because traversing a recursive link walks the
    # depth-bounded CLOSURE, which is many_to_many (see LinkType.
    # effective_cardinality) -- so this query is non-additive and must group by a
    # key that identifies one row, exactly as the playlist case must.
    sql = build(lite_with_employee_customers, lite_metadata, object="Employee",
                group_by=["id", "last_name"], metrics=["customer_count"],
                traverse=[Hop(link="Employee_Manager"), Hop(link="Employee_Customers"),
                          Hop(link="Customer_Invoices")])
    defined = re.findall(r"(employee_manager_cte\w*)\([a-z_, ]+\) AS ", sql)
    assert len(defined) == 2, sql
    assert len(set(defined)) == 2, defined


def test_repeating_one_recursive_link_still_names_each_cte_once(
    chinook_lite, lite_metadata
):
    """`Employee_Manager` goes Employee -> Employee, so it may legally be walked
    twice in one path. Two CTEs, one link, still one statement."""
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager"), Hop(link="Employee_Manager")])
    defined = re.findall(r"(employee_manager_cte\w*)\([a-z_, ]+\) AS ", sql)
    assert len(defined) == 2, sql
    assert len(set(defined)) == 2, defined


def test_a_nullable_group_key_rejoins_null_safely(chinook_lite, lite_metadata):
    """`customer.country` is nullable, so `=` would leave every NULL-country row
    unmatched against its own group — the metric would read NULL for a group
    that has a perfectly good number."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS)
    assert "IS NOT DISTINCT FROM" in sql.upper()


def test_a_not_null_group_key_rejoins_with_plain_equality(
    lite_with_employee_customers, lite_metadata
):
    """`employee.last_name` is NOT NULL in the schema, so the null-safe operator
    buys nothing and costs the optimiser a hash join."""
    # `id` is in the group_by because traversing a recursive link walks the
    # depth-bounded CLOSURE, which is many_to_many (see LinkType.
    # effective_cardinality) -- so this query is non-additive and must group by a
    # key that identifies one row, exactly as the playlist case must.
    sql = build(lite_with_employee_customers, lite_metadata, object="Employee",
                group_by=["id", "last_name"], metrics=["customer_count"],
                traverse=[Hop(link="Employee_Manager"), Hop(link="Employee_Customers"),
                          Hop(link="Customer_Invoices")])
    assert "IS NOT DISTINCT FROM" not in sql.upper()
    assert "employee.last_name = customer_count_at_customer.last_name" in sql


def test_reflected_nullability_overrides_a_declaration_that_understates_it(
    chinook_lite, lite_metadata
):
    """Defence in depth for CRITICAL-1. The loader refuses a `nullable: false`
    over a nullable column, but nothing downstream should have to trust that it
    did — an ontology built in memory bypasses the loader entirely. The
    reflected column is authoritative; a declaration may only ADD nullability.
    """
    from grain.engine.ontology import Property

    obj = chinook_lite.objects["Customer"]
    lying = obj.model_copy(
        update={
            "properties": {
                **obj.properties,
                "country": Property(column="customer.country", type="string",
                                    nullable=False),
            }
        }
    )
    onto = chinook_lite.model_copy(
        update={"objects": {**chinook_lite.objects, "Customer": lying}}
    )
    sql = build(onto, lite_metadata, object="Customer", group_by=["country"],
                metrics=["invoice_total"], traverse=BOTH_HOPS)
    assert "IS NOT DISTINCT FROM" in sql.upper()


def test_duplicate_metrics_and_keys_are_answered_not_crashed(chinook_lite, lite_metadata):
    """Two identically-aliased subqueries, or two identically-labelled columns,
    make SQLAlchemy raise mid-compile on a spec whose meaning was never unclear."""
    sql = build(chinook_lite, lite_metadata, object="Customer",
                group_by=["country", "country"],
                metrics=["invoice_total", "invoice_total"], traverse=BOTH_HOPS)
    assert sql.count(") AS invoice_total_at_invoice") == 1  # one subquery, not two
    assert sql.count("AS country") == 2  # outer column and the subquery's copy
    assert sql.count("AS invoice_total ") == 2  # ditto, one metric not two
