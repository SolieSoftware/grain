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

NO DATABASE, ANYWHERE IN THIS FILE. Every assertion here is about an ontology
and a plan derived from it; not one executes a query. Five of these tests took
`chinook_ontology` anyway, which needs `GRAIN_DATABASE_URL` and so SKIPPED
without one — and this project's characteristic trap is precisely a green run
that skipped the tests which would have failed, which is why `CLAUDE.md` tells
readers to check the skip count rather than the colour. A test that joins the
skippable set for no reason works against that number. `chinook_lite` declares
every object, link and metric these need and loads from `lite_metadata`, so the
claims below hold in both configurations.
"""
import ast
import functools
import inspect
import textwrap
from dataclasses import dataclass, fields
from typing import ClassVar, get_origin, get_type_hints

import pytest

from grain.engine.errors import MetricNotSymmetric
from grain.engine.grain import (
    IRRELEVANT_TO_ADDITIVITY,
    _ADDITIVITY_INPUTS,
    GrainPlan,
    MetricPlan,
    OverlapFact,
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
    GrainPlan as SymmetricGrainPlan,
)
from grain.engine_symmetric.grain import (
    MetricPlan as SymmetricMetricPlan,
)
from grain.engine_symmetric.grain import (
    PrefixOverlap,
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


def test_a_non_additive_verdict_always_has_a_fact_behind_it(chinook_lite):
    """Every False verdict must be explained by a stored fact, and the fact's
    own fields must be the ones the live reason string actually names — not
    merely present, but correct.

    Playlist's only unique key is `id` — chinook ships duplicate playlist names,
    and `chinook_lite` mirrors that by declaring `name` nullable and non-unique
    — so `group_by=["id"]` is the legal, single-key form. See
    `test_a_unique_key_alongside_a_non_unique_one_is_enough` in
    test_grain_additivity.py. revenue's prefix crosses Playlist_Tracks, a
    many_to_many, so the groups overlap even though each is correct.
    """
    mp = _plan(
        chinook_lite,
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


def test_a_separated_fan_verdict_also_has_a_fact_that_agrees_with_the_reason(chinook_lite):
    """The mirror shape: `employee_count` is measured at the ROOT's grain, so
    its prefix is empty and no `OverlapFact` can fire — but the fanning
    `Employee_Manager` hop downstream of it is pinned by a unique group key,
    which is exactly the `SeparatedFan` case. Same agreement requirement as
    above: the fact's fields must be the ones the reason names.
    """
    mp = _plan(
        chinook_lite,
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


def test_an_additive_verdict_has_no_overlap_or_separated_fact(chinook_lite):
    mp = _plan(chinook_lite, object="InvoiceLine", metrics=["revenue"])
    assert mp.additive is True
    assert mp.overlap_link is None
    assert mp.separated_fan is None
    assert mp.grouped is False


# The claims the census is a census OF. They are neither inputs to the
# derivation nor irrelevant to it — they ARE it, so they need their own bucket
# rather than a special case inside the walk.
#
# This bucket exists because of the escape the derivation task itself opened.
# The census used to read `dataclasses.fields()` alone, and `additive` /
# `non_additive_reason` stopped being fields on this branch: a second claim
# added the same way — `@property safe_to_total` — was invisible to it, and the
# whole suite stayed green. A census that cannot see the shape the code now uses
# is a census of the shape the code used to have.
THE_VERDICT = frozenset({"additive", "non_additive_reason"})


def _claim_bearing_names(plan_type):
    """Every name on `plan_type` that can carry a claim about the result.

    EVERY PUBLIC MEMBER, not fields alone and not fields plus `property` alone.
    A derived verdict is a property, so a census counting only fields would be
    blind to exactly the shape this branch introduced — measured, not supposed:
    adding `safe_to_total` as a property left all 590 tests green. Widening it
    to `isinstance(v, property)` closed that one shape and left two others open,
    BOTH MEASURED on the real `MetricPlan` in this worktree, both green at
    593/0:

        SAFE_TO_TOTAL_CLASSVAR: ClassVar[bool] = True

        @functools.cached_property
        def safe_to_total_cached(self) -> bool: return True

    Neither is self-defeating. `functools.cached_property` works on a frozen
    dataclass — it writes through the instance `__dict__`, which `__setattr__`
    never sees — and it is the OBVIOUS optimisation here, because the two
    verdict properties call `_additivity` twice per plan. Reaching for it would
    have silently removed the guard. A `ClassVar` simply reads. `isinstance(v,
    property)` is False for a `cached_property`, and a `ClassVar` is neither a
    field nor a class-level descriptor, so the old walk saw neither.

    So the walk is now by EXCLUSION rather than by enumerating descriptor types:
    every public name on the class, whatever shape it has, plus the fields
    (which include ones with no class-level default) plus `ClassVar`
    annotations carrying no value. Enumerating the shapes is what failed twice;
    `property`, `cached_property`, any other `__get__`, a bare class constant
    and a `__slots__` entry are all covered by not asking what shape a name is.
    Methods ARE now swept in, reversing the note this docstring used to carry:
    the AST check reads `_additivity` and nothing else, so it was never the
    thing covering a claim written as `def safe_to_total(self)`.

    KNOWN ESCAPE FORMS, measured rather than reasoned about, and pinned by
    `test_the_measured_census_escape_forms_are_the_ones_recorded`:

    - a property on a custom METACLASS — ESCAPES. `type.__dir__` merges the
      class's own MRO only, so a metaclass's attributes are not listed and
      nothing here looks at `type(plan_type)`.
    - `__getattr__` on that metaclass, synthesising the name on access —
      ESCAPES, and cannot not: there is no static name to find.
    - an attribute set in `__post_init__` via `object.__setattr__` and never
      declared — ESCAPES. It is on the INSTANCE; this walk reads the class.
    - a leading-underscore name, `_safe_to_total` — ESCAPES, deliberately. The
      filter is what keeps the dunders out, and a private name is not the claim
      a caller reads.

    All four are stated for the reason the AST check's escapes are: a known gap
    stated is a gap, and a known gap unstated is the exact defect — a claim and
    the code under it parting company — this file exists to close. Closing the
    first three would mean reading `type(plan_type)` and giving up on static
    inspection entirely; the cheap defence is that a plan type here is a plain
    frozen dataclass with no metaclass, which `test_the_plan_types_are_the_shape
    _the_census_can_see` asserts rather than assumes.
    """
    names = {f.name for f in fields(plan_type)}
    names |= {name for name in dir(plan_type) if not name.startswith("_")}
    names |= {
        name
        for name, hint in get_type_hints(plan_type).items()
        if not name.startswith("_")
        and (hint is ClassVar or get_origin(hint) is ClassVar)
    }
    return names


def _assert_census_complete(plan_type, inputs, irrelevant, verdict=THE_VERDICT):
    """The census itself, applied to a dataclass rather than hard-wired to one.

    Written as a function so the proof below can run it against a type that
    DOES have a forgotten field. A census whose own failure path is never
    exercised is a test of nothing — and the failure path is the entire product
    here, since the passing path is what the codebase already looked like on
    the day `window` was added.
    """
    names = _claim_bearing_names(plan_type)
    classified = inputs | set(irrelevant) | set(verdict)
    unclassified = names - classified
    assert not unclassified, (
        f"MetricPlan members not classified for additivity: "
        f"{sorted(unclassified)}. Add each to _ADDITIVITY_INPUTS, or to "
        f"IRRELEVANT_TO_ADDITIVITY with a one-line reason it cannot affect "
        f"whether the column sums to the total — or, if it is itself a claim "
        f"about the result rather than an input to one, to THE_VERDICT."
    )
    assert not (inputs & set(irrelevant)), "a field cannot be both an input and irrelevant"
    assert not (set(verdict) & (inputs | set(irrelevant))), (
        "a name cannot be both the verdict and an input to it"
    )
    stale = classified - names
    assert not stale, f"classified fields that no longer exist: {sorted(stale)}"


def test_every_metric_plan_field_is_classified():
    """A new PUBLIC MEMBER of MetricPlan, whatever its shape, must be declared
    either an input to
    the additivity derivation, explicitly irrelevant with a reason, or the
    verdict itself.

    This is the point of the exercise. `window` was added beside `additive`
    without either knowing about the other, and the result was a level
    reported as summable — 1153, the exact figure a test pins as wrong.
    Forgetting is now a red test naming the field, not a reading someone has
    to do.

    Properties are swept because THIS BRANCH made the verdict one. A census of
    `dataclasses.fields()` alone stopped covering the shape the code uses the
    moment `additive` became a property, and a second claim added the same way
    was measured to leave the whole suite green. The name is unchanged because
    the design doc cross-references it."""
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

    # `verdict=frozenset()` because this local type has no derived verdict at
    # all; classifying two names it does not carry would trip the stale check
    # before the failure under test could fire.
    with pytest.raises(AssertionError, match="forgotten_field"):
        _assert_census_complete(
            PlanWithAForgottenField, frozenset({"overlap_link", "window"}), {},
            verdict=frozenset(),
        )

    # And the two subtler failures, which a census that only counted names
    # would miss: a field claimed twice, and a classification left behind by a
    # field that has since been deleted.
    with pytest.raises(AssertionError, match="both an input and irrelevant"):
        _assert_census_complete(
            PlanWithAForgottenField,
            frozenset({"overlap_link", "window", "forgotten_field"}),
            {"window": "claimed in both places"},
            verdict=frozenset(),
        )
    with pytest.raises(AssertionError, match="no longer exist"):
        _assert_census_complete(
            PlanWithAForgottenField,
            frozenset({"overlap_link", "window", "forgotten_field"}),
            {"deleted_long_ago": "a classification outliving its field"},
            verdict=frozenset(),
        )


def test_the_census_actually_rejects_a_property_nobody_classified():
    """The escape this branch opened, closed and then proved closed.

    Turning `additive` into a property is what made a census of
    `dataclasses.fields()` insufficient: the claim moved out of the set the
    census could see, and any NEXT claim added the same way — a second opinion
    about the result, contradicting the derivation for every non-additive plan —
    passed unnoticed. Measured on the real `MetricPlan` during review: 590
    passed, green.

    Local rather than by editing `grain.py`, for the same reason the field proof
    above is local: a proof performed by hand and reverted is a ritual, and this
    one runs on every suite.
    """
    @dataclass(frozen=True)
    class PlanWithAnUnclassifiedClaim:
        overlap_link: str | None = None

        @property
        def additive(self) -> bool:
            return self.overlap_link is None

        @property
        def safe_to_total(self) -> bool:
            return True

    with pytest.raises(AssertionError, match="safe_to_total"):
        _assert_census_complete(
            PlanWithAnUnclassifiedClaim,
            frozenset({"overlap_link"}),
            {},
            verdict=frozenset({"additive"}),
        )

    # And the classified verdict itself is accepted, so the check above is
    # rejecting the UNCLASSIFIED claim and not merely every property.
    _assert_census_complete(
        PlanWithAnUnclassifiedClaim,
        frozenset({"overlap_link"}),
        {},
        verdict=frozenset({"additive", "safe_to_total"}),
    )


def test_the_census_rejects_the_two_shapes_a_property_walk_missed():
    """The second escape, and the reason the walk stopped enumerating shapes.

    `isinstance(v, property)` closed the shape THIS branch introduced and left
    two open that were measured on the real `MetricPlan` in this worktree: a
    `ClassVar`, which is neither a field nor a class-level descriptor, and a
    `functools.cached_property`, for which `isinstance(v, property)` is False.
    Both left the suite at 593 passed, 0 skipped.

    `cached_property` is the one that matters. It is not a hypothetical shape
    someone might contrive — `additive` and `non_additive_reason` each call
    `_additivity`, so the plan computes its verdict twice per read and caching
    is the obvious thing to reach for. It works on a frozen dataclass (it writes
    through the instance `__dict__`, so `__setattr__` is never consulted), which
    means reaching for it would have removed the guard and said nothing.

    Local for the reason the two proofs above are local: a proof performed by
    hand on `grain.py` and reverted is a ritual, and this one runs every suite.
    """
    @dataclass(frozen=True)
    class PlanWithTheTwoEscapes:
        overlap_link: str | None = None

        SAFE_TO_TOTAL_CLASSVAR: ClassVar[bool] = True
        # A ClassVar with no value at all — invisible to `dir`, which is why
        # the annotations are read as well as the class.
        SAFE_TO_TOTAL_UNVALUED: ClassVar[bool]

        @property
        def additive(self) -> bool:
            return self.overlap_link is None

        @functools.cached_property
        def safe_to_total_cached(self) -> bool:
            return True

    for escaped in (
        "SAFE_TO_TOTAL_CLASSVAR", "SAFE_TO_TOTAL_UNVALUED", "safe_to_total_cached",
    ):
        with pytest.raises(AssertionError, match=escaped):
            _assert_census_complete(
                PlanWithTheTwoEscapes,
                frozenset({"overlap_link"}),
                {},
                verdict=frozenset({"additive"}),
            )

    # And classified, the census passes — so the check above is rejecting the
    # unclassified claim rather than every member of a shape it cannot name.
    _assert_census_complete(
        PlanWithTheTwoEscapes,
        frozenset({"overlap_link"}),
        {},
        verdict=frozenset({
            "additive", "SAFE_TO_TOTAL_CLASSVAR", "SAFE_TO_TOTAL_UNVALUED",
            "safe_to_total_cached",
        }),
    )

    # `cached_property` really does work on a frozen dataclass, which is the
    # whole reason it is a live risk rather than a contrived one. Asserted, not
    # assumed: if it ever raised, the shape would be self-defeating and this
    # test would be guarding against nothing.
    assert PlanWithTheTwoEscapes().safe_to_total_cached is True


def test_the_measured_census_escape_forms_are_the_ones_recorded():
    """`_claim_bearing_names`'s documented escapes, run rather than asserted in
    prose — the same treatment `test_the_measured_escape_forms_are_the_ones_
    recorded` gives the AST check's. A documented gap nothing exercises decays
    into a documented gap that is no longer the real one.
    """
    class Meta(type):
        @property
        def meta_claim(cls) -> bool:
            return True

        def __getattr__(cls, name):
            return True

    @dataclass(frozen=True)
    class PlanBehindAMetaclass(metaclass=Meta):
        overlap_link: str | None = None

    names = _claim_bearing_names(PlanBehindAMetaclass)
    # `type.__dir__` merges the class's own MRO only.
    assert "meta_claim" not in names
    assert PlanBehindAMetaclass.meta_claim is True
    # And the dynamic form, which no static walk can see.
    assert "synthesised_claim" not in names
    assert PlanBehindAMetaclass.synthesised_claim is True

    @dataclass(frozen=True)
    class PlanWithAnInstanceAttribute:
        overlap_link: str | None = None

        def __post_init__(self):
            object.__setattr__(self, "safe_to_total_instance", True)

        @property
        def _safe_to_total_private(self) -> bool:
            return True

    names = _claim_bearing_names(PlanWithAnInstanceAttribute)
    assert "safe_to_total_instance" not in names
    assert PlanWithAnInstanceAttribute().safe_to_total_instance is True
    assert "_safe_to_total_private" not in names

    # The other side of the line, and the point of walking by exclusion: shapes
    # nobody enumerated are caught anyway. A bare class constant carries a claim
    # just as a `ClassVar` does, and a `__slots__` entry is a descriptor whose
    # type nothing here names.
    @dataclass(frozen=True)
    class PlanWithUnenumeratedShapes:
        __slots__ = ("overlap_link", "safe_to_total_slot")
        overlap_link: str | None

    assert "safe_to_total_slot" in _claim_bearing_names(PlanWithUnenumeratedShapes)

    @dataclass(frozen=True)
    class PlanWithABareConstant:
        overlap_link: str | None = None
        SAFE_TO_TOTAL_BARE = True

    assert "SAFE_TO_TOTAL_BARE" in _claim_bearing_names(PlanWithABareConstant)


def test_the_plan_types_are_the_shape_the_census_can_see():
    """The three escapes above are survivable only while no plan type uses them.

    Stated as an assertion rather than a hope: both engines' `MetricPlan` must
    stay a plain class with no custom metaclass and no `__getattr__`, because a
    census that cannot see those shapes is a census of the shape the code used
    to have — which is exactly how this guard failed twice already.
    """
    for plan_type in (MetricPlan, SymmetricMetricPlan):
        assert type(plan_type) is type, (
            f"{plan_type.__module__}.MetricPlan has a custom metaclass; its "
            f"attributes are invisible to the census"
        )
        assert "__getattr__" not in vars(plan_type), (
            f"{plan_type.__module__}.MetricPlan synthesises attributes "
            f"dynamically; the census cannot enumerate them"
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
    """The same census, over the other engine's own plan type — fields and
    properties alike, since this engine's verdict is a property too."""
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


