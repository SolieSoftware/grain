# Derive a Plan's Claims From the Plan — Design

**Status:** design 2026-09-06, not yet implemented.

**Goal:** Make a `MetricPlan`'s claims about its result — `additive`,
`non_additive_reason` — a function of the finished plan, so that a decision and
the claim describing it cannot part company.

**Backlog:** item 1 in `docs/BACKLOG.md`.

---

## 1. The defect this generalises

A windowed stock reported `additive: true`. Every per-group figure was correct;
the totals were not. Grouped by `as_of` the column summed to **1153** — exactly
the across-time figure `test_the_naive_sum_differs_from_the_level` pins as the
wrong answer — and grain labelled it summable. `agent/tools.py` emits its "do NOT
add them together" caveat only when `additive` is `False`, and `agent/prompt.py`
instructs the model to total otherwise, so the sanctioned path ended in
*"total inventory: 1153"*.

The wrong number was not arithmetic. **The claim on the column was false, and the
wrong number arrived one honest `sum()` later.**

That case is fixed. The structure that produced it is not: `analyse()` computes
the window and the additivity verdict in the same loop and independently of each
other, and nothing links them.

**Neither standing safety net can see this class.** The differential harness needs
two engines to disagree, and a claim made in the plan layer is not SQL — worse,
the symmetric engine refuses a stock outright, so there is no second answer at
all. `tools/oracle.py` answers the same per-group question and agrees per group;
it has no opinion about a total nobody computed.

This is the third Critical of this shape. The first two — a fanned 5738.28
returned as one unlabelled figure, and a count of 6 for a truth of 3 reported
additive — were also found by review, not by a test.

## 2. The rule

**A plan records what it decided. It must not also record what it believes about
the result.**

`strategy`, `window`, `subquery_edges` and `forced_by` are decisions: each names
something the compiler will do. `additive` and `non_additive_reason` are claims
*about the result of those decisions*. Today they sit in the same dataclass,
written by different branches of one loop, and a reader must hold both halves in
mind to notice a contradiction. That is the reading the last three Criticals
depended on someone doing.

## 3. What changes

**`MetricPlan` gains the facts the verdict depends on** and loses the verdict as
a constructor argument.

The three conditions currently accumulated in `engine/grain.py` are:

| # | Condition | Fact needed |
|---|---|---|
| 1 | a `many_to_many` edge anywhere on `prefix` — the same grain row belongs to more than one group | the prefix edges |
| 2 | `separated` — inline was correct only because a unique key scattered replicated rows into distinct groups, so every group is right and the total is meaningless | the `separated` finding |
| 3 | a window with group keys — each group collapses to its own boundary instant, so the column is a set of levels at different instants | `window` (already carried) and whether `rq.group_by` is non-empty |

Conditions 1 and 2 are computed from data `analyse()` currently has in scope and
throws away. They become fields. Condition 3 needs only a boolean.

**`_additivity(plan) -> tuple[bool, str | None]`** then reads the finished plan
and returns the verdict. `MetricPlan.additive` and `.non_additive_reason` become
properties delegating to it, so every existing caller — `GrainPlan.additive`,
`agent/tools.py`, `describe.py`, the CLI — is untouched.

### 3a. A refusal is hiding inside a reason string

Condition 1 does not only describe; it **decides**. Building its reason calls
`_require_identifying_keys(rq, metric, edge.link.name)`, which RAISES
`NonAdditiveRefused` when the group keys do not identify one row of the object
being grouped — because surfacing overlap as a flag is only defensible while the
per-group numbers are correct, and where they are not the engine itself would do
the wrong summing and no flag could rescue the caller.

So a refusal currently lives on the path that constructs a claim. Separating
decisions from claims forces it out, which is an argument for this restructuring
rather than a complication of it:

- `analyse()` keeps the call — refusing is a decision — and stores the
  identifying keys it returned as a fact on the plan.
- `_additivity(plan)` reads those keys to build the reason and **never raises**.

