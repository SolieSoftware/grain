"""A metric declares its aggregate either as one opaque string (`expr`) or
structurally (`agg` + `value`). Only the structured form can be reasoned about:
the fan-out taxonomy needs the FUNCTION, and the symmetric engine needs the
VALUE. Neither is recoverable from `expr` without parsing SQL, which would make
a wrong answer depend on the quality of a regex."""
import pytest
from pydantic import ValidationError

from grain.engine.ontology import Metric


def test_opaque_expr_still_loads_unchanged():
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               expr="sum(invoice_line.unit_price * invoice_line.quantity)")
    assert m.sql_expr == "sum(invoice_line.unit_price * invoice_line.quantity)"
    assert m.is_structured is False
    assert m.fanout_immune is False


def test_structured_form_renders_the_same_sql():
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               agg="sum", value="invoice_line.unit_price * invoice_line.quantity")
    assert m.sql_expr == "sum(invoice_line.unit_price * invoice_line.quantity)"
    assert m.is_structured is True


def test_count_distinct_renders_the_distinct_keyword():
    m = Metric(name="track_count", grain="track", type="integer",
               agg="count_distinct", value="track.track_id")
    assert m.sql_expr == "count(distinct track.track_id)"


@pytest.mark.parametrize("agg", ["min", "max", "count_distinct"])
def test_these_aggregates_are_immune_to_fan_out(agg):
    """min/max of a multiset are unaffected by duplicates, and count(distinct x)
    dedupes by construction. Row replication cannot change any of them."""
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds")
    assert m.fanout_immune is True


@pytest.mark.parametrize("agg", ["sum", "count", "avg"])
def test_these_aggregates_are_not_immune(agg):
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds")
    assert m.fanout_immune is False


def test_an_opaque_count_distinct_is_not_claimed_immune():
    """It may well BE immune, but nothing here can prove it, and guessing would
    trade a needless subquery for a possible wrong number."""
    m = Metric(name="track_count", grain="track", type="integer",
               expr="count(distinct track.track_id)")
    assert m.fanout_immune is False


def test_declaring_both_forms_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer",
               expr="sum(track.milliseconds)", agg="sum",
               value="track.milliseconds")


def test_declaring_neither_form_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer")


def test_agg_without_value_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer", agg="sum")


def test_value_without_agg_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer", value="track.milliseconds")
