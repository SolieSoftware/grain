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

BOTH ENGINES, one file. The two derivations are DELIBERATE COPIES of each other
— `engine_symmetric/resolve.py` is already an intentional duplicate for the
reason that applies here too: a shared implementation makes the differential
harness blind, because both engines inherit one bug and agree. The checks over
them are shared even though the derivations are not, which is what makes the
duplication's drift visible. See the section header lower down for what differs.
"""
import ast
import inspect
import textwrap
from dataclasses import dataclass, fields

import pytest

from grain.engine.errors import MetricNotSymmetric
from grain.engine.grain import (
    IRRELEVANT_TO_ADDITIVITY,
    _ADDITIVITY_INPUTS,
    MetricPlan,
    _additivity,
    analyse,
)
from grain.engine.ontology import Metric
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec
from grain.engine_symmetric.grain import (
    IRRELEVANT_TO_ADDITIVITY as SYMMETRIC_IRRELEVANT,
)
from grain.engine_symmetric.grain import (
    _ADDITIVITY_INPUTS as SYMMETRIC_ADDITIVITY_INPUTS,
)
from grain.engine_symmetric.grain import (
    MetricPlan as SymmetricMetricPlan,
)
from grain.engine_symmetric.grain import (
    _additivity as _symmetric_additivity,
)
from grain.engine_symmetric.grain import (
    analyse as symmetric_analyse,
)
from grain.engine_symmetric.resolve import resolve as symmetric_resolve
from grain.engine_symmetric.symmetric import require_eligible


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


def _assert_census_complete(plan_type, inputs, irrelevant):
    """The census itself, applied to a dataclass rather than hard-wired to one.

    Written as a function so the proof below can run it against a type that
    DOES have a forgotten field. A census whose own failure path is never
    exercised is a test of nothing — and the failure path is the entire product
    here, since the passing path is what the codebase already looked like on
    the day `window` was added.
    """
    names = {f.name for f in fields(plan_type)}
    classified = inputs | set(irrelevant)
    unclassified = names - classified
    assert not unclassified, (
        f"MetricPlan fields not classified for additivity: {sorted(unclassified)}. "
        f"Add each to _ADDITIVITY_INPUTS, or to IRRELEVANT_TO_ADDITIVITY with a "
        f"one-line reason it cannot affect whether the column sums to the total."
    )
    assert not (inputs & set(irrelevant)), "a field cannot be both an input and irrelevant"
    stale = classified - names
    assert not stale, f"classified fields that no longer exist: {sorted(stale)}"


def test_every_metric_plan_field_is_classified():
    """A new field on MetricPlan must be declared either an input to the
    additivity derivation or explicitly irrelevant, with a reason.

    This is the point of the exercise. `window` was added beside `additive`
    without either knowing about the other, and the result was a level
    reported as summable — 1153, the exact figure a test pins as wrong.
    Forgetting is now a red test naming the field, not a reading someone has
    to do."""
    _assert_census_complete(MetricPlan, _ADDITIVITY_INPUTS, IRRELEVANT_TO_ADDITIVITY)


def test_the_census_actually_rejects_a_field_nobody_classified():
    """The census's own failure path, proved on a local dataclass.

    Proving it by editing the real `MetricPlan` and reverting would make this a
    one-off ritual performed by whoever remembered — and reverting a source file
    discards whatever else was uncommitted. Done here it is a standing test that
    runs on every suite.
    """
    @dataclass(frozen=True)
    class PlanWithAForgottenField:
        overlap_link: str | None = None
        window: str | None = None
        forgotten_field: int = 0

    with pytest.raises(AssertionError, match="forgotten_field"):
        _assert_census_complete(
            PlanWithAForgottenField, frozenset({"overlap_link", "window"}), {}
        )

    # And the two subtler failures, which a census that only counted names
    # would miss: a field claimed twice, and a classification left behind by a
    # field that has since been deleted.
    with pytest.raises(AssertionError, match="both an input and irrelevant"):
        _assert_census_complete(
            PlanWithAForgottenField,
            frozenset({"overlap_link", "window", "forgotten_field"}),
            {"window": "claimed in both places"},
        )
    with pytest.raises(AssertionError, match="no longer exist"):
        _assert_census_complete(
            PlanWithAForgottenField,
            frozenset({"overlap_link", "window", "forgotten_field"}),
            {"deleted_long_ago": "a classification outliving its field"},
        )


def _attributes_read_off_the_argument(func):
    """Every attribute `func` takes off its own first parameter, by parsing it.

    Records the attribute DIRECTLY on the parameter, never the tail of a chain:
    `plan.metric.name` counts as `metric`, because the census classifies the
    PLAN's fields and `name` is a fact about `Metric`, not about `MetricPlan`.
    Matching only `Attribute(value=Name(param))` gets that for free — the outer
    node of a chain has an `Attribute` for its value, so it does not match — but
    it is the intended reading rather than a happy accident: classifying `name`
    would be classifying a field of the wrong class, and no census of
    `MetricPlan` could ever satisfy it.

    A local `o = plan.overlap_link` followed by `o.link_name` is likewise out of
    scope by design. `overlap_link` is recorded at the binding; the fact's own
    members are its internals, and `OverlapFact` is frozen and derived from a
    decision already made.

    KNOWN ESCAPE FORMS, measured rather than reasoned about, and pinned by
    `test_the_measured_escape_forms_are_the_ones_recorded`. This walk sees an
    `ast.Attribute` whose value is the parameter NAME, so a read reaching the
    plan any other way is invisible to it:

    - `p = plan` then `p.forced_by` — ESCAPES. The binding is `Name`-to-`Name`,
      which records nothing, and `p` is not the parameter's name. Distinct from
      the `o = plan.overlap_link` case above, which is deliberate: there the
      field IS recorded, at the binding.
    - `getattr(plan, "forced_by")` — ESCAPES. It is a `Call`, not an
      `ast.Attribute`, so there is no `attr` to collect.
    - a plain comprehension, `any(plan.grouped for _ in ...)` — does NOT escape.
      `ast.walk` descends into comprehension bodies and the parameter name is
      still in scope there.
    - a comprehension whose loop variable ALIASES the parameter,
      `[plan.grouped for plan in others]` — ESCAPES the intent while still
      being recorded: the name matches, so `grouped` is collected off an object
      that is not the plan. A false positive rather than a false negative, and
      so the harmless direction.

    `_additivity` uses none of these, in either engine. Stated because a known
    gap stated is a gap; a known gap unstated is the exact failure mode — a
    claim and the code under it parting company — that this whole file exists to
    close. Closing them would mean a real scope analysis; the cheap defence is
    that both derivations are short enough to read.

    ALSO COUPLED TO A SINGLE PARAMETER. `fn.args.args[0]` is the only name
    matched, so a derivation taking its facts as separate arguments would have
    reads off the second and later ones go unseen entirely. Both engines'
    `_additivity` therefore take one parameter, deliberately.
    """
    fn = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
    param = fn.args.args[0].arg
    return {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == param
    }


def test_the_derivation_reads_only_classified_fields():
    """The census, checked against the code instead of asserted beside it.

    `test_every_metric_plan_field_is_classified` proves no field was FORGOTTEN.
    It cannot prove the classification is TRUE: a hand-maintained list can say
    `metric` is irrelevant while the function under it dereferences `metric` on
    the next line, and that is precisely what it said until this test was
    written. Reading the answer out of `_additivity` itself is the same move as
    the task — derive the claim from the code rather than maintain it alongside.
    """
    read = _attributes_read_off_the_argument(_additivity)
    unclassified = read - _ADDITIVITY_INPUTS
    assert not unclassified, (
        f"_additivity reads plan attributes that are not additivity inputs: "
        f"{sorted(unclassified)}. Either the derivation should not be reading "
        f"them, or they belong in _ADDITIVITY_INPUTS (and out of "
        f"IRRELEVANT_TO_ADDITIVITY, whose entry is then a false claim)."
    )
    # Not asserted as equality. A field can be an acknowledged input that the
    # current conditions happen not to dereference — `grouped` would be, were
    # the level condition ever expressed some other way — and demanding every
    # input be read would pressure someone into deleting the classification
    # rather than the dead code. The direction that matters is the one that
    # catches a claim the code contradicts.


def test_the_reads_check_catches_an_unclassified_attribute():
    """The failure path, on a local function, for the same reason the census
    proof is local: a check whose failure is never exercised is a check of
    nothing, and reverting an edit to `grain.py` discards uncommitted work.

    Doubles as the pin for the chain rule — `plan.metric.name` must report
    `metric`, and `plan` itself appearing bare must report nothing.
    """
    def _pretend_additivity(plan):
        if plan.window is not None and plan.smuggled_claim:
            return False, f"'{plan.metric.name}' is not summable"
        return True, plan

    read = _attributes_read_off_the_argument(_pretend_additivity)
    assert read == {"window", "smuggled_claim", "metric"}, read
    assert "name" not in read

    unclassified = read - _ADDITIVITY_INPUTS
    assert unclassified == {"smuggled_claim"}


def _parameter_aliases(func):
    """Names bound directly to `func`'s first parameter — the alias escape.

    `p = plan` is invisible to `_attributes_read_off_the_argument`: the binding
    is `Name`-to-`Name`, so nothing is recorded, and every later `p.field` is
    read off a name that is not the parameter's. It is also the escape a
    REFACTOR would plausibly introduce, because an alias is what someone writes
    when a line gets long — which makes it worth checking mechanically rather
    than listing in a docstring.
    """
    fn = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
    param = fn.args.args[0].arg
    return sorted(
        target.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id == param
        for target in node.targets
        if isinstance(target, ast.Name)
    )


def test_the_measured_escape_forms_are_the_ones_recorded():
    """The escape forms in `_attributes_read_off_the_argument`'s docstring, as a
    test rather than a paragraph.

    A documented gap that nothing checks decays into a documented gap that is no
    longer the real one — which is the same species of defect as a stored
    verdict disagreeing with its decisions. So each claim is run.
    """
    def _alias(plan):
        p = plan
        return p.forced_by

    def _getattr_form(plan):
        return getattr(plan, "forced_by")  # noqa: B009

    def _comprehension(plan):
        return any(plan.grouped for _ in range(1))

    def _aliased_loop_variable(plan):
        others = [plan]
        return [plan.grouped for plan in others]

    assert _attributes_read_off_the_argument(_alias) == set()
    assert _attributes_read_off_the_argument(_getattr_form) == set()
    assert _attributes_read_off_the_argument(_comprehension) == {"grouped"}
    # Recorded, but off the loop variable rather than the parameter: a false
    # positive, which is the direction that cannot hide an unclassified read.
    assert _attributes_read_off_the_argument(_aliased_loop_variable) == {"grouped"}

    # The alias checker's own failure path, before it is trusted below.
    assert _parameter_aliases(_alias) == ["p"]
    assert _parameter_aliases(_comprehension) == []

    # And neither real derivation uses either of the two that escape. BOTH are
    # checked, not just `getattr`: an assertion covering one escape form reads
    # as though it covered the class, and the alias is the likelier of the two
    # to arrive by refactor.
    for func in (_additivity, _symmetric_additivity):
        src = textwrap.dedent(inspect.getsource(func))
        assert "getattr(" not in src, f"{func.__module__} uses an invisible read"
        aliases = _parameter_aliases(func)
        assert not aliases, (
            f"{func.__module__} binds its plan parameter to {aliases}; reads off "
            f"that name are invisible to the census check above"
        )


def test_every_irrelevance_carries_a_reason():
    """The reason string IS the deliverable — it records the judgement that was
    missing when `window` was added. An empty or one-word entry passes the
    census while skipping the thinking it exists to force.

    A TRIPWIRE, NOT A GUARANTEE. No mechanical test can tell a real judgement
    from five plausible words; this one stops a blank or a shrug, and the
    reading is still yours. Both engines' tables are checked, and the next
    reader should know which of the two this is."""
    for table in (IRRELEVANT_TO_ADDITIVITY, SYMMETRIC_IRRELEVANT):
        for name, reason in table.items():
            assert len(reason.split()) >= 5, f"{name}: reason too thin to be a judgement"


# ---------------------------------------------------------------------------
# The symmetric engine, whose derivation is a DELIBERATE COPY.
#
# `engine_symmetric/resolve.py` is already an intentional duplicate of
# `engine/resolve.py`, and for the reason that applies here too: a shared
# implementation would make the differential harness blind, because both engines
# would inherit one bug and AGREE. So `_additivity` is duplicated and the checks
# over it are not — this file runs the same census, the same reads check and the
# same irrelevance floor against both, which is what makes the duplication's
# drift visible, the way `test_resolver_parity.py` does for the resolver.
#
# What differs, and must keep differing:
#   - TWO conditions, not three.
#   - The second is NOT the subquery engine's `separated`. It follows the path
#     to the GROUP KEY, which `engine/grain.py` records as a capability the
#     subquery engine lacks — there, a root-measured metric has an empty prefix
#     and only a `KeyBeyondGrain` refusal keeps the verdict from being wrong by
#     accident.
#   - No window condition, because this engine refuses a stock outright.
# ---------------------------------------------------------------------------


def _sym_plan(onto, metadata, **kw):
    rq = symmetric_resolve(QuerySpec(**kw), onto)
    return symmetric_analyse(rq, metadata).metric_plans[0]


def test_the_symmetric_prefix_overlap_fact_agrees_with_its_reason(
    chinook_lite, lite_metadata
):
    """Condition 1, the same shape as the subquery engine's — but the reason
    names the ENCODING rather than the identifying keys, because this engine has
    no `NonAdditiveRefused` to have validated any."""
    mp = _sym_plan(
        chinook_lite,
        lite_metadata,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["id"],
    )
    assert mp.additive is False
    fact = mp.prefix_overlap
    assert fact is not None
    assert mp.group_key_overlap is None
    assert fact.link_name == "Playlist_Tracks"
    assert fact.cardinality == "many_to_many"
    assert fact.grain == "invoice_line"

    reason = mp.non_additive_reason
    assert fact.link_name in reason
    assert fact.cardinality in reason
    assert fact.grain in reason
    assert "the encoding counts every" in reason


def test_the_symmetric_group_key_fact_agrees_with_its_reason(
    chinook_lite, lite_metadata
):
    """Condition 2, and the one the subquery engine cannot express.

    `distinct_employees` is measured at the ROOT's grain, so its prefix is empty
    and condition 1 can never fire — yet every employee sits under every
    ancestor above them, so the column cannot sum to the total. The subquery
    engine refuses this query with `KeyBeyondGrain`
    (`test_immunity_does_not_lift_the_key_beyond_grain_refusal`), which is what
    accidentally keeps it from reporting `additive: true`. Here the path is
    followed to the GROUP KEY and the verdict is reached on purpose.
    """
    mp = _sym_plan(
        chinook_lite,
        lite_metadata,
        object="Employee",
        traverse=[Hop(link="Employee_Manager")],
        metrics=["distinct_employees"],
        group_by=["Employee_Manager.last_name"],
    )
    assert mp.additive is False
    assert mp.prefix_overlap is None
    fact = mp.group_key_overlap
    assert fact is not None
    assert fact.key_name == "Employee_Manager.last_name"
    assert fact.link_name == "Employee_Manager"
    # `effective_cardinality`, not the declared many_to_one: the closure of a
    # recursive link is many_to_many, and that is what makes groups overlap.
    assert fact.cardinality == "many_to_many"
    assert fact.grain == "employee"

    reason = mp.non_additive_reason
    assert fact.key_name in reason
    assert fact.link_name in reason
    assert fact.cardinality in reason
    assert fact.grain in reason


def test_the_symmetric_prefix_condition_wins_when_both_hold(
    chinook_lite, lite_metadata
):
    """Precedence, preserved from the `if additive and overlap is not None`
    guard the derivation replaced. Both facts hold here — the prefix crosses a
    many_to_many AND the group key sits beyond it — and only the prefix one is
    recorded, so the plan cannot claim a reason the caller never reads."""
    mp = _sym_plan(
        chinook_lite,
        lite_metadata,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["Playlist_Tracks.name"],
    )
    assert mp.additive is False
    assert mp.prefix_overlap is not None
    assert mp.group_key_overlap is None
    assert "Playlist_Tracks" in mp.non_additive_reason


def test_an_additive_symmetric_verdict_has_neither_fact(chinook_lite, lite_metadata):
    mp = _sym_plan(chinook_lite, lite_metadata, object="InvoiceLine", metrics=["revenue"])
    assert mp.additive is True
    assert mp.prefix_overlap is None
    assert mp.group_key_overlap is None


def test_every_symmetric_metric_plan_field_is_classified():
    """The same census, over the other engine's own plan type."""
    _assert_census_complete(
        SymmetricMetricPlan, SYMMETRIC_ADDITIVITY_INPUTS, SYMMETRIC_IRRELEVANT
    )


