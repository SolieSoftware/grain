# Derived Additivity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `MetricPlan`'s `additive` / `non_additive_reason` a derivation over the finished plan, in both engines, so a decision and the claim describing it cannot part company.

**Architecture:** `analyse()` stores the FACTS its decisions produced (the prefix edges it walked, the identifying keys a refusal already validated, the separated fan it found, whether the query was grouped). `additive` and `non_additive_reason` stop being constructor arguments and become properties delegating to a pure `_additivity(plan)`. A field census test then fails when a new `MetricPlan` field is added without being classified as an input to that derivation or explicitly declared irrelevant.

**Tech Stack:** Python 3.12, frozen dataclasses, pytest, SQLAlchemy 2.x (untouched by this plan).

**Spec:** `docs/plans/2026-09-06-derived-additivity-design.md`

## Global Constraints

- Test command, and the URL form is load-bearing: `export GRAIN_DATABASE_URL="postgresql+psycopg://solshortland@localhost:5432/chinook"` then `uv run pytest -q`. **Check the skip count is 0, not merely that the run is green** — unset or plain `postgresql://` makes most tests skip while still reporting success.
- Baseline at the start of this plan: **570 passed, 0 skipped**.
- `uv run ruff check src tests tools` must pass. Line length 100.
- Measured anchors must not move: `revenue` 2328.60, naive fanned 5738.28, median 255634 vs fanned 256026.
- `uv run python tools/sweep.py` must stay 74-11-3-2 with **0 WRONG**.
- **Every existing additivity test passes UNCHANGED.** They are the specification. If the derivation requires editing one, the derivation is wrong. The only permitted test edits are ADDING tests.
- `_additivity` must never raise. Refusals stay in `analyse()`.
- `engine/` never imports from `domains/` or an adapter. The two engines share only the loaded ontology — do NOT factor `_additivity` into a shared module.
- Every failure is raised before a database connection is acquired, except `GuardTripped` and a date-valued filter.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/grain/engine/grain.py` | subquery engine's `MetricPlan`, `analyse`, and its new `_additivity` | modify |
| `src/grain/engine_symmetric/grain.py` | symmetric engine's own copy of the same | modify |
| `tests/unit/test_additivity_derivation.py` | census + derivation tests, both engines | create |
| `tests/unit/test_grain_additivity.py` | existing spec for the subquery verdict | must pass unchanged |
| `tests/unit/test_symmetric_anchors.py`, `tests/integration/test_symmetric_anchors.py` | existing spec for the symmetric verdict | must pass unchanged |

---

## Task 1: Store the facts on the subquery engine's MetricPlan

Behaviour must not change in this task. This is the "make the inputs visible" half.

**Files:**
- Modify: `src/grain/engine/grain.py` (`MetricPlan` at :24-41, `analyse` at :247-441)

**Interfaces:**
- Produces: `SeparatedFan` (frozen dataclass, fields `link_name: str`, `cardinality: str`, `to_object_name: str`, `pin_name: str`, `grain: str`); `MetricPlan.overlap_link: OverlapFact | None`, `MetricPlan.separated_fan: SeparatedFan | None`, `MetricPlan.grouped: bool`; `OverlapFact` (frozen dataclass, fields `link_name: str`, `cardinality: str`, `identifying_keys: str`, `subject: str`).

- [ ] **Step 1: Add the two fact records above `MetricPlan`**

```python
@dataclass(frozen=True)
class OverlapFact:
    """A many_to_many edge on the metric's own prefix, plus the keys a refusal
    has ALREADY validated as identifying. Stored rather than recomputed: the
    validation is a decision (`_require_identifying_keys` raises), and the
    derivation that reads this must never raise."""
    link_name: str
    cardinality: str
    identifying_keys: str
    subject: str


@dataclass(frozen=True)
class SeparatedFan:
    """A fanning edge downstream of the grain whose target a unique group key
    pins, so its copies scatter into distinct groups instead of piling into
    one. Correct per group; the total counts one row in every group it joins."""
    link_name: str
    cardinality: str
    to_object_name: str
    pin_name: str
    grain: str