# ---------------------------------------------------------------------------
# Parity, and the layer above.
#
# The two checks that no amount of per-engine testing gives you: that the
# deliberate duplication has not DRIFTED, and that the verdict actually reaches
# the caller who has to act on it.
# ---------------------------------------------------------------------------

# Shapes both engines answer, over the ontology both can load. Deliberately
# spanning both verdicts: a parity test over non-additive shapes alone would
# pass with a derivation that returned False for everything.
#
# `chinook_lite`: parity is a claim about two derivations reading one ontology,
# and the symmetric engine's `analyse` needs the MetaData that ontology was
# loaded from. Using the lite pair keeps both halves reading the same thing and
# needs no database, so the parity claim cannot quietly become a skip.
#
# That argument was written here first and applies to EVERY test in this file;
# it is now carried across — see the module docstring.
_SHAPES_BOTH_ENGINES_ANSWER = [
    dict(object="InvoiceLine", metrics=["revenue"]),
    dict(object="Invoice", traverse=[Hop(link="Invoice_Lines")], metrics=["revenue"]),
    dict(object="Customer", traverse=[Hop(link="Customer_Invoices")],
         metrics=["distinct_customers"], group_by=["country"]),
    dict(object="Customer", traverse=[Hop(link="Customer_Invoices"),
         Hop(link="Invoice_Lines")], metrics=["revenue"], group_by=["country"]),
    dict(object="Playlist", traverse=[Hop(link="Playlist_Tracks"),
         Hop(link="Track_InvoiceLines")], metrics=["revenue"], group_by=["id"]),
    dict(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
         metrics=["distinct_tracks"], group_by=["id"]),
    dict(object="Employee", traverse=[Hop(link="Employee_Manager")],
         metrics=["distinct_employees"], group_by=["Employee_Manager.id"]),
]


