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