```

- [ ] **Step 2: Add the three fields to `MetricPlan`, keeping `additive` and `non_additive_reason` exactly as they are**

Append inside `class MetricPlan`, after `window`:

```python
    # The facts the additivity verdict is derived from. Stored so the verdict
    # can be a function of the finished plan rather than a variable accumulated
    # beside it — see docs/plans/2026-09-06-derived-additivity-design.md. Three
    # Criticals have been caused by a decision and its claim disagreeing.
    overlap_link: OverlapFact | None = None
    separated_fan: SeparatedFan | None = None
    grouped: bool = False
```

- [ ] **Step 3: Populate them in `analyse`, without removing the existing accumulation**

In the `for edge in prefix:` loop at :339, inside the `many_to_many` branch, immediately after `subject = identifying[0].object.name`, add:

```python
                overlap_fact = OverlapFact(
                    link_name=edge.link.name,
                    cardinality=edge.link.effective_cardinality,
                    identifying_keys=keys,
                    subject=subject,
                )
```

Initialise `overlap_fact: OverlapFact | None = None` and `separated_fact: SeparatedFan | None = None` immediately before that loop, beside `additive = True`.

In the `if additive and separated:` branch at :364, after `pin = _pinned_by_a_unique_key(rq, index)`, add:

```python
            separated_fact = SeparatedFan(
                link_name=edge.link.name,
                cardinality=edge.link.effective_cardinality,
                to_object_name=edge.to_object.name,
                pin_name=pin.name,
                grain=metric.grain,
            )
```

Then pass all three into the `MetricPlan(...)` call at :431:

```python
                overlap_link=overlap_fact,
                separated_fan=separated_fact,
                grouped=bool(rq.group_by),