def test_both_engines_agree_on_additivity_where_both_answer(chinook_lite, lite_metadata):
    """The derivations are deliberately duplicated, so drift is possible by
    design — this is `test_resolver_parity.py`'s job done for the verdict.

    Only the VERDICT is compared. The reason strings differ legitimately and
    must keep differing: the symmetric engine says the encoding counts each row
    once, the subquery engine names the identifying keys its own
    `NonAdditiveRefused` validated.

    WHERE BOTH ANSWER is a real restriction, not a hedge. The symmetric engine
    has a condition the subquery engine lacks — a group key beyond the metric's
    prefix — and on that one shape (`distinct_employees` grouped by
    `Employee_Manager.last_name`) the subquery engine raises `KeyBeyondGrain`
    instead of answering, which is the accident recorded beside
    `_ADDITIVITY_INPUTS` and pinned by
    `test_immune_aggregates.py::test_immunity_does_not_lift_the_key_beyond_grain_refusal`.
    It is excluded here because a refusal is not a disagreement; if that refusal
    is ever lifted without the derivation being fixed, that test goes red, not
    this one.
    """
    verdicts = []
    for kw in _SHAPES_BOTH_ENGINES_ANSWER:
        sub = analyse(resolve(QuerySpec(**kw), chinook_lite)).additive
        sym = symmetric_analyse(
            symmetric_resolve(QuerySpec(**kw), chinook_lite), lite_metadata
        ).additive
        assert sub == sym, f"engines disagree on additivity for {kw}: {sub} vs {sym}"
        verdicts.append(sub)

    # Both answers present, so the parity above is not vacuous.
    assert True in verdicts and False in verdicts, verdicts


