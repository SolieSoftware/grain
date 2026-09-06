"""A time dimension, and a quantity that does not sum across it.

grain has `type: date` and `type: datetime` but nothing marking a property as
THE time axis. `time_grain` does that, and a `stock` metric names it in
`over_time`.
"""
import typing

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, DateTime, Integer, MetaData, Numeric, Table

from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import AggFunc, LinkType, Metric, ObjectType, Ontology, Property


def _time_onto(metric: Metric | None = None, grain: str | None = "day",
               column: str = "invoice.invoice_date") -> Ontology:
    props = {
        "when": Property(column=column, type="datetime", time_grain=grain),
        "total": Property(column="invoice.total", type="decimal",
                          quantity="flow"),
    }
    return Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice",
                                       properties=props)},
        metrics={metric.name: metric} if metric else {},
    )


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_the_five_declarable_grains(grain):
    assert Property(column="invoice.invoice_date", type="datetime",
                    time_grain=grain).time_grain == grain


def test_an_unknown_grain_is_refused():
    with pytest.raises(ValidationError):
        Property(column="invoice.invoice_date", type="datetime",
                 time_grain="fortnight")


def test_time_grain_is_optional():
    """Most properties are not a time axis and should need nothing said."""
    assert Property(column="invoice.total", type="decimal").time_grain is None


def test_a_time_grain_on_a_temporal_column_is_accepted(lite_metadata):
    validate(_time_onto(), lite_metadata)


def test_a_time_grain_on_a_non_temporal_column_is_refused(lite_metadata):
    """The whole point is that ordering and comparison are meaningful, which a
    text column cannot promise. Checked against the REFLECTED type, not the
    declared one, for the same reason order statistics are."""
    with pytest.raises(OntologyError, match="not a date or timestamp"):
        validate(_time_onto(column="invoice.customer_id"), lite_metadata)


def _nullable_time_metadata() -> MetaData:
    """A temporal column the database allows to be NULL. `lite_metadata` has
    none — chinook's `invoice_date` is NOT NULL — which is exactly why no test
    could reach the boundary filter's NULL behaviour before."""
    md = MetaData()
    Table(
        "snapshot", md,
        Column("snapshot_id", Integer, primary_key=True, nullable=False),
        Column("taken_at", DateTime),  # nullable
        Column("balance", Numeric, nullable=False),
    )
    return md


def _nullable_time_onto(nullable: bool = True) -> Ontology:
    props = {
        "when": Property(column="snapshot.taken_at", type="datetime",
                         time_grain="day", nullable=nullable),
        "balance": Property(column="snapshot.balance", type="decimal", quantity="flow"),
    }
    return Ontology(
        name="t",
        objects={"Snapshot": ObjectType(name="Snapshot", primary="snapshot",
                                        properties=props)},
    )


def test_a_time_grain_on_a_nullable_column_is_refused():
    """`_window_to_boundary` filters `t = max(t) over (...)`, and `max` ignores
    NULLs, so a group whose instants are ALL NULL picks nothing and DISAPPEARS
    from the result — a missing row, harder to notice than a wrong number.

    `IS NOT DISTINCT FROM` is the right answer for a nullable group KEY, where
    NULL is a real group and the rejoin only restores an identity `=` broke.
    It is the wrong answer here: it would decide that undated rows form an
    instant of their own, which nothing declares."""
    with pytest.raises(OntologyError, match="nullable"):
        validate(_nullable_time_onto(), _nullable_time_metadata())


def test_the_refusal_names_the_column_to_make_not_null():
    with pytest.raises(OntologyError) as exc:
        validate(_nullable_time_onto(), _nullable_time_metadata())
    assert "snapshot.taken_at" in str(exc.value)
    assert "NOT NULL" in str(exc.value)


def test_declaring_nullable_false_over_a_nullable_column_is_still_refused():
    """The alternative the refusal names must be the real one. Declaring the
    property NOT NULL when the database says otherwise does not resolve it —
    `_check_nullability` refuses that first, which is what lets this check read
    `prop.nullable` and trust it."""
    with pytest.raises(OntologyError, match="may add nullability, never remove"):
        validate(_nullable_time_onto(nullable=False), _nullable_time_metadata())