```

- [ ] **Step 4: Run the whole suite — nothing may change**

```bash
export GRAIN_DATABASE_URL="postgresql+psycopg://solshortland@localhost:5432/chinook"
uv run pytest -q
```
Expected: `570 passed, 0 skipped`. Any failure means a fact was stored wrongly; the verdict logic has not been touched yet.

- [ ] **Step 5: Add a test that the facts match the verdict they will replace**

Create `tests/unit/test_additivity_derivation.py`:

```python
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
    no fact behind it is the shape that produced the stock defect."""
    mp = _plan(
        chinook_ontology,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["playlist_id"],
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
```

- [ ] **Step 6: Run it**

```bash
uv run pytest tests/unit/test_additivity_derivation.py -v
```
Expected: 2 passed. If `test_a_non_additive_verdict_always_has_a_fact_behind_it` fails, the group key or traversal above does not produce the overlap this repo documents — read the error and use `tests/unit/test_grain_additivity.py` for a shape that does, rather than weakening the assertion.

- [ ] **Step 7: Commit**

```bash
git add src/grain/engine/grain.py tests/unit/test_additivity_derivation.py
git commit -m "refactor: a plan records the facts its additivity verdict rests on"
```

---

## Task 2: Derive the subquery engine's verdict, and census its fields

**Files:**
- Modify: `src/grain/engine/grain.py`
- Modify: `tests/unit/test_additivity_derivation.py`

**Interfaces:**
- Consumes: `OverlapFact`, `SeparatedFan`, `MetricPlan.overlap_link`, `.separated_fan`, `.grouped`, `.window` from Task 1.
- Produces: `_additivity(plan: MetricPlan) -> tuple[bool, str | None]`; module constants `_ADDITIVITY_INPUTS: frozenset[str]` and `IRRELEVANT_TO_ADDITIVITY: dict[str, str]`; `MetricPlan.additive` and `.non_additive_reason` as properties.

- [ ] **Step 1: Write the census test first — it is the deliverable**

Append to `tests/unit/test_additivity_derivation.py`:

```python
from dataclasses import fields

from grain.engine.grain import (
    IRRELEVANT_TO_ADDITIVITY,
    MetricPlan,
    _ADDITIVITY_INPUTS,
)


def test_every_metric_plan_field_is_classified():
    """A new field on MetricPlan must be declared either an input to the
    additivity derivation or explicitly irrelevant, with a reason.

    This is the point of the exercise. `window` was added beside `additive`
    without either knowing about the other, and the result was a level
    reported as summable — 1153, the exact figure a test pins as wrong.
    Forgetting is now a red test naming the field, not a reading someone has
    to do."""
    names = {f.name for f in fields(MetricPlan)}
    classified = _ADDITIVITY_INPUTS | set(IRRELEVANT_TO_ADDITIVITY)
    unclassified = names - classified
    assert not unclassified, (
        f"MetricPlan fields not classified for additivity: {sorted(unclassified)}. "
        f"Add each to _ADDITIVITY_INPUTS, or to IRRELEVANT_TO_ADDITIVITY with a "
        f"one-line reason it cannot affect whether the column sums to the total."
    )
    assert not (_ADDITIVITY_INPUTS & set(IRRELEVANT_TO_ADDITIVITY)), (
        "a field cannot be both an input and irrelevant"
    )
    stale = classified - names
    assert not stale, f"classified fields that no longer exist: {sorted(stale)}"


def test_every_irrelevance_carries_a_reason():
    """The reason string IS the deliverable — it records the judgement that was
    missing when `window` was added. An empty or one-word entry passes the
    census while skipping the thinking it exists to force."""
    for name, reason in IRRELEVANT_TO_ADDITIVITY.items():
        assert len(reason.split()) >= 5, f"{name}: reason too thin to be a judgement"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/unit/test_additivity_derivation.py -v
```
Expected: FAIL with `ImportError: cannot import name 'IRRELEVANT_TO_ADDITIVITY'`.

- [ ] **Step 3: Add the derivation and the classification, above `class GrainPlan`**

```python
# The fields the additivity verdict is derived from. Adding a field that can
# affect whether a column sums to the total means adding it here AND handling
# it in `_additivity`; the census test refuses an unclassified field.
_ADDITIVITY_INPUTS = frozenset({"overlap_link", "separated_fan", "grouped", "window"})

# Fields that provably cannot change the verdict, each with the reason. The
# reason is the record of the judgement — see the census test.
IRRELEVANT_TO_ADDITIVITY: dict[str, str] = {
    "metric": "identifies which metric this is; its own quantity reaches the verdict through window",
    "strategy": "chooses the SQL shape, and both shapes are correct per group; overlap is a property of the path",
    "forced_by": "names the link that forced a rewrite, which is a strategy fact and not a summability one",
    "subquery_edges": "how far a pre-aggregating subquery walks, which changes emitted SQL and not whether groups overlap",
    "additive": "the verdict itself",
    "non_additive_reason": "the verdict itself",
}


def _additivity(plan: "MetricPlan") -> tuple[bool, str | None]:
    """The verdict, derived from the finished plan.

    NEVER RAISES. Refusing is a decision and belongs in `analyse`; a derivation
    that can raise is a second decision point wearing a property's clothes.
    `plan.overlap_link` already carries the identifying keys that
    `_require_identifying_keys` validated when it chose not to refuse.

    Conditions are independent and both reasons are kept when both hold:
    dropping half of why a total is meaningless is not an improvement on
    saying both."""
    reasons: list[str] = []

    if plan.overlap_link is not None:
        o = plan.overlap_link
        reasons.append(
            f"'{plan.metric.name}' is grouped across '{o.link_name}', which is "
            f"{o.cardinality}. Each group is correct — "
            f"'{o.identifying_keys}' identifies one {o.subject} — but the groups "
            f"overlap, so this column will not sum to the total."
        )
    elif plan.separated_fan is not None:
        s = plan.separated_fan
        reasons.append(
            f"'{plan.metric.name}' is counted once per {s.to_object_name} reached "
            f"through '{s.link_name}', which is "
            f"{s.cardinality}. Each group is correct — "
            f"'{s.pin_name}' identifies one {s.to_object_name} — but one "
            f"'{s.grain}' row belongs to several groups, so this column "
            f"will not sum to the total."
        )

    # A level is whole only at one instant, and the window partitions by the
    # query's OWN group keys, so each group collapses to its own instant.
    # Ungrouped stays additive: one global instant, one figure, nothing to add
    # it to.
    if plan.window is not None and plan.grouped:
        reasons.append(
            f"'{plan.metric.name}' is a level at an instant, windowed to each "
            f"group's own boundary. Each group is correct — but adding them "
            f"sums across instants and gives a level at no instant, and "
            f"grouping by the time dimension itself makes the total the "
            f"across-time sum a level is defined not to have."
        )

    if not reasons:
        return True, None
    return False, " ".join(reasons)
```

- [ ] **Step 4: Turn the two verdict fields into properties**

Delete `additive: bool = True` and `non_additive_reason: str | None = None` from `MetricPlan`, delete them from the `IRRELEVANT_TO_ADDITIVITY` entries added in Step 3 (they are no longer fields, and the census's staleness assertion will catch them), and add inside `class MetricPlan`:

```python
    @property
    def additive(self) -> bool:
        return _additivity(self)[0]

    @property
    def non_additive_reason(self) -> str | None:
        return _additivity(self)[1]
```

- [ ] **Step 5: Delete the accumulation from `analyse`**

Remove `additive = True`, `non_additive_reason: str | None = None`, every `additive = False`, every `non_additive_reason = ...` assignment, the `level_reason` block, and the `additive=` / `non_additive_reason=` arguments to `MetricPlan(...)`. **Keep** the `_require_identifying_keys` call and the `break`, and keep `if additive and separated:` as `if overlap_fact is None and separated:` — the `elif` in `_additivity` preserves the original precedence.

- [ ] **Step 6: Run the whole suite. Existing tests may not be edited**

```bash
uv run pytest -q && uv run ruff check src tests tools
```
Expected: `570 passed` plus the 4 new tests = **574 passed, 0 skipped**. If an existing additivity test fails, the derivation is wrong — fix `_additivity`, not the test.

- [ ] **Step 7: Prove the census works by breaking it**

```bash
uv run python - <<'PY'
import re, pathlib
p = pathlib.Path("src/grain/engine/grain.py"); t = p.read_text()
t2 = t.replace("    grouped: bool = False", "    grouped: bool = False\n    dummy_field: int = 0", 1)
p.write_text(t2)
PY
uv run pytest tests/unit/test_additivity_derivation.py::test_every_metric_plan_field_is_classified -q
git checkout -- src/grain/engine/grain.py
```
Expected: FAIL naming `dummy_field`. Then the `git checkout` restores the file — confirm with `git status --porcelain src/grain/engine/grain.py` printing nothing but your intended change... if it prints nothing at all you have discarded Step 3-5's work, so **commit before running this step**.

- [ ] **Step 8: Commit**

```bash
git add src/grain/engine/grain.py tests/unit/test_additivity_derivation.py
git commit -m "feat: the subquery engine derives its additivity verdict from the plan"
```

---

## Task 3: The same derivation in the symmetric engine

The two engines share only the loaded ontology. This is a deliberate copy, like `engine_symmetric/resolve.py` — do not factor it out.

**Files:**
- Modify: `src/grain/engine_symmetric/grain.py` (`MetricPlan` at :43-49, the loop at :174-215)

**Interfaces:**
- Produces: `grain.engine_symmetric.grain._additivity`, `_ADDITIVITY_INPUTS`, `IRRELEVANT_TO_ADDITIVITY`, and `OverlapFact` / `GroupKeyOverlap` fact records for this engine's own two conditions.

- [ ] **Step 1: Note what differs, before writing code**

This engine has TWO conditions, not three, and its second is NOT the subquery engine's `separated`:
1. a `many_to_many` edge on `prefix` — same shape as the subquery engine's condition 1, but its reason says *"the encoding counts every row once per group"* rather than naming identifying keys, and it calls no validator, so there is no refusal to keep out of the derivation.
2. `_overlap(rq)` at :99 — a group key reached through a fanning link. **This engine already follows the path to the GROUP KEY**, which is exactly what `engine/grain.py:305` says the subquery engine cannot do. Keep that difference; it is a capability, not drift.

There is no window condition here: this engine refuses a stock outright (`MetricNotSymmetric`). `_additivity` must SAY SO in a comment rather than silently omit it — a missing condition looks identical to an unconsidered one, which is the whole failure this plan addresses.

- [ ] **Step 2: Add the two fact records above `MetricPlan`**

```python
@dataclass(frozen=True)
class PrefixOverlap:
    """A many_to_many edge on the metric's own prefix."""
    link_name: str
    cardinality: str
    grain: str


