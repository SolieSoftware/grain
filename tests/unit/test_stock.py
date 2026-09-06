"""A time dimension, and a quantity that does not sum across it.

grain has `type: date` and `type: datetime` but nothing marking a property as
THE time axis. `time_grain` does that, and a `stock` metric names it in
`over_time`.
"""
import typing

import pytest
from pydantic import ValidationError

from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import AggFunc, Metric, ObjectType, Ontology, Property


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
    oracle is the only independent judge for it."""
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

def test_the_emitted_sql_windows_before_aggregating(lite_metadata):
    """The shape: a window function inside a subquery, then a filter to the
    picked instant, then the aggregate. Summing across time is impossible by
    construction rather than by a check."""
    from grain.engine.compile import compile_query, sql_text
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock())
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    sql = sql_text(compile_query(rq, analyse(rq), lite_metadata)).lower()
    assert "over (" in sql, "needs a window function"
    assert "max(" in sql, "last means the maximum instant"
    assert "sum(" in sql


def test_choice_first_uses_min(lite_metadata):
    from grain.engine.compile import compile_query, sql_text
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock(choice="first"))
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    sql = sql_text(compile_query(rq, analyse(rq), lite_metadata)).lower()
    assert "min(" in sql


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


def test_a_windowed_stock_forced_into_aggregate_then_join_is_refused(lite_metadata):
    """The window wraps `stmt`; `aggregate_then_join` pre-aggregates this
    metric in an entirely separate subquery built fresh from the root, which
    the wrap never touches. Combining the two would silently sum across time
    in that untouched subquery. Built by hand, since no ontology in this file
    forces a root-grain stock through that strategy -- this checks the guard
    directly rather than waiting for a query shape that may never arise."""
    from grain.engine.compile import compile_query
    from grain.engine.errors import GrainError
    from grain.engine.grain import GrainPlan, MetricPlan, WindowSpec
    from grain.engine.ontology import ColumnRef
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    metric = _stock()
    onto = _time_onto(metric)
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    forced_plan = GrainPlan(metric_plans=[MetricPlan(
        metric=metric, strategy="aggregate_then_join",
        window=WindowSpec(column=ColumnRef(table="invoice", column="invoice_date"),
                          choice="last"),
    )])
    with pytest.raises(GrainError, match="aggregate-then-join"):
        compile_query(rq, forced_plan, lite_metadata)


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