def test_a_gratuitous_nullable_true_is_refused_and_says_so(lite_metadata):
    """`nullable: true` over a column the database says is NOT NULL is legal
    everywhere else — a declaration may always ADD nullability. On a time axis
    it is not, and the repair is the declaration rather than the schema, so the
    refusal has to name that one and not send the author to the DDL."""
    onto = _time_onto()
    onto.objects["Invoice"].properties["when"] = Property(
        column="invoice.invoice_date", type="datetime", time_grain="day", nullable=True)
    with pytest.raises(OntologyError) as exc:
        validate(onto, lite_metadata)
    assert "remove 'nullable: true'" in str(exc.value)


def test_a_time_axis_behind_a_left_join_names_both_repairs():
    """A left join manufactures NULLs from a NOT NULL column all on its own, so
    it is a second, independent reason. Both are named at once: fixing only the
    one the error mentioned would send the author straight back round."""
    from grain.engine.ontology import TableJoin

    md = MetaData()
    Table("snapshot", md, Column("snapshot_id", Integer, primary_key=True, nullable=False))
    Table("detail", md,
          Column("snapshot_id", Integer, primary_key=True, nullable=False),
          Column("taken_at", DateTime))  # nullable AND behind a left join
    onto = Ontology(
        name="t",
        objects={"Snapshot": ObjectType(
            name="Snapshot", primary="snapshot",
            joins={"d": TableJoin(to="detail", kind="left", cardinality="many_to_one",
                                  on=[{"from": "snapshot.snapshot_id",
                                       "to": "detail.snapshot_id"}])},
            properties={"when": Property(column="detail.taken_at", type="datetime",
                                         time_grain="day", nullable=True, via="d")},
        )},
    )
    with pytest.raises(OntologyError) as exc:
        validate(onto, md)
    assert "detail.taken_at" in str(exc.value)
    assert "left join 'd'" in str(exc.value)


def test_a_time_grain_on_a_not_null_column_is_accepted(lite_metadata):
    """The named alternative, actually used: chinook's own `invoice_date` is
    NOT NULL, so it loads."""
    validate(_time_onto(), lite_metadata)


# -- over_time ---------------------------------------------------------------

def _stock(choice="last", dimension="when") -> Metric:
    return Metric(name="level", grain="invoice", type="decimal", agg="sum",
                  value="invoice.total", quantity="stock",
                  over_time={"dimension": dimension, "choice": choice})


@pytest.mark.parametrize("choice", ["first", "last"])
def test_both_choices_are_legal(choice):
    assert _stock(choice=choice).over_time.choice == choice


def test_min_and_max_are_not_the_vocabulary():
    """MetricFlow spells these min|max. `window_choice: max` reads as the
    largest VALUE when it means the value at the latest DATE — two readings, one
    wrong, in a field whose whole job is to disambiguate."""
    with pytest.raises(ValidationError):
        _stock(choice="max")


def test_a_stock_requires_over_time():
    with pytest.raises(ValidationError, match="over_time"):
        Metric(name="level", grain="invoice", type="decimal", agg="sum",
               value="invoice.total", quantity="stock")


def test_over_time_is_refused_on_a_flow():
    """A field meaningful for one quantity kind must be rejected elsewhere, or
    it reads as configuration that silently does nothing."""
    with pytest.raises(ValidationError, match="over_time"):
        Metric(name="rev", grain="invoice", type="decimal", agg="sum",
               value="invoice.total", quantity="flow",
               over_time={"dimension": "when", "choice": "last"})


def test_the_named_dimension_must_exist(lite_metadata):
    with pytest.raises(OntologyError, match="no property"):
        validate(_time_onto(_stock(dimension="nope")), lite_metadata)


def test_the_named_dimension_must_declare_a_time_grain(lite_metadata):
    """Naming a non-temporal property would window over something with no
    meaningful order."""
    with pytest.raises(OntologyError, match="time_grain"):
        validate(_time_onto(_stock(dimension="total")), lite_metadata)


