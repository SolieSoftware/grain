"""The facts a MetricPlan stores must agree with the verdict it reports.

This file is the guard the design calls for: a plan records what it DECIDED,
and the claim about its result is derived from those decisions. Where the two
can disagree, they eventually do — three Criticals so far."""
import pytest

from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.unit


def _plan(onto, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return analyse(rq).metric_plans[0]


def test_a_non_additive_verdict_always_has_a_fact_behind_it(chinook_ontology):
    """Every False verdict must be explained by a stored fact. A verdict with
    no fact behind it is the shape that produced the stock defect.

    Playlist's only unique key is `id` (chinook ships duplicate playlist
    names), so `group_by=["id"]` is the legal, single-key form — see
    `test_a_unique_key_alongside_a_non_unique_one_is_enough` in
    test_grain_additivity.py. revenue's prefix crosses Playlist_Tracks, a
    many_to_many, so the groups overlap even though each is correct.
    """
    mp = _plan(
        chinook_ontology,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["id"],
    )
    assert mp.additive is False
    assert mp.overlap_link is not None or mp.separated_fan is not None
    assert mp.grouped is True


def test_an_additive_verdict_has_no_overlap_or_separated_fact(chinook_ontology):
    mp = _plan(chinook_ontology, object="InvoiceLine", metrics=["revenue"])
    assert mp.additive is True
    assert mp.overlap_link is None
    assert mp.separated_fan is None
    assert mp.grouped is False
