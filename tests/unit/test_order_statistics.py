"""Order statistics: median and percentile over a fanning join.

The subquery engine gets these for free — it pre-aggregates at the grain, so a
plain `percentile_disc` is already looking at distinct rows. The symmetric
engine has no subquery to hide behind, so it encodes.
"""
import pytest
from pydantic import ValidationError

from grain.engine.ontology import Metric


def test_median_renders_as_percentile_disc():
    m = Metric(name="med", grain="track", type="integer", agg="median",
               value="track.milliseconds")
    assert m.sql_expr == (
        "percentile_disc(0.5) within group (order by track.milliseconds)")


def test_percentile_renders_with_its_own_p():
    m = Metric(name="p90", grain="track", type="integer", agg="percentile",
               percentile=0.9, value="track.milliseconds")
    assert m.sql_expr == (
        "percentile_disc(0.9) within group (order by track.milliseconds)")


def test_percentile_requires_a_p():
    """A percentile with no p has no meaning, and defaulting it would silently
    answer a question nobody asked."""
    with pytest.raises(ValidationError, match="percentile"):
        Metric(name="p", grain="track", type="integer", agg="percentile",
               value="track.milliseconds")


def test_p_is_refused_on_any_other_aggregate():
    """A field meaningful for exactly one aggregate must be rejected elsewhere,
    or it reads as configuration that silently does nothing."""
    with pytest.raises(ValidationError, match="percentile"):
        Metric(name="s", grain="track", type="integer", agg="sum",
               percentile=0.9, value="track.milliseconds")


@pytest.mark.parametrize("p", [-0.1, 1.1, 2.0])
def test_p_outside_zero_to_one_is_refused(p):
    with pytest.raises(ValidationError):
        Metric(name="p", grain="track", type="integer", agg="percentile",
               percentile=p, value="track.milliseconds")


@pytest.mark.parametrize("p", [0.0, 0.5, 1.0])
def test_the_endpoints_are_legal(p):
    """p=0 is the minimum and p=1 the maximum; both are meaningful."""
    assert Metric(name="p", grain="track", type="integer", agg="percentile",
                  percentile=p, value="track.milliseconds").percentile == p


@pytest.mark.parametrize("agg", ["median", "percentile"])
def test_neither_is_fanout_immune(agg):
    """A fanning join genuinely corrupts an order statistic — the median
    `track.milliseconds` reads 256026 over the fan against a true 255634 — so
    neither may join FANOUT_IMMUNE."""
    kw = {"percentile": 0.5} if agg == "percentile" else {}
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds", **kw)
    assert m.fanout_immune is False


def test_an_opaque_expr_metric_is_unaffected():
    """The validator must not fire for a metric that declares no agg at all."""
    m = Metric(name="e", grain="track", type="integer",
               expr="percentile_disc(0.5) within group (order by track.milliseconds)")
    assert m.percentile is None
    assert m.is_structured is False