A derivation that can raise is not a derivation; it is a second decision point
wearing a property's clothes.

**Duplicated, not shared.** Each engine gets its own `_additivity`, exactly as
`engine_symmetric/resolve.py` is a deliberate copy of `engine/resolve.py`. A
shared derivation would put the rule in the layer the differential harness
already cannot see, which is precisely how the inferred-stock defect escaped.
The cost — two places to fix — is the one `CLAUDE.md` already argues is worth
paying, and `tests/unit/test_resolver_parity.py` is the precedent for making the
drift visible.

The symmetric engine carries two of the three conditions (it refuses stock, so
condition 3 cannot arise there). Its `_additivity` must state that refusal as the
reason condition 3 is absent, rather than silently omitting it.

## 4. The field census — the part that is not a refactor

A derivation alone does not stop the next field being forgotten; it moves where
the forgetting happens. The deliverable that actually closes the class is a test.

`test_every_metric_plan_field_is_classified` enumerates
`dataclasses.fields(MetricPlan)` and requires each name to appear in exactly one
of:

- the derivation's declared inputs, `_ADDITIVITY_INPUTS`
- an explicit `IRRELEVANT_TO_ADDITIVITY` mapping, each entry carrying a one-line
  reason why that field cannot affect summability

Adding a field and forgetting the verdict fails this test, naming the field. A
field that genuinely does not matter costs one line and a sentence — and that
sentence is the record of the judgement, which is what was missing when `window`
was added beside `additive` without either knowing about the other.

The census runs in both engines against their own `MetricPlan`.

## 5. What must not change

**Every existing additivity test passes unchanged.** They are the specification.
If the derivation requires editing one, the derivation is wrong — not the test.
This is the strongest available check that a restructuring preserved behaviour,
and it is the reason to restructure rather than rewrite.

Anchors: 2328.60, 5738.28, 255634, 256026. Sweep: 74-11-3-2, 0 WRONG.

## 6. Also pinned, not fixed

`engine/grain.py:305` documents that a correct verdict currently depends on an
unrelated refusal firing first:

> *"this refusal is also, and accidentally, the only thing preventing a WRONG
> ADDITIVE VERDICT ... the additivity loop below iterates `prefix`, which is
> EMPTY for a metric measured at the root, so it never sees the many_to_many and
> would report `additive: true`."*

A test pins the `KeyBeyondGrain` refusal so that deleting it goes red. **The
latent bug is not fixed here.** Fixing it means making overlap detection follow
the path to the GROUP KEY rather than to the metric's grain, which is a semantic
change and a prerequisite for lifting `KeyBeyondGrain` — a stated symmetric-engine
deliverable. Mixing it into a restructuring would make it impossible to tell
which change caused a behavioural difference.

The census will list the new prefix field as an input, so the derivation's
dependence on `prefix` becomes explicit — which is what makes the latent case
visible to the next reader rather than accidental.

## 7. Testing

- **Mutation, not reading.** Break each branch of each `_additivity` and confirm
  a named test fails. A branch no test defends is not covered.
- **The three conditions**, each asserted through `agent/tools.py`'s rendered
  output as well as the flag — the defect was that the flag reached the agent as
  advice, so the agent-facing text is the surface that matters.
- **The census**, proved by adding a dummy field in the test and asserting it
  fails, then removing it.
- **Parity**, in the spirit of `test_resolver_parity.py`: where both engines can
  answer, their verdicts and reasons agree.

## 8. Risks

1. **A restructuring that changes behaviour silently.** Mitigated by §5 — the
   existing tests are the specification and may not be edited.
2. **The census becomes a formality.** If `IRRELEVANT_TO_ADDITIVITY` accumulates
   entries with thin reasons, the test passes while the judgement it exists to
   force stops happening. The reason string is the deliverable, not the entry.
3. **Duplication drift**, accepted knowingly per §3 and made visible by the
   parity test.
