"""The encoded aggregate, and the four conditions it depends on.

Verified against chinook before the design was written: through a join that
inflates the naive sum from 2328.60 to 5738.28, this form returns 2328.60
exactly, and 24 of 24 country groups match the per-country truth.
"""
import pytest

from grain.engine.errors import MetricNotSymmetric, NoIntegerKeyForGrain
from grain.engine.ontology import Metric
from grain.engine_symmetric.symmetric import (
    BOUND,
    grain_key,
    require_eligible,
    symmetric_expr,
)

REVENUE = Metric(name="revenue", grain="invoice_line", type="decimal", agg="sum",
                 value="invoice_line.unit_price * invoice_line.quantity")


def test_a_summing_metric_encodes_key_and_value(lite_metadata):
    sql = str(symmetric_expr(REVENUE, lite_metadata)).lower()
    assert "distinct" in sql
    assert "coalesce" in sql
    assert "1e30" in sql
    assert "invoice_line.invoice_line_id" in sql


def test_the_subtracted_term_carries_only_the_key(lite_metadata):
    """Two sums, and the second must not mention the value -- it exists purely
    to cancel the key offset out of the first."""
    sql = str(symmetric_expr(REVENUE, lite_metadata)).lower()
    assert sql.count("sum(distinct") == 2
    # The value appears once (in the added term), not twice.
    assert sql.count("unit_price") == 1


def test_coalesce_is_not_optional(lite_metadata):
    """Without it a NULL value drops out of the added sum while its key stays in
    the subtracted one. Measured: a true 10.00 became
    -1999999999999999999999999999990.00."""
    assert "coalesce" in str(symmetric_expr(REVENUE, lite_metadata)).lower()


def test_count_becomes_count_distinct_of_the_key(lite_metadata):
    """count(*) over a fanned join counts replicas; count(distinct key) counts
    rows."""
    m = Metric(name="lines", grain="invoice_line", type="integer", agg="count",
               value="invoice_line.quantity")
    sql = str(symmetric_expr(m, lite_metadata)).lower().replace(" ", "")
    assert "count(distinctinvoice_line.invoice_line_id)" in sql


def test_avg_divides_the_symmetric_sum_by_a_filtered_count(lite_metadata):
    """avg ignores NULL values, so the divisor must count only rows that
    contributed to the numerator -- a COALESCE-to-zero numerator over an
    unfiltered count would drag the mean towards zero."""
    m = Metric(name="avg_line", grain="invoice_line", type="decimal", agg="avg",
               value="invoice_line.unit_price")
    sql = str(symmetric_expr(m, lite_metadata)).lower()
    assert "filter" in sql
    assert "is not null" in sql
    assert "nullif" in sql


@pytest.mark.parametrize("agg", ["min", "max", "count_distinct"])
def test_an_immune_aggregate_is_not_encoded_at_all(agg, lite_metadata):
    """Encoding an immune aggregate would add cost for nothing."""
    m = Metric(name="m", grain="invoice_line", type="integer", agg=agg,
               value="invoice_line.quantity")
    sql = str(symmetric_expr(m, lite_metadata)).lower()
    assert "1e30" not in sql
    assert agg.replace("_distinct", "") in sql


def test_an_opaque_metric_is_refused_naming_the_other_engine(lite_metadata):
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               expr="sum(invoice_line.unit_price)")
    with pytest.raises(MetricNotSymmetric, match="subquery"):
        symmetric_expr(m, lite_metadata)


def test_a_non_exact_type_is_refused_rather_than_rounded(lite_metadata):
    """Only exactly-representable types may be encoded. Refusing beats silently
    losing digits -- the whole reason this design does not copy Looker's
    FLOOR-scaled variant."""
    m = Metric(name="d", grain="invoice", type="date", agg="sum",
               value="invoice.total")
    with pytest.raises(MetricNotSymmetric, match="exactly"):
        symmetric_expr(m, lite_metadata)


def test_a_grain_without_a_single_integer_key_is_refused(lite_metadata):
    """`playlist_track` is a pure join table with no single-column primary key,
    so condition (a) cannot be met."""
    m = Metric(name="pt", grain="playlist_track", type="integer", agg="sum",
               value="playlist_track.track_id")
    with pytest.raises(NoIntegerKeyForGrain, match="playlist_track"):
        symmetric_expr(m, lite_metadata)


def test_grain_key_returns_the_single_integer_primary_key(lite_metadata):
    assert grain_key(REVENUE, lite_metadata).name == "invoice_line_id"


def test_eligibility_is_checkable_without_building_the_expression(lite_metadata):
    """The planner calls this before a connection is acquired, per the rule that
    every failure except GuardTripped is raised first."""
    require_eligible(REVENUE, lite_metadata)
    with pytest.raises(MetricNotSymmetric):
        require_eligible(
            Metric(name="o", grain="invoice", type="decimal",
                   expr="sum(invoice.total)"),
            lite_metadata,
        )


def test_the_bound_is_half_the_offset():
    """Condition (b) is |v| < K/2. If these two ever disagree the proof in the
    module docstring stops applying.

    Compared as `Decimal`, read from K's own SQL text. The first draft of this
    test used `float("1e30")` and failed: that is 1000000000000000019884624838656,
    not 1e30, which is precisely the inexactness the module refuses to build on.
    """
    from decimal import Decimal

    from grain.engine_symmetric.symmetric import K

    assert BOUND * 2 == Decimal(str(K))