def test_a_well_formed_stock_loads(lite_metadata):
    validate(_time_onto(_stock()), lite_metadata)


def test_a_stock_is_no_longer_refused_for_not_accumulating(lite_metadata):
    """Task 2 left `stock` outside ACCUMULATES, so summing one was refused.
    `over_time` is what makes it summable — the window means the sum never
    crosses time."""
    validate(_time_onto(_stock()), lite_metadata)


# -- a stock INFERRED from the property, not declared on the metric ----------

def _inferred_stock_onto(metric_quantity=None, over_time=None) -> Ontology:
    """A stock declared on the PROPERTY, with a metric that says nothing.

    This is the path `_effective_quantity` exists to serve, and the one
    `Metric._check_over_time` structurally cannot see: it keys on
    `metric.quantity`, which is None here.
    """
    props = {
        "when": Property(column="invoice.invoice_date", type="datetime", time_grain="day"),
        "balance": Property(column="invoice.total", type="decimal", quantity="stock"),
    }
    kw = {"quantity": metric_quantity} if metric_quantity else {}
    if over_time is not None:
        kw["over_time"] = over_time
    return Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice", properties=props)},
        metrics={"level": Metric(name="level", grain="invoice", type="decimal",
                                 agg="sum", value="invoice.total", **kw)},
    )


def test_a_property_declared_stock_summed_by_a_silent_metric_is_refused(lite_metadata):
    """The gap: `Metric._check_over_time` keys on `metric.quantity`, so a stock
    inferred from the property never reached it and the ontology loaded clean
    with no `over_time`. Both engines then summed it across time — and AGREED,
    because each triggers on the same absent `metric.over_time`. Two engines
    agreeing on a wrong answer is the one failure the differential harness
    cannot see, so it has to be refused at load."""
    with pytest.raises(OntologyError, match="sets no 'over_time'"):
        validate(_inferred_stock_onto(), lite_metadata)


def test_the_refusal_names_a_declared_time_axis(lite_metadata):
    with pytest.raises(OntologyError) as exc:
        validate(_inferred_stock_onto(), lite_metadata)
    assert "when" in str(exc.value)


def test_the_named_over_time_alternative_actually_loads(lite_metadata):
    """The alternative the refusal names must itself resolve. Naively it did
    not: adding `over_time` to a metric whose own `quantity` stays silent was
    refused in turn by `Metric._check_over_time`, looping the author between two
    errors. Checked by USING the advice, not by matching its text."""
    validate(_inferred_stock_onto(over_time={"dimension": "when", "choice": "last"}),
             lite_metadata)


def test_the_named_flow_alternative_also_loads(lite_metadata):
    """The second escape the refusal offers: the metric declaring itself a flow.
    Precedence is one-directional, so the metric's word beats the property's."""
    validate(_inferred_stock_onto(metric_quantity="flow"), lite_metadata)


def test_over_time_on_a_silent_metric_over_a_flow_property_is_refused(lite_metadata):
    """The other direction of the same rule, equally invisible to the model: the
    effective kind is a flow, so collapsing across time means nothing."""
    props = {
        "when": Property(column="invoice.invoice_date", type="datetime", time_grain="day"),
        "total": Property(column="invoice.total", type="decimal", quantity="flow"),
    }
    onto = Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice", properties=props)},
        metrics={"level": Metric(name="level", grain="invoice", type="decimal", agg="sum",
                                 value="invoice.total",
                                 over_time={"dimension": "when", "choice": "last"})},
    )
    with pytest.raises(OntologyError, match="quantity: stock"):
        validate(onto, lite_metadata)


def test_a_non_sum_stock_still_needs_over_time():
    """`count_distinct(employee_id)` is the textbook stock and reads no quantity
    column at all, so it can only ever declare its kind on the metric. The check
    therefore cannot live behind the `agg == 'sum'` guard the accumulation rule
    uses."""
    with pytest.raises(ValidationError, match="over_time"):
        Metric(name="headcount", grain="employee", type="integer",
               agg="count_distinct", value="employee.employee_id", quantity="stock")