@dataclass(frozen=True)
class GroupKeyOverlap:
    """A group key reached through a fanning link. Read from `_overlap`, which
    follows the path to the KEY rather than to the grain — a capability the
    subquery engine does not have."""
    key_name: str
    link_name: str
    cardinality: str
    grain: str
```

- [ ] **Step 3: Store the facts, add the derivation, make the verdict properties**

Add to `MetricPlan`:

```python
    prefix_overlap: PrefixOverlap | None = None
    group_key_overlap: GroupKeyOverlap | None = None

    @property
    def additive(self) -> bool:
        return _additivity(self)[0]

    @property
    def non_additive_reason(self) -> str | None:
        return _additivity(self)[1]
```

and above `class GrainPlan`:

```python
_ADDITIVITY_INPUTS = frozenset({"prefix_overlap", "group_key_overlap"})

IRRELEVANT_TO_ADDITIVITY: dict[str, str] = {
    "metric": "identifies which metric this is; this engine refuses a stock, so no quantity fact reaches the verdict",
    "strategy": "inline versus symmetric changes the encoding, and both count every grain row once per group",
    "forced_by": "names the link that forced the encoding, which is a strategy fact and not a summability one",
}


def _additivity(plan: "MetricPlan") -> tuple[bool, str | None]:
    """This engine's verdict, derived from the finished plan. Never raises.

    TWO conditions, not the subquery engine's three. There is deliberately no
    window condition: this engine refuses a stock with `MetricNotSymmetric`,
    so a level never reaches a plan here. Stated rather than omitted — a
    missing condition looks identical to an unconsidered one, and that
    resemblance is what this derivation exists to remove."""
    if plan.prefix_overlap is not None:
        o = plan.prefix_overlap
        return False, (
            f"'{plan.metric.name}' is grouped across '{o.link_name}', which "
            f"is {o.cardinality}. Each group is correct "
            f"— the encoding counts every '{o.grain}' row once per "
            f"group — but one row belongs to several groups, so this "
            f"column will not sum to the total."
        )
    if plan.group_key_overlap is not None:
        g = plan.group_key_overlap
        return False, (
            f"'{plan.metric.name}' is grouped by '{g.key_name}', which is reached "
            f"through '{g.link_name}' ({g.cardinality}). "
            f"Each group is correct, but one '{g.grain}' row belongs to "
            f"every group it can reach, so this column will not sum to the "
            f"total."
        )
    return True, None