def _additive_metric(name):
    return Metric(name=name, grain="invoice_line", type="decimal", agg="sum",
                  value="invoice_line.unit_price")


def test_a_mixed_plan_is_non_additive_and_reports_the_offending_metric():
    """`GrainPlan.additive` folds the per-metric verdicts with `all`, and the
    fold is the last mile: `EnginePlan`, `Result`, `agent/tools.py`, the CLI and
    the MCP server all read this one boolean, never the per-metric ones.

    HAND-BUILT, and deliberately so. Review measured that NO chinook query
    produces a mixed plan — every root × path ≤ 4 hops × metric pair × group key
    makes either all its metrics non-additive or none of them — so `all(...)` and
    `self.metric_plans[0].additive` are indistinguishable on the corpus, and
    replacing one with the other left all 590 tests green. The fold was
    unreachable by accident of the fixture, not by construction. Two plans built
    here reach it directly; waiting for a fixture that happens to produce one is
    waiting for the corpus to change.

    Order matters in the construction: the additive plan comes FIRST, so a fold
    that read only the first metric would return True and this test would be the
    thing that says so.
    """
    additive = MetricPlan(metric=_additive_metric("revenue"), strategy="inline")
    assert additive.additive is True

    non_additive = MetricPlan(
        metric=_additive_metric("track_revenue"),
        strategy="inline",
        grouped=True,
        overlap_link=OverlapFact(
            link_name="Playlist_Tracks",
            cardinality="many_to_many",
            identifying_keys="id",
            subject="Playlist",
        ),
    )
    assert non_additive.additive is False

    plan = GrainPlan(metric_plans=[additive, non_additive])
    assert plan.additive is False
    # And the reason the caller reads is the NON-additive metric's own, not a
    # generic one and not `None` — a False verdict whose reason came back empty
    # would render a caveat with nothing in it.
    assert plan.non_additive_reason == non_additive.non_additive_reason
    assert "Playlist_Tracks" in plan.non_additive_reason
    assert "track_revenue" in plan.non_additive_reason

    # The all-additive fold, without which the assertion above passes for a
    # `GrainPlan` that reports False unconditionally.
    assert GrainPlan(metric_plans=[additive, additive]).additive is True
    assert GrainPlan(metric_plans=[additive, additive]).non_additive_reason is None


