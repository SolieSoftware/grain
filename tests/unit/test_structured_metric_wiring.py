"""The two declaration forms are two spellings of one aggregate.

If they ever compile differently, migrating the chinook pack would silently
change every number it reports. And the loader's guarantee -- every
`table.column` token in a metric belongs to that metric's grain table, which is
what lets the compiler render the aggregate verbatim -- has to cover both forms
or the structured form is an unchecked hole.
"""
import pytest

from grain.engine.compile import _metric_column
from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import Metric, Ontology

OPAQUE = Metric(name="revenue", grain="invoice_line", type="decimal",
                expr="sum(invoice_line.unit_price * invoice_line.quantity)")
STRUCTURED = Metric(name="revenue", grain="invoice_line", type="decimal",
                    agg="sum",
                    value="invoice_line.unit_price * invoice_line.quantity")


def test_the_two_forms_compile_byte_identically():
    assert str(_metric_column(OPAQUE)) == str(_metric_column(STRUCTURED))


def test_a_structured_count_distinct_compiles_to_the_same_sql():
    opaque = Metric(name="track_count", grain="track", type="integer",
                    expr="count(distinct track.track_id)")
    structured = Metric(name="track_count", grain="track", type="integer",
                        agg="count_distinct", value="track.track_id")
    assert str(_metric_column(opaque)) == str(_metric_column(structured))


def test_a_structured_value_may_not_reference_another_table(lite_metadata):
    """`_metric_expr` binds a metric to the first un-aliased occurrence of its
    grain table. A value naming a different table would bind somewhere the grain
    analysis never reasoned about -- the hole `expr` was closed against."""
    onto = Ontology(name="t", metrics={
        "bad": Metric(name="bad", grain="invoice_line", type="decimal",
                      agg="sum", value="invoice.total"),
    })
    with pytest.raises(OntologyError, match="may only reference"):
        validate(onto, lite_metadata)


def test_a_structured_value_may_not_name_a_missing_column(lite_metadata):
    onto = Ontology(name="t", metrics={
        "bad": Metric(name="bad", grain="invoice_line", type="decimal",
                      agg="sum", value="invoice_line.nope"),
    })
    with pytest.raises(OntologyError, match="does not exist"):
        validate(onto, lite_metadata)


def test_a_structured_value_may_not_be_unqualified(lite_metadata):
    onto = Ontology(name="t", metrics={
        "bad": Metric(name="bad", grain="invoice_line", type="decimal",
                      agg="sum", value="quantity"),
    })
    with pytest.raises(OntologyError, match="unqualified"):
        validate(onto, lite_metadata)


def test_a_valid_structured_metric_passes(lite_metadata):
    onto = Ontology(name="t", metrics={
        "ok": Metric(name="ok", grain="invoice_line", type="decimal", agg="sum",
                     value="invoice_line.unit_price * invoice_line.quantity"),
        "counted": Metric(name="counted", grain="track", type="integer",
                          agg="count_distinct", value="track.track_id"),
    })
    validate(onto, lite_metadata)