```

Populate `prefix_overlap` in the `many_to_many` branch and `group_key_overlap` from `overlap` (guarded so the prefix condition still wins, matching the original `if additive and overlap is not None`), then delete the accumulation and the two constructor arguments.

- [ ] **Step 4: Run the suite**

```bash
uv run pytest -q && uv run ruff check src tests tools
```
Expected: **574 passed, 0 skipped**. Existing symmetric tests may not be edited.

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine_symmetric/grain.py
git commit -m "feat: the symmetric engine derives its own additivity verdict"
```

---

## Task 4: Census the symmetric engine, assert parity, and pin the accident

**Files:**
- Modify: `tests/unit/test_additivity_derivation.py`

- [ ] **Step 1: Census the symmetric engine's MetricPlan**

```python
from dataclasses import fields as dc_fields

from grain.engine_symmetric.grain import (
    IRRELEVANT_TO_ADDITIVITY as SYM_IRRELEVANT,
    MetricPlan as SymMetricPlan,
    _ADDITIVITY_INPUTS as SYM_INPUTS,
)


def test_every_symmetric_metric_plan_field_is_classified():
    names = {f.name for f in dc_fields(SymMetricPlan)}
    classified = SYM_INPUTS | set(SYM_IRRELEVANT)
    unclassified = names - classified
    assert not unclassified, (
        f"symmetric MetricPlan fields not classified: {sorted(unclassified)}"
    )
    assert not (SYM_INPUTS & set(SYM_IRRELEVANT))
    assert not classified - names, f"stale: {sorted(classified - names)}"
```

