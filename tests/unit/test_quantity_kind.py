"""A quantity that does not accumulate may not be summed.

grain validates a metric's GRAIN — that its rows are not replicated — and until
now had no concept of whether the QUANTITY was additive by nature.
`sum(track.unit_price)` was arithmetically perfect, reported `additive: true`,
and answered no question. This closes that, at load, for both engines.

The rule is narrow on purpose: it inspects a summed value only when that value
is a BARE column reference. `sum(a * b)` is left alone, because a rate times a
count genuinely is extensive — `revenue` is exactly that shape, and a cruder
rule would refuse grain's flagship metric.
"""
import pytest
from pydantic import ValidationError

from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import Metric, ObjectType, Ontology, Property


def _onto(metric: Metric, quantity: str | None = None, **prop_kw) -> Ontology:
    """One object over `track`, one metric, with unit_price declared however the
    test needs it."""
    props = {
        "unit_price": Property(column="track.unit_price", type="decimal",
                               quantity=quantity, **prop_kw),
        "duration_ms": Property(column="track.milliseconds", type="integer",
                                quantity="flow"),
    }
    return Ontology(
        name="t",
        objects={"Track": ObjectType(name="Track", primary="track", properties=props)},
        metrics={metric.name: metric},
    )


def _sum(value: str, name: str = "m") -> Metric:
    return Metric(name=name, grain="track", type="decimal", agg="sum", value=value)


# -- the vocabulary itself ---------------------------------------------------

@pytest.mark.parametrize("kind", ["flow", "stock", "value_per_unit"])
def test_the_three_declarable_kinds(kind):
    assert Property(column="track.unit_price", type="decimal",
                    quantity=kind).quantity == kind


@pytest.mark.parametrize("old", ["extensive", "rate", "ratio"])
def test_the_old_vocabulary_is_refused(old):
    """No alias mapping. A value that silently mapped from an old name to a new
    one is the quiet accommodation this codebase avoids — the ontology author
    should see the error and choose."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Property(column="track.unit_price", type="decimal", quantity=old)


def test_an_unknown_kind_is_refused_at_declaration():
    with pytest.raises(ValidationError):
        Property(column="track.unit_price", type="decimal", quantity="money")


def test_quantity_is_optional_so_most_properties_need_no_annotation():
    """Only columns a sum actually touches have to declare. A name or a country
    is never summed and should not need a word said about it."""
    assert Property(column="track.name", type="string").quantity is None


# -- the rule ----------------------------------------------------------------

def test_summing_a_flow_column_is_allowed(lite_metadata):
    validate(_onto(_sum("track.milliseconds"), quantity="flow"), lite_metadata)


def test_summing_a_value_per_unit_is_refused(lite_metadata):
    """The case this whole feature exists for."""
    with pytest.raises(OntologyError, match="does not accumulate"):
        validate(_onto(_sum("track.unit_price"), quantity="value_per_unit"),
                 lite_metadata)


def test_the_refusal_names_what_to_do_instead(lite_metadata):
    """Every error in this codebase names a legal alternative."""
    with pytest.raises(OntologyError) as exc:
        validate(_onto(_sum("track.unit_price"), quantity="value_per_unit"), lite_metadata)
    message = str(exc.value)
    assert "value_per_unit" in message
    assert "avg" in message and "min" in message and "max" in message


def test_summing_an_undeclared_column_is_refused(lite_metadata):
    """Defaulting to summable would be the silent assumption this codebase
    exists to remove. The author has to say."""
    with pytest.raises(OntologyError, match="does not declare"):
        validate(_onto(_sum("track.unit_price"), quantity=None), lite_metadata)


def test_summing_a_column_with_no_property_at_all_is_refused(lite_metadata):
    """There is nowhere to put the declaration, so the model is incomplete."""
    onto = _onto(_sum("track.album_id"), quantity="flow")  # album_id has no property
    with pytest.raises(OntologyError, match="no declared property"):
        validate(onto, lite_metadata)


# -- the narrowness that keeps `revenue` legal -------------------------------

def test_summing_a_product_of_a_rate_and_a_count_is_allowed(lite_metadata):
    """`revenue` is `sum(unit_price * quantity)`. A rate times a count IS
    extensive, so a rule that refused every sum touching a rate would refuse
    grain's flagship metric. Only BARE column references are inspected."""
    onto = _onto(_sum("track.unit_price * track.milliseconds"), quantity="value_per_unit")
    validate(onto, lite_metadata)


