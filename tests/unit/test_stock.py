"""A time dimension, and a quantity that does not sum across it.

grain has `type: date` and `type: datetime` but nothing marking a property as
THE time axis. `time_grain` does that, and a `stock` metric names it in
`over_time`.
"""
import pytest
from pydantic import ValidationError

from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import Metric, ObjectType, Ontology, Property


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