# -- a typo must be a typo, not a semantic change ----------------------------

def test_a_misspelled_over_time_is_refused():
    """`OverTime` forbids extras; `Metric` did not. So `overtime: {...}` parsed
    cleanly, left `over_time` None, and produced a metric that reported
    `additive: true` and summed a stock across time. The misspelling was not a
    misspelling — it was a different declaration."""
    with pytest.raises(ValidationError, match="overtime"):
        Metric(name="level", grain="invoice", type="decimal", agg="sum",
               value="invoice.total", quantity="flow",
               overtime={"dimension": "when", "choice": "last"})


def test_a_misspelled_time_grain_is_refused():
    """Same failure on the property side: `time_grian` left the property not a
    time axis at all, so any stock naming it was refused for the wrong
    reason."""
    with pytest.raises(ValidationError, match="time_grian"):
        Property(column="invoice.invoice_date", type="datetime", time_grian="day")


# -- the planning verdict ----------------------------------------------------

def test_the_plan_carries_the_window(lite_metadata, chinook_lite):
    """Decided with the other verdicts rather than recomputed in compile, for
    the same reason subquery_edges is: a window applied over a different column
    from the one the analysis reasoned about would silently invalidate it."""
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock())
    plan = analyse(resolve(QuerySpec(object="Invoice", metrics=["level"]), onto))
    (mp,) = plan.metric_plans
    assert mp.window is not None
    assert mp.window.choice == "last"
    assert mp.window.column.qualified == "invoice.invoice_date"


def test_a_flow_carries_no_window(chinook_lite):
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    plan = analyse(resolve(
        QuerySpec(object="Invoice", metrics=["invoice_total"]), chinook_lite))
    (mp,) = plan.metric_plans
    assert mp.window is None


def test_the_symmetric_engine_refuses_a_stock(lite_metadata):
    """Stated in the design rather than discovered. The window needs a window
    function inside a subquery, and that engine is one pass over the join.

    The cost is real: the differential harness cannot cross-check stock, so the
    oracle is the only independent judge for it. That judgement is made in
    `tests/integration/test_stock_anchors.py`, by
    `test_the_oracle_agrees_with_the_engine`, against a `tools/oracle.py` that
    collapses to the boundary instant in pure Python and shares no SQL with
    either engine. This sentence stood here while the oracle had no stock
    support at all and the claim was simply false; if that check is ever
    removed, this claim goes with it."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import require_eligible

    with pytest.raises(MetricNotSymmetric, match="subquery"):
        require_eligible(_stock(), lite_metadata)


def test_the_refusal_explains_why(lite_metadata):
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import require_eligible

    with pytest.raises(MetricNotSymmetric) as exc:
        require_eligible(_stock(), lite_metadata)
    assert "window" in str(exc.value).lower()


# -- compiling the window ----------------------------------------------------
#
# Two tests lived here that only grepped the emitted SQL for "over (", "max("
# and "min(". They never executed it, so a wrongly-partitioned or
# wrongly-filtered window passed all three — the third appearance in this file
# of a pattern that had already shipped broken twice (see
# `test_the_named_alternative_actually_resolves_the_recursive_case`). Deleted
# rather than rewritten into slightly better string checks; the behavioural
# coverage for the same ground is in
# `tests/integration/test_stock_window_compile.py`, where the SQL runs and the
# numbers are compared against an independent oracle.


# -- guarding against a windowed stock alongside anything else ---------------

def _multi_metric_onto(*metrics: Metric) -> Ontology:
    """Like `_time_onto`, but for tests that need more than one metric in
    scope at once."""
    props = {
        "when": Property(column="invoice.invoice_date", type="datetime", time_grain="day"),
        "total": Property(column="invoice.total", type="decimal", quantity="flow"),
    }
    return Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice", properties=props)},
        metrics={m.name: m for m in metrics},
    )


def test_a_windowed_stock_with_another_structured_metric_is_refused(lite_metadata):
    """`_window_to_boundary` re-exposes only the windowed metric's own value
    through the wrap. A second, structured metric would still render as
    `_metric_column`'s raw text naming its own physical table -- a table the
    wrap has removed from the outer FROM -- so it would raise `UndefinedTable`
    at execution rather than compile time. Refused up front instead."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    invoice_total = Metric(name="invoice_total", grain="invoice", type="decimal",
                           agg="sum", value="invoice.total")
    onto = _multi_metric_onto(_stock(), invoice_total)
    rq = resolve(QuerySpec(object="Invoice", metrics=["level", "invoice_total"]), onto)
    with pytest.raises(GrainError, match="also asks for"):
        compile_query(rq, analyse(rq), lite_metadata)


