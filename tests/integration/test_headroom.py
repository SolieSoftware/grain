"""Condition (b) of the symmetric encoding, checked against real data.

`|v| < K/2` is what makes distinct keys produce distinct encoded terms. At
K = 1e30 the bound is 5e29, unreachable for monetary and count data — but
"unreachable" was reasoning about money, not a measurement.
"""
import pytest
from sqlalchemy import MetaData

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.errors import OntologyError
from grain.engine.loader import _check_symmetric_headroom, load_ontology
from grain.engine.ontology import Metric, Ontology

pytestmark = pytest.mark.integration


def test_the_chinook_pack_has_headroom(db_engine, chinook_metadata):
    """Passes today. Its job is to fail the day a domain pack arrives whose
    values approach the bound."""
    load_ontology(CHINOOK_DIR / "ontology.yaml", chinook_metadata, db_engine)


def test_a_metric_exceeding_the_bound_is_refused(db_engine, chinook_metadata):
    """chinook's largest unit_price is 1.99, so this observes 5.97e29 — just
    over the 5e29 bound. The first draft of this test multiplied by 1e29 and
    observed only 1.99e29, so it passed while proving nothing."""
    onto = Ontology(name="t", metrics={
        "huge": Metric(name="huge", grain="invoice_line", type="decimal",
                       agg="sum", value="invoice_line.unit_price * 3e29"),
    })
    with pytest.raises(OntologyError, match="headroom"):
        _check_symmetric_headroom(onto, chinook_metadata, db_engine)


def test_a_metric_just_inside_the_bound_is_accepted(db_engine, chinook_metadata):
    """The other side of the same boundary: 1.99 * 2e29 = 3.98e29, under 5e29.
    A check that only ever refuses is not a check."""
    onto = Ontology(name="t", metrics={
        "big": Metric(name="big", grain="invoice_line", type="decimal",
                      agg="sum", value="invoice_line.unit_price * 2e29"),
    })
    _check_symmetric_headroom(onto, chinook_metadata, db_engine)


def test_ineligible_metrics_are_not_refused_at_load(db_engine, chinook_metadata):
    """Eligibility is per-ENGINE and belongs to the symmetric planner. Refusing
    an opaque metric here would make an ontology containing one fail to load for
    the subquery engine too, which serves it correctly."""
    onto = Ontology(name="t", metrics={
        "opaque": Metric(name="opaque", grain="invoice_line", type="decimal",
                         expr="sum(invoice_line.unit_price * 3e29)"),
    })
    _check_symmetric_headroom(onto, chinook_metadata, db_engine)


def test_loading_without_an_engine_still_works(chinook_metadata):
    """Structural validation must stay hermetic — the unit suite loads
    ontologies with no database at all."""
    onto = load_ontology(CHINOOK_DIR / "ontology.yaml", chinook_metadata)
    assert "revenue" in onto.metrics