def test_a_non_sum_aggregate_over_a_rate_is_allowed(lite_metadata):
    """An average price is a perfectly good statistic; only summing is wrong."""
    for agg in ("avg", "min", "max"):
        m = Metric(name="m", grain="track", type="decimal", agg=agg,
                   value="track.unit_price")
        validate(_onto(m, quantity="value_per_unit"), lite_metadata)


def test_an_opaque_expr_metric_is_skipped(lite_metadata):
    """Known limit, documented rather than hidden: an `expr` is not decomposed,
    so nothing here can tell whether it sums. Sniffing for 'sum(' with a regex
    is exactly the fragility this codebase avoids."""
    m = Metric(name="m", grain="track", type="decimal",
               expr="sum(track.unit_price)")
    validate(_onto(m, quantity="value_per_unit"), lite_metadata)


def test_the_real_chinook_pack_still_loads(chinook_lite):
    """The fixture pack declares real metrics including the product form. If
    this breaks, the rule is too broad."""
    assert "revenue" in chinook_lite.metrics


# -- the declaration site ----------------------------------------------------

def test_a_metric_may_declare_its_own_quantity(lite_metadata):
    """Headcount is the textbook stock and a column-level field cannot express
    it: count_distinct(employee_id) has no quantity column, because employee_id
    is an identifier.

    Asserted through the LOADER, not by reading `m.quantity` back. That earlier
    form merely echoed a Pydantic field: it passed whether or not anything in
    the loader consulted a metric-level quantity, which is the only thing the
    declaration is for. `_effective_quantity` is what the rules actually read.

    `over_time` is required alongside `quantity: stock` (see test_stock.py) --
    named here with a dimension that need not resolve to anything, since this
    test never calls `validate()` and so never reaches the ontology check."""
    from grain.engine.loader import _effective_quantity

    m = Metric(name="headcount", grain="employee", type="integer",
               agg="count_distinct", value="employee.employee_id",
               quantity="stock",
               over_time={"dimension": "hire_date", "choice": "last"})
    onto = _onto(m)
    kind, source = _effective_quantity(onto, onto.metrics["headcount"])
    assert kind == "stock"
    # No property reads `employee_id`, and the value is not even on the object
    # this ontology declares -- so nothing but the metric itself could have
    # supplied this.
    assert source == "metric 'headcount'"


def test_a_metric_quantity_beats_the_property(lite_metadata):
    """Precedence is one-directional and stated: the metric is closer to the
    meaning of the number, so it wins. No error is raised on disagreement."""
    from grain.engine.loader import _effective_quantity

    onto = _onto(Metric(name="m", grain="track", type="decimal", agg="sum",
                        value="track.unit_price", quantity="flow"),
                 quantity="value_per_unit")
    kind, source = _effective_quantity(onto, onto.metrics["m"])
    assert kind == "flow"
    assert "metric" in source


def test_the_property_is_used_when_the_metric_is_silent(lite_metadata):
    """What keeps chinook's four existing annotations working."""
    from grain.engine.loader import _effective_quantity

    onto = _onto(_sum("track.unit_price"), quantity="value_per_unit")
    kind, source = _effective_quantity(onto, onto.metrics["m"])
    assert kind == "value_per_unit"
    assert "property" in source


def test_inference_still_refuses_the_original_case(lite_metadata):
    """The migration must not silently drop the guarantee Task 1 preserved: a
    sum over a bare column with no metric-level quantity is still refused when
    the property says value_per_unit."""
    with pytest.raises(OntologyError, match="does not accumulate"):
        validate(_onto(_sum("track.unit_price"), quantity="value_per_unit"),
                 lite_metadata)


def test_a_metric_level_declaration_works_without_any_property(lite_metadata):
    """`sum(track.album_id)` has no declared property, which used to be refused
    outright. A metric-level quantity now supplies what the column cannot."""
    m = Metric(name="m", grain="track", type="integer", agg="sum",
               value="track.album_id", quantity="flow")
    onto = _onto(m, quantity="flow")
    validate(onto, lite_metadata)