- [ ] **Step 2: Assert the two derivations agree where both engines answer**

In the spirit of `tests/unit/test_resolver_parity.py`, which exists because the duplication is deliberate and drift must be visible:

```python
def test_both_engines_agree_on_additivity_where_both_answer(chinook_ontology):
    """The derivations are deliberately duplicated, so drift is possible by
    design. Where both engines can answer, the VERDICT must match — the reason
    strings differ legitimately (the symmetric engine says the encoding counts
    each row once; the subquery engine names identifying keys)."""
    from grain.engine_symmetric.grain import analyse as sym_analyse
    from grain.engine_symmetric.resolve import resolve as sym_resolve

    shapes = [
        dict(object="InvoiceLine", metrics=["revenue"]),
        dict(object="Invoice", traverse=[Hop(link="Invoice_Lines")], metrics=["revenue"],
             group_by=["invoice_id"]),
        dict(object="Playlist", traverse=[Hop(link="Playlist_Tracks"),
             Hop(link="Track_InvoiceLines")], metrics=["revenue"], group_by=["playlist_id"]),
    ]
    for kw in shapes:
        sub = analyse(resolve(QuerySpec(**kw), chinook_ontology)).additive
        sym = sym_analyse(sym_resolve(QuerySpec(**kw), chinook_ontology)).additive
        assert sub == sym, f"engines disagree on additivity for {kw}"
```

- [ ] **Step 3: The accidental guard is ALREADY pinned — cross-reference it, do not duplicate it**

`tests/unit/test_immune_aggregates.py:62`,
`test_immunity_does_not_lift_the_key_beyond_grain_refusal`, already pins this and
already explains it:

> *"that refusal is also the only thing preventing a wrong ADDITIVE verdict:
> `distinct_employees` grouped by an ancestor's surname puts one employee in every
> ancestor group above them, and the additivity loop iterates the path to the
> GRAIN, which is empty for a root-measured metric."*

**Write no new test here.** Instead do two things:

1. Read that test and confirm it still passes and still raises `KeyBeyondGrain`
   after Tasks 1-3. If the restructuring broke it, the derivation is wrong.
2. Add a pointer to it from `_ADDITIVITY_INPUTS` in `src/grain/engine/grain.py`,
   so the next reader of the derivation learns that its dependence on `prefix`
   has a known hole and where the guard lives:

```python
# `overlap_link` is derived from the metric's PREFIX, which is EMPTY for a
# metric measured at the root — so a root-grain metric grouped by an ancestor
# key has overlapping groups this derivation cannot see. Nothing here catches
# it; `KeyBeyondGrain` does, by accident, and
# tests/unit/test_immune_aggregates.py::test_immunity_does_not_lift_the_key_beyond_grain_refusal
# is what stops that accident being removed. Fixing it properly means following
# the path to the GROUP KEY, as engine_symmetric/grain.py::_overlap does.
# Deliberately out of scope here: it is a semantic change, and mixing it into a
# restructuring would make a behavioural difference impossible to attribute.
```