def test_a_windowed_stock_with_an_opaque_metric_is_refused(lite_metadata):
    """The dangerous case: an opaque `count(*)` names no table at all, so
    unlike a structured metric it would not fail loudly -- it would compile,
    run, and silently count only the handful of rows the window's boundary
    filter left behind, instead of the real population. Refused for the same
    reason as the structured case, before that number is ever produced."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    count_all = Metric(name="count_all", grain="invoice", type="integer", expr="count(*)")
    onto = _multi_metric_onto(_stock(), count_all)
    rq = resolve(QuerySpec(object="Invoice", metrics=["level", "count_all"]), onto)
    with pytest.raises(GrainError, match="also asks for"):
        compile_query(rq, analyse(rq), lite_metadata)


def test_two_windowed_stocks_are_refused(lite_metadata):
    """The second window's filter would apply to rows the first had already
    dropped -- a behavioural check, not just a string check on the SQL."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    opening = _stock(choice="first").model_copy(update={"name": "opening"})
    onto = _multi_metric_onto(_stock(), opening)
    rq = resolve(QuerySpec(object="Invoice", metrics=["level", "opening"]), onto)
    with pytest.raises(GrainError, match="at most one stock"):
        compile_query(rq, analyse(rq), lite_metadata)


def _recursive_stock_onto() -> Ontology:
    """A recursive self-link, mirroring the shipped `Employee_Manager` link,
    plus a time dimension and a root-grain stock -- built fresh here because
    no existing fixture combines recursion with a time axis."""
    props = {
        "id": Property(column="employee.employee_id", type="integer", unique=True),
        "when": Property(column="employee.hired_at", type="datetime", time_grain="day"),
    }
    metric = Metric(name="level", grain="employee", type="decimal", agg="sum",
                    value="employee.level_amt", quantity="stock",
                    over_time={"dimension": "when", "choice": "last"})
    return Ontology(
        name="t",
        objects={"Employee": ObjectType(name="Employee", primary="employee",
                                        properties=props)},
        links={"Employee_Manager": LinkType(
            name="Employee_Manager", **{"from": "Employee"}, to="Employee",
            kind="recursive",
            on=[{"from": "employee.reports_to", "to": "employee.employee_id"}],
            cardinality="many_to_one", max_depth=10,
        )},
        metrics={"level": metric},
    )


def _recursive_stock_metadata() -> MetaData:
    md = MetaData()
    Table(
        "employee", md,
        Column("employee_id", Integer, primary_key=True, nullable=False),
        Column("reports_to", Integer),
        Column("hired_at", DateTime, nullable=False),
        Column("level_amt", Numeric, nullable=False),
    )
    return md