def test_a_mixed_symmetric_plan_is_non_additive_and_reports_the_offending_metric():
    """The same fold, in the other engine's own copy. `GrainPlan` is duplicated
    for the reason `_additivity` is — a shared one would make the differential
    harness blind — so a fix to one is not a fix to the other, and the mutation
    that replaced `all(...)` with the first metric left 590 green HERE too."""
    additive = SymmetricMetricPlan(
        metric=_additive_metric("revenue"), strategy="inline"
    )
    assert additive.additive is True

    non_additive = SymmetricMetricPlan(
        metric=_additive_metric("track_revenue"),
        strategy="symmetric",
        prefix_overlap=PrefixOverlap(
            link_name="Playlist_Tracks",
            cardinality="many_to_many",
            grain="invoice_line",
        ),
    )
    assert non_additive.additive is False

    plan = SymmetricGrainPlan(metric_plans=[additive, non_additive])
    assert plan.additive is False
    assert plan.non_additive_reason == non_additive.non_additive_reason
    assert "Playlist_Tracks" in plan.non_additive_reason
    assert "track_revenue" in plan.non_additive_reason

    assert SymmetricGrainPlan(metric_plans=[additive, additive]).additive is True
    assert (
        SymmetricGrainPlan(metric_plans=[additive, additive]).non_additive_reason
        is None
    )