That comment is the deliverable of this step. The hole was previously recorded
only in a comment beside the refusal; it is now also recorded beside the
derivation that has it, which is where someone extending the derivation will
look.

- [ ] **Step 4: Run everything, including the sweep and the anchors**

```bash
export GRAIN_DATABASE_URL="postgresql+psycopg://solshortland@localhost:5432/chinook"
uv run pytest -q && uv run ruff check src tests tools && uv run python tools/sweep.py | tail -5
```
Expected: **576 passed, 0 skipped** (570 baseline + 6 new; no new pin in Step 3); ruff clean; sweep 74-11-3-2 with 0 WRONG.

- [ ] **Step 5: Mutate each derivation branch and confirm a test fails**

For each of the five branches (subquery: overlap, separated, window; symmetric: prefix, group-key), invert or delete the condition, run `uv run pytest -q`, record which test failed, and restore. A branch no test defends is not covered — report any that survive.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_additivity_derivation.py
git commit -m "test: census both engines' plan fields and pin the accidental guard"
```

---

## Self-Review

**Spec coverage.** §2 (the rule) → Tasks 1-3. §3 (facts, derivation, properties) → Tasks 1-3. §3a (the hidden refusal stays in `analyse`) → Task 2 Step 5, explicitly. §4 (field census) → Task 2 Steps 1-3, Task 4 Step 1, with the reason-quality guard in Task 2 Step 1. §5 (existing tests unchanged) → Global Constraints and Task 2 Step 6. §6 (pin the accident, do not fix it) → Task 4 Step 3, which found the pin ALREADY EXISTS at tests/unit/test_immune_aggregates.py:62 and so cross-references it from the derivation instead of duplicating it. §7 (mutation, agent-facing surface, census self-proof, parity) → Task 4 Steps 2, 5 and Task 2 Step 7. §8 risk 2 (thin reasons) → `test_every_irrelevance_carries_a_reason`.

**Gap found and closed:** §7 asks for the three conditions to be asserted through `agent/tools.py`'s rendered output, not only the flag — the defect was that the flag reached the agent as advice. Task 4 needs that; it is added as Step 2a below rather than left implicit.

- [ ] **Task 4, Step 2a: Assert the agent sees the caveat, not just the flag**

```python
def test_the_agent_is_told_not_to_add_a_non_additive_column(chinook_ontology):
    """The flag's only job is to reach the caller. `agent/tools.py` emits its
    caveat when additive is False; a correct flag that never renders is the
    same defect one layer along."""
    from grain.agent.tools import _caveats

    mp = _plan(
        chinook_ontology,
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")],
        metrics=["revenue"],
        group_by=["playlist_id"],
    )
    assert mp.additive is False
    assert mp.non_additive_reason is not None
```

Then assert the caveat renders. `_caveats(result) -> list[str]` in
`src/grain/agent/tools.py:102` is the function that turns the flag into the text
the model reads, and `run(grain, raw_input) -> tuple[str, bool]` at :127 is the
whole path. Use `tests/unit/test_agent_tools.py` for how it is already exercised,
and assert that a non-additive plan yields a caveat mentioning not adding the
column — a correct flag that never renders is the same defect one layer along.

**Placeholder scan:** no TBDs. Two steps (Task 4 Step 3, Step 2a) name a fallback if the exact query shape or function name differs, with the authoritative file to read — deliberate, because those two names were not verified against a live run while writing this plan.

**Type consistency:** `OverlapFact` / `SeparatedFan` (subquery) and `PrefixOverlap` / `GroupKeyOverlap` (symmetric) are distinct per engine by design; `_additivity` has the same signature in both. `_ADDITIVITY_INPUTS` is a `frozenset[str]`, `IRRELEVANT_TO_ADDITIVITY` a `dict[str, str]`, in both.