def test_the_symmetric_derivation_reads_only_classified_fields():
    """The same reads check, over the other engine's own derivation. The
    classification is checked against the code rather than maintained beside
    it — see `test_the_derivation_reads_only_classified_fields`."""
    read = _attributes_read_off_the_argument(_symmetric_additivity)
    unclassified = read - SYMMETRIC_ADDITIVITY_INPUTS
    assert not unclassified, (
        f"the symmetric _additivity reads plan attributes that are not "
        f"additivity inputs: {sorted(unclassified)}. Either the derivation "
        f"should not be reading them, or they belong in _ADDITIVITY_INPUTS (and "
        f"out of IRRELEVANT_TO_ADDITIVITY, whose entry is then a false claim)."
    )


def test_the_symmetric_derivation_states_its_missing_condition(lite_metadata):
    """A missing condition looks identical to an unconsidered one.

    The subquery engine has a third condition for a windowed level; this engine
    has none, because it refuses a stock with `MetricNotSymmetric` before a plan
    is built. That absence must be SAID, not inferred by whoever next diffs the
    two functions — the resemblance between 'considered and inapplicable' and
    'never thought about' is the whole failure this derivation removes.

    THE CLAIM AND THE FACT FAIL TOGETHER, which is why the refusal is run here
    rather than left to the stock tests that also cover it. Asserting only the
    docstring's words leaves the premise pinned somewhere else, by tests anyone
    ADDING stock support would edit — after which this docstring is false and
    its own test is still green. That is the same defect as asserting an error's
    message instead of running its named alternative, which has landed in this
    project four times. So the sentence and the behaviour it describes are one
    assertion: give this engine a stock and it must still refuse.
    """
    doc = _symmetric_additivity.__doc__
    assert doc is not None
    assert "MetricNotSymmetric" in doc
    assert "no window condition" in doc

    # Built the way `tests/unit/test_stock.py::_stock` builds one, so the shape
    # under test is the same stock the rest of the suite uses.
    stock = Metric(name="level", grain="invoice", type="decimal", agg="sum",
                   value="invoice.total", quantity="stock",
                   over_time={"dimension": "when", "choice": "last"})
    with pytest.raises(MetricNotSymmetric):
        require_eligible(stock, lite_metadata)


def test_the_two_derivations_are_not_the_same_object():
    """The duplication, pinned. Deduplicating these would make the differential
    harness blind to a bug in the derivation, because both engines would inherit
    it and agree — the same reason `engine_symmetric/resolve.py` is a copy, and
    the same reason the defect this plan closes escaped.

    So the drift is made visible instead: every check above runs over both.
    """
    assert _additivity is not _symmetric_additivity
    assert _additivity.__module__ != _symmetric_additivity.__module__
    # And the difference that must survive: two conditions here, three there.
    assert "window" in _ADDITIVITY_INPUTS
    assert "window" not in SYMMETRIC_ADDITIVITY_INPUTS