class _ResultWithTheEngineVerdict:
    """The shape `agent/tools.py` reads, carrying a REAL plan's verdict.

    Only the two additivity fields are real; rows, columns and the other two
    caveat sources are filler, because what is under test is whether the verdict
    survives the trip from the derivation to the text a model reads. Executing
    the query would add a database to a claim that has nothing to do with one —
    the rendering is pure given a result.
    """

    def __init__(self, plan):
        self.additive = plan.additive
        self.non_additive_reason = plan.non_additive_reason
        self.rows = [(1, "2.99")]
        self.columns = ["id", "revenue"]
        self.limit_reached = False
        self.rewrites = []


class _GrainReturning:
    def __init__(self, result):
        self._result = result

    def query(self, spec):
        return self._result


_NON_ADDITIVE_SPEC = {
    "object": "Playlist",
    "traverse": [{"link": "Playlist_Tracks"}, {"link": "Track_InvoiceLines"}],
    "metrics": ["revenue"],
    "group_by": ["id"],
}


def test_the_agent_is_told_not_to_add_a_non_additive_column(chinook_lite):
    """The flag's only job is to reach the caller, so the derivation is checked
    through the layer that acts on it.

    A correct verdict that never renders is the same defect one layer along —
    and that is not hypothetical: the defect this whole plan closes reached the
    model as ADVICE, because `agent/tools.py` emits its caveat only when
    `additive` is False and the prompt tells the model to total otherwise. So
    the assertion runs the real derivation, hands its verdict to the real
    `run()`, and reads the text.
    """
    from grain.agent import tools

    mp = _plan(
        chinook_lite,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["id"],
    )
    assert mp.additive is False
    assert mp.non_additive_reason is not None

    text, is_error = tools.run(
        _GrainReturning(_ResultWithTheEngineVerdict(mp)), _NON_ADDITIVE_SPEC
    )
    assert not is_error
    assert "NOT ADDITIVE" in text
    assert "do NOT add them together" in text
    # The derivation's own reason, verbatim, not a generic warning: the fact
    # that made the verdict False is what the model needs in order to say
    # anything useful about the rows.
    assert mp.non_additive_reason in text
    assert "Playlist_Tracks" in text


def test_an_additive_verdict_renders_no_caveat(chinook_lite):
    """The other half, without which the check above passes for a renderer that
    warns on everything — and a warning on every result is a warning on none."""
    from grain.agent.tools import _caveats

    mp = _plan(chinook_lite, object="InvoiceLine", metrics=["revenue"])
    assert mp.additive is True
    assert _caveats(_ResultWithTheEngineVerdict(mp)) == []