def test_a_windowed_stock_forced_into_aggregate_then_join_is_refused():
    """A recursive traversal fans out beyond the metric's own (root) grain,
    forcing aggregate_then_join -- which the window cannot reach, since that
    strategy pre-aggregates in its own subquery built fresh from the root."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import Hop, QuerySpec

    onto = _recursive_stock_onto()
    md = _recursive_stock_metadata()
    rq = resolve(QuerySpec(object="Employee", metrics=["level"],
                           traverse=[Hop(link="Employee_Manager")]), onto)
    with pytest.raises(GrainError, match="aggregate-then-join"):
        compile_query(rq, analyse(rq), md)


def test_the_named_alternative_actually_resolves_the_recursive_case():
    """The bug this guards against: the root's own unique key, unqualified --
    the natural first reading of "pin the fanning edge" -- looks like it
    should work and does not, looping the reader back to the same refusal.
    Checked by actually USING the alternative the error names, not by matching
    a substring of the error text -- that distinction is the whole reason the
    original bug slipped through."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import Hop, QuerySpec

    onto = _recursive_stock_onto()
    md = _recursive_stock_metadata()

    wrong = resolve(QuerySpec(object="Employee", metrics=["level"],
                              traverse=[Hop(link="Employee_Manager")],
                              group_by=["id"]), onto)
    with pytest.raises(GrainError) as exc:
        compile_query(wrong, analyse(wrong), md)
    keyed = [a for a in exc.value.alternatives if a.startswith("group_by ")]
    assert keyed, f"no group_by alternative offered: {exc.value.alternatives}"
    key = keyed[0].split("'")[1]
    assert key != "id", "must be qualified through the fanning edge, not the root's own key"

    fixed = resolve(QuerySpec(object="Employee", metrics=["level"],
                              traverse=[Hop(link="Employee_Manager")],
                              group_by=[key]), onto)
    compile_query(fixed, analyse(fixed), md)  # must not raise


def test_an_opaque_stock_is_refused_when_windowed(lite_metadata):
    """An opaque `expr` has no separable per-row value to isolate and
    re-aggregate once the window has picked one instant."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.ontology import OverTime
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    metric = Metric(name="level", grain="invoice", type="decimal",
                    expr="sum(invoice.total)", quantity="stock",
                    over_time=OverTime(dimension="when", choice="last"))
    onto = _time_onto(metric)
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    with pytest.raises(GrainError, match="opaque"):
        compile_query(rq, analyse(rq), lite_metadata)


def test_a_group_key_named_like_the_reserved_bookkeeping_columns_is_refused(lite_metadata):
    """`__grain_value`/`__grain_t`/`__grain_pick` are reserved for the wrap's
    own bookkeeping columns. Nothing stops an ontology from naming a real
    PROPERTY the same way -- that reservation only ever guarded a physical
    column -- so the collision is checked explicitly and raises `GrainError`
    rather than falling through to SQLAlchemy's own ambiguous-label
    `InvalidRequestError`."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    props = {
        "when": Property(column="invoice.invoice_date", type="datetime", time_grain="day"),
        "total": Property(column="invoice.total", type="decimal", quantity="flow"),
        "__grain_value": Property(column="invoice.customer_id", type="integer"),
    }
    onto = Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice", properties=props)},
        metrics={"level": _stock()},
    )
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"],
                           group_by=["__grain_value"]), onto)
    with pytest.raises(GrainError, match="reserves for its own bookkeeping"):
        compile_query(rq, analyse(rq), lite_metadata)


# -- _reaggregate stays in sync with Metric.sql_expr --------------------------

@pytest.mark.parametrize("agg", typing.get_args(AggFunc))
def test_reaggregate_matches_sql_expr_for_every_agg(agg):
    """`_reaggregate` is a deliberate copy of `Metric.sql_expr`'s dispatch --
    built from a `ColumnElement` instead of rendered as text, needed once a
    window has moved the value out of its physical table and into a wrapping
    subquery. A copy this repo tolerates only where a parity test makes drift
    visible (`tests/unit/test_resolver_parity.py` is the other one); this is
    that test for the aggregate dispatch."""
    from sqlalchemy import literal_column

    from grain.engine.compile import _reaggregate

    kwargs = {"percentile": 0.9} if agg == "percentile" else {}
    metric = Metric(name="m", grain="t", type="decimal", agg=agg, value="t.v", **kwargs)
    built = str(
        _reaggregate(metric, literal_column("t.v"))
        .compile(compile_kwargs={"literal_binds": True})
    )
    # Normalised: SQLAlchemy renders `DISTINCT`/`WITHIN GROUP` upper-case where
    # `Metric.sql_expr` renders the same text lower-case. Whitespace differs the
    # same way. Neither difference is a difference in what SQL runs.
    assert metric.sql_expr.lower().replace(" ", "") == built.lower().replace(" ", "")
