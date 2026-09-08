"""The facts a MetricPlan stores must agree with the verdict it reports.

This file is the guard the design calls for: a plan records what it DECIDED,
and the claim about its result is derived from those decisions. Where the two
can disagree, they eventually do — three Criticals so far.

Every assertion below checks VALUES, not merely non-nullness: a stored fact
that names the wrong link, key or subject would pass an `is not None` check
while still being exactly the defect this whole plan exists to close — a
decision and the claim describing it parting company. So each fact's fields
are checked against the live `non_additive_reason` string: if the fact says
`Playlist_Tracks`, the reason must say so too, because that is precisely the
agreement the later derivation task will depend on.
"""
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def _plan(onto, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return analyse(rq).metric_plans[0]


def test_a_non_additive_verdict_always_has_a_fact_behind_it(chinook_ontology):
    """Every False verdict must be explained by a stored fact, and the fact's
    own fields must be the ones the live reason string actually names — not
    merely present, but correct.

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
    assert mp.grouped is True

    fact = mp.overlap_link
    assert fact is not None
    assert mp.separated_fan is None
    assert fact.link_name == "Playlist_Tracks"
    assert fact.cardinality == "many_to_many"
    assert fact.identifying_keys == "id"
    assert fact.subject == "Playlist"
    # Agreement, not just presence: every field the fact reports must be
    # traceable in the reason the caller actually reads.
    reason = mp.non_additive_reason
    assert fact.link_name in reason
    assert fact.cardinality in reason
    assert fact.identifying_keys in reason
    assert fact.subject in reason


def test_a_separated_fan_verdict_also_has_a_fact_that_agrees_with_the_reason(chinook_ontology):
    """The mirror shape: `employee_count` is measured at the ROOT's grain, so
    its prefix is empty and no `OverlapFact` can fire — but the fanning
    `Employee_Manager` hop downstream of it is pinned by a unique group key,
    which is exactly the `SeparatedFan` case. Same agreement requirement as
    above: the fact's fields must be the ones the reason names.
    """
    mp = _plan(
        chinook_ontology,
        object="Employee",
        traverse=[Hop(link="Employee_Manager")],
        metrics=["employee_count"],
        group_by=["Employee_Manager.id"],
    )
    assert mp.additive is False
    assert mp.grouped is True

    fact = mp.separated_fan
    assert fact is not None
    assert mp.overlap_link is None
    assert fact.link_name == "Employee_Manager"
    assert fact.cardinality == "many_to_many"
    assert fact.to_object_name == "Employee"
    assert fact.pin_name == "Employee_Manager.id"
    assert fact.grain == "employee"
    reason = mp.non_additive_reason
    assert fact.link_name in reason
    assert fact.cardinality in reason
    assert fact.to_object_name in reason
    assert fact.pin_name in reason
    assert fact.grain in reason


def test_an_additive_verdict_has_no_overlap_or_separated_fact(chinook_ontology):
    mp = _plan(chinook_ontology, object="InvoiceLine", metrics=["revenue"])
    assert mp.additive is True
    assert mp.overlap_link is None
    assert mp.separated_fan is None
    assert mp.grouped is False
