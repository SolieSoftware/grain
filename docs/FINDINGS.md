# Findings

What building this actually taught, as opposed to what the design documents
claim. Written for the next person modelling a different dataset, who will hit
the same shapes under different names.

Each entry is: **what was believed**, **what turned out to be true**, and **what
it cost**. Entries are added while the work is happening — a difficulty is
easiest to describe honestly while it is still costing you something, and by the
time it is fixed the reasoning that made it hard has usually evaporated.

---

## Verification

### A green test run proves the command you ran, not the command you wrote down

`python -m pytest` puts the working directory on `sys.path`; `uv run pytest`
does not. A test module importing `tests.corpus` therefore collected fine for
the person who wrote it and raised `ModuleNotFoundError` for everyone following
the README. It shipped that way for two commits.

**Transferable:** run the documented command verbatim before documenting it. A
convenient variant is a different command.

### Two implementations agreeing proves only that they agree

The differential harness between grain's two engines can find disagreements. It
cannot find a mistake both share, and both were written by the same author from
the same misconception, so the shared-mistake case is the likely one.

What fixed this was `tools/oracle.py`: the answer computed in pure Python from
raw rows, sharing no SQL with either engine. It is the only check that cannot
inherit a misconception from the code it checks, and it is worth more than
either engine.

**Transferable:** a third answer derived by a genuinely different route turns
"they agree" into "they are right". Two is not enough.

### Skipped tests are green

With no database URL set, the suite reports `270 passed, 121 skipped` — and
every test that asserts a *measured* value against real data is in the skipped
121. The colour is green and nothing was verified.

**Transferable:** check the skip count, not the colour. If the suite can be
green having tested nothing, it will eventually be green having tested nothing.

### Hand-written comparison SQL carries the bug it is checking for

While writing an explanation *of fan-out bugs*, the comparison query used to
check grain's output had a fan-out bug: `album → track → invoice_line` fans the
track rows, so `sum(track.unit_price)` came back inflated by exactly one
duplicated track's price. The engines returned the right number; the check
didn't.

**Transferable:** if the system exists to prevent a class of error, your
verification code is written by someone who makes that error. Prefer an oracle
in a different language over SQL you wrote by hand.

---

### A second implementation earns its cost the day it disagrees

The differential harness plus the oracle found a real defect in the **default**
engine — one that had been there since the fan-out rewrite was written, and that
no amount of testing that engine against itself would have surfaced.

`aggregate_then_join` pre-aggregates a metric at its own grain, walking only far
enough to reach it. That is deliberate: applying the downstream FANNING edges
would replicate the grain's rows inside the very subquery built to prevent that.
But not walking an edge also means **not filtering by it**. A traversal that
restricts the population — `Track -> Track_InvoiceLines`, meaning tracks that
actually sold — is invisible to the pre-aggregate, which computes over every
track sharing the group key.

It stayed hidden because every summing metric in the domain sits at a grain
nothing downstream eliminates. An order statistic is sensitive to precisely
which rows are in the set, so it surfaced on the first enumerated sweep: 54 of
1888 groups differ, one of them returning 200620 where the answer is 356284.

**Transferable:** the argument for a second implementation is usually stated as
redundancy, and that undersells it. Its value is that it disagrees, and a
disagreement you cannot dismiss is the only cheap way to find a bug that has
been correct-looking for months. Build the second one differently enough that it
*can* disagree.

## Modelling

### Validating the grain of a metric says nothing about whether the quantity is additive

grain enforced that a metric's rows were not replicated — a real and hard
guarantee — and had no concept of whether summing was a sensible operation at
all. `sum(track.unit_price)` was arithmetically perfect, matched hand-written
SQL exactly, reported `additive: true` with no caveat, and answered no question.

The fix was not an aggregate technique. It was a declaration: a property says
`quantity: extensive | rate | ratio`, and the loader refuses a `sum` over
something that does not accumulate. No amount of clever SQL can tell you a price
should not be totalled.

**Transferable:** most semantic layers model *how* to aggregate and leave *what
may be aggregated* to the modeller's judgement. That gap is where the plausible
wrong numbers live.

### The narrow rule was the only correct rule

The obvious version — "any column a `sum` touches must be extensive" — refuses
`revenue`, because `revenue` is `sum(unit_price * quantity)` and `unit_price` is
a rate. A rate times a count genuinely is extensive.

So the check inspects a summed value **only when it is a bare column reference**.
`sum(a * b)` is left alone: writing the product is the author doing the
composition work, and inferring it properly would need an expression evaluator.

**Transferable:** a rule that fires on the flagship metric is wrong however
sound its principle. Test new validation against the model's most important
metric before its worst one.

### Defaulting to permissive is the assumption you were trying to remove

When `quantity` was added, the tempting default was "undeclared means summable" —
fully backwards compatible, nothing breaks. But that is exactly the silent
assumption the field exists to eliminate.

Refusing undeclared columns turned out to be cheap because the rule only reaches
columns a `sum` actually reads: **three columns in the entire chinook pack**.

**Transferable:** count the blast radius before assuming a strict default is too
disruptive. A rule scoped to where it matters usually touches very little.

### Eligibility belongs to the database, not the declaration

`ValueType` has no float member, so a `double precision` column would be
declared `decimal` by any ontology author. A check reading the declared type
would admit an inexact column into an encoding that depends on exactness.

**Transferable:** where a declaration and the schema can disagree, and the
consequence is a wrong number, read the schema. grain already did this for
cardinality, uniqueness and nullability; the same rule applies to precision.

---

## Encodings

### A bound from a type is worth far more than a bound from data

Two constants that look alike and are not:

| | bounds | enforced by |
|---|---|---|
| symmetric sum, `\|v\| < 5e29` | a **value** | a load-time check — later writes can violate it silently |
| order statistics, `K = 1e19` | a **key** | a **proof** — a bigint cannot exceed 9.22e18 |

The first is recorded as grain's weakest point. The second cannot drift at all,
because the key's own type caps it.

**Transferable:** when choosing what to encode against, prefer the quantity the
schema already bounds. A bound you can prove needs no monitoring.

### A cited limitation can be narrower than its wording

The literature says a median *"has no equivalent distinct-sum rewrite, so the
math simply does not hold"*. True — and it rules out **that** rewrite, not every
encoding. Packing the value into the high digits and the key into the low ones
gives an orderable scalar that `DISTINCT` deduplicates correctly, which an array
index then reads back.

Taking the sentence at its broadest reading would have closed the question and
left a real capability unbuilt.

**Transferable:** check what a stated impossibility is actually quantified over.
"No X-shaped solution exists" is a much smaller claim than "no solution exists".

### The objection to decimals dissolved on inspection

Scaling values to clear their fraction was initially rejected as reintroducing
Looker's lossiness. But Looker `FLOOR`-scales to a **guessed** precision — 6 by
default, and its own documentation advises dropping to 5 for large values, which
loses more. The scale can instead be **read from reflection**: `NUMERIC(10,2)`
gives `scale=2`, and multiplying by 10² clears the fraction with nothing to
round.

A guessed scale must truncate. A derived scale cannot. The initial
integer-only restriction was unnecessary and would have been a real limit on
what the engine could answer.

**Transferable:** when rejecting a technique because a known implementation of
it is flawed, check whether the flaw is in the technique or in that
implementation's inputs.

---

## Process

### Fixing a pinned limitation means deleting a test

`tests/integration/test_shared_limits.py` proves what the system *cannot* do. The
standard adopted here is that closing one of those requires **deleting** its
test, not asserting past it. That forces the claim to be real: a test that was
inverted rather than removed is a limitation that moved rather than closed.

### The first real API call is the real test

The chat agent had 30 passing tests, a schema generated from the same Pydantic
model the engine validates against, and had never made a network call. The first
one failed. So did the second, differently.

Both failures were in the same class — the tool schema satisfied JSON Schema but
not the API's *strict mode* subset — and no test caught them because every test
asserted the schema **was** the QuerySpec schema, which it was, and none asserted
it was **acceptable**. Two different claims; only one was being made.

**Transferable:** a test that checks provenance is not checking validity. If a
component's contract is enforced by a system you cannot run locally, the tests
that matter are the ones that assert what that system requires.

### The elegant invariant was not the load-bearing one

"The tool schema *is* `QuerySpec.model_json_schema()`, verbatim, so the contract
cannot drift" was stated in three places and was the design's stated selling
point. It had to be abandoned — strict mode rejects keywords Pydantic emits.

Nothing was actually lost. The property that mattered was never the identity: it
was that *one generated object defines both* and that Pydantic remains the
enforcement boundary. Both survived. The verbatim-ness was decoration.

**Transferable:** when an elegant invariant breaks, ask which part of it was
doing the work. It is often not the part that made it elegant.
