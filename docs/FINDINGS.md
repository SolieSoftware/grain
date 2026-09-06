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

### The harness cannot see a rule that is missing, and it went green on a wrong number

The entry above states the harness's weakness as *a mistake both engines share*,
and treats the oracle as the answer. That framing quietly assumes the shared
mistake would be a **misconception** — two implementations reasoning the same
wrong way about the same SQL — which is why the mitigation is to derive a third
answer by a different route.

There is a second version of the same weakness, structural rather than
psychological, and the oracle does not cover it. The two engines share the
**loaded ontology**. Every rule the loader enforces is therefore enforced once,
in code neither engine owns, so a rule that is *absent* is not something the two
could disagree about. It is invisible by construction.

grain walked into it. `Metric._check_over_time` keyed on `metric.quantity` — the
word the metric declares for itself. A metric that stayed silent and inherited
`stock` from its property had an *effective* quantity of `stock` and a
`metric.quantity` of `None`, so the check never fired. The ontology loaded
clean, nothing required `over_time`, and neither engine built a window. Both
then summed a level across every date, and both returned the same figure,
because each triggers its window on the same absent `metric.over_time`. On the
seeded table that shape is the difference between **111 and 1153**.

The harness was green. Not flaky, not skipped: green, with two independent
engines agreeing on a wrong number.

**What it cost.** Nothing shipped — and nothing in the verification stack found
it either. Not the harness, not the oracle, not the sweep, not the anchors. It
was found by a review lens outside all of them, by someone asking which field
the validator actually read. It is now refused at load and pinned by
`test_a_property_declared_stock_summed_by_a_silent_metric_is_refused`.

**Transferable:** a differential harness detects *asymmetry*. Find the line
where your two implementations stop being two — here, the loaded ontology — and
treat everything above it as checked once, by nothing. Those rules need a
different kind of test: not "do the two agree", but "does this validator read
the value it claims to". "Both engines agree" is a claim about the layers below
the split, and it is very easy to hear as a claim about the whole system.

### The third shipping of a test that could not fail, and the question that caught it

`assert "over (" in sql`, `assert "max(" in sql`, `assert "min(" in sql`. Three
substring assertions over compiled SQL, none of which executed it, so a window
partitioned by the wrong keys or filtered at the wrong boundary passed all
three. They were the **third** appearance of that pattern in the one file whose
own comments record it having shipped broken twice before.

Deleted rather than rewritten into slightly better string assertions; the same
ground is covered by executed queries in `test_stock_window_compile.py`, checked
against raw SQL. A test that cannot fail is worse than no test, because it
occupies the place where a missing one would be noticed.

The fix is not the interesting part. **What caught it is:** a review pass
instructed to take each test and ask *"would this FAIL if the code were wrong?"*
— and then mutate the code to find out, rather than reasoning about it. That
question needs no understanding of windowing, took one pass, and found what
three rounds of deliberate test-writing had not, in a file already carrying two
warnings about exactly this.

**Transferable:** a codebase can know a lesson, write it down in the file where
it applies, and re-learn it, because knowing a pattern is not the same as having
a step that detects it. Knowledge is not a control. Add the mutation question to
review, and answer it by editing the implementation and watching the test go
red.

### Skipped tests are green

With no database URL set, the suite reports `384 passed, 170 skipped` — and
every test that asserts a *measured* value against real data is in the skipped
170. The colour is green and nothing was verified.

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

*Fixed with an `EXISTS` semi-join — see "One decision, two effects" below for
what the bug itself turned out to be about.*

### One decision, two effects, and only one of them was reasoned about

The pre-aggregate skipped the downstream edges for a documented reason:
**walking them would replicate the grain's rows**. That reasoning is correct and
was carefully written down.

Skipping an edge does two things, though. It avoids replicating — the effect
that was reasoned about — and it avoids **filtering**, which was not mentioned
anywhere, in code or comments. The join is an inner join; not walking it silently
widens the population.

The fix keeps both effects apart: an `EXISTS` semi-join filters without
multiplying, so the property being protected survives untouched while the one
that was overlooked is restored.

**Transferable:** when a comment explains why something is skipped, check what
*else* skipping it does. A rationale that names one consequence tends to stop
the reader looking for a second, which is what let this survive for months in a
codebase dense with careful comments.

### A test that names a proxy will fail when the proxy stops holding

Two tests asserted "the downstream table does not appear in the subquery". The
property they meant was "is not JOINED, so nothing replicates". Once the table
started appearing inside an `EXISTS` — filtering, not multiplying — the proxy
broke while the guarantee held perfectly.

They were right to fail: a proxy failing is the moment you find out it was a
proxy. But the fix is to assert the real property, not to relax the test.

**Transferable:** when a test breaks on a change you believe is correct, work
out whether it was asserting the property or a stand-in for it. Relaxing a
genuine guarantee and tightening a broken proxy look identical in a diff.

### The first fix did nothing, silently

The `EXISTS` was built by reusing the departure table un-aliased, which put a
second `track` in the subquery's `FROM`. `correlate()` cannot remove what
`select_from` put there, so the clause compiled, ran, and asked *"does any sold
track exist"* — true for every row. It filtered nothing and raised nothing.

Caught only because the test compared numbers against hand-written SQL. A test
asserting "an EXISTS is present" would have passed.

**Transferable:** for anything correlated — subqueries, semi-joins, window
partitions — assert the *answer*, never the shape. An uncorrelated correlated
subquery is valid SQL with a constant result, and it looks right in a plan.

### The mitigation for a lost cross-check was named, cited, and did not exist

The symmetric engine refuses a `stock` by design — the window needs a subquery
and that engine is one pass — so the differential harness cannot check stock at
all. The design accepted that loss explicitly, on the stated ground that
`tools/oracle.py` would check it instead. A test docstring then said, in prose,
that *"the oracle is the only independent judge for it"*.

The oracle had no stock code. Its metric support stopped at `revenue` and
`units_sold`. For several commits the branch had **neither** independent check
on stock windowing, while carrying a sentence asserting it had one — and the
sentence was inside a passing test.

**Transferable:** when a design trades away a verification, the replacement is
part of the same change, not a later task. A named mitigation is easy to cite
and easy not to build, and prose describing a test is not a test.

### A dedup key narrower than the real one deletes the thing under test

The oracle dedupes grain rows on one primary-key column. `daily_inventory` is
keyed `(track_id, as_of_date)`; registering `track_id` alone would have kept one
row per track, collapsed every date, and produced an oracle that agreed with a
correct engine for entirely the wrong reason — and would have gone on agreeing
with an engine that had stopped windowing.

**Transferable:** a dedup key narrower than the real one does not fail, it
silently answers a smaller question. Check the arity of the key whenever an
oracle meets a table it has not seen before.

## Modelling

### Validating the grain of a metric says nothing about whether the quantity is additive

grain enforced that a metric's rows were not replicated — a real and hard
guarantee — and had no concept of whether summing was a sensible operation at
all. `sum(track.unit_price)` was arithmetically perfect, matched hand-written
SQL exactly, reported `additive: true` with no caveat, and answered no question.

The fix was not an aggregate technique. It was a declaration: `quantity: flow |
stock | value_per_unit`, and the loader refuses a `sum` over something that does
not accumulate. No amount of clever SQL can tell you a price should not be
totalled.

*(The field first read `extensive | rate | ratio` and lived on the property. Both
were later corrected — see "The problem was well-factored in 1997" below for the
vocabulary, and "Where a quantity lives" for why it moved to the metric.)*

**Transferable:** most semantic layers model *how* to aggregate and leave *what
may be aggregated* to the modeller's judgement. That gap is where the plausible
wrong numbers live.

### The narrow rule was the only correct rule

The obvious version — "any column a `sum` touches must be a flow" — refuses
`revenue`, because `revenue` is `sum(unit_price * quantity)` and `unit_price` is
a value-per-unit. A value-per-unit times a count genuinely is a flow.

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

### A pack that describes a table it also creates is two different things

chinook ships no level column, so `stock` needed one seeded. The plan put the
new `Inventory` object into `chinook/ontology.yaml`, which reads naturally and
does not work: the loader refuses an ontology naming a table the database does
not have, so the entire chinook pack — every test, the CLI's default domain, the
agent — would have failed to load on any machine where the opt-in seed had not
been run. The pack would have acquired a hard dependency on a tool that is, by
design, optional.

Splitting it into its own pack was the whole fix. `Inventory` lives in
`domains/chinook_inventory/`, alongside the SQL that creates its table, and a
caller loads it only after seeding.

**Transferable:** the rule "a domain pack describes a database it does not own"
has a corollary nobody stated — a pack that *does* ship schema cannot be the
same pack as one that must load without it. Opt-in schema needs an opt-in
ontology to name it.

### Where a quantity lives: the case that has no column decides it

`quantity` began on `Property`, which is where it obviously belongs: a price is
a value-per-unit, an amount is a flow, and both are facts about a column.

Headcount is the textbook `stock` and has no such column.
`count_distinct(employee.employee_id)` reads an *identifier* — `employee_id` is
not a level, and annotating it would be a lie about that column that happens to
produce the right verdict for one metric. Stock-ness is a fact about what the
metric's *result* means.

So `quantity` moved to `Metric`, and `Property.quantity` stayed, because it is
still the right place to say what a column is. A metric that is a `sum` over a
bare column and declares nothing inherits the property's word, which keeps the
common case at one annotation in one place — and is also exactly the path that
slipped past `_check_over_time` (above). Precedence is one-directional and the
metric wins; a metric and property that *disagree* raise nothing, which is the
one part of this still recorded as debatable rather than settled.

**Transferable:** put a declaration where the hardest instance of it can be
stated, not where the common one reads best. The common instance can usually be
inferred from the general place; the hard one cannot be expressed in the narrow
one at all.

### A NULL group key and a NULL instant are opposite problems

grain already had a NULL answer it was pleased with. `_key_is_nullable` picks
`IS NOT DISTINCT FROM` over `=` when rejoining a pre-aggregate on a nullable
key, precisely so a NULL group does not silently vanish — Lenz & Shoshani's
completeness condition, arrived at by bug. So the first instinct for a nullable
time column was the same move: match NULL to NULL and carry on.

They are opposites. A NULL *join key* is a **real group**: those rows exist,
they belong together, and `=` drops them, so `IS NOT DISTINCT FROM` *restores*
something the query already had. A NULL *instant* has nothing to restore.
`max(t)` over a partition has no answer for a row whose `t` is NULL, and
matching NULL to NULL there would not recover a fact — it would **decide** one:
that the undated rows form an instant of their own, ordered nowhere, which
nothing in the ontology declares and no author asked for.

So the loader refuses `time_grain` on a nullable column instead, and the error
names every repair that applies rather than the first (make the column `NOT
NULL`, or point the axis at one that already is; a column reached through a left
join needs both).

**What it cost.** Two attempts. The first reused the existing NULL handling
because it was there and the shape looked identical.

**Transferable:** "we already handle NULLs here" is not a reusable answer, it is
a per-column question. Ask whether the NULL is a *value you are missing* or a
*position on an axis*. Restoring the first is completeness; inventing the second
is a wrong number with an invented premise underneath it.

### A misspelled field name was not a typo, it was a different declaration

`extra="forbid"` was set on `OverTime`, the small nested model. That reads like
the careful thing to have done, and it is where a reviewer's eye goes.

It was absent from `Metric` and `Property` — the two models an ontology author
actually hand-writes. `Property(..., time_grian="day")` therefore parsed
cleanly, with the real field left `None`. That is not a typo with a typo's
consequences: `time_grain` is a field that changes a verdict when present and is
silently absent when not, so a misspelling did not fail, it **disabled a rule**.
Nothing declared a time axis, so nothing required `over_time`, so no window was
built, and the metric went on to report `additive: true` and sum a level across
time. `Metric(..., overtime={...})` did the same thing from the other side.

**What it cost.** One line per model, and nothing in the shipped packs or tests
had ever passed an unknown key, so no caller changed.

**Transferable:** on any model a human hand-writes, ignoring an unknown key
converts every misspelling into a *default* — and where the default means "this
rule does not apply", a typo turns a rule off. Set the strict option on the
outermost hand-written model first. The leaf model is the one that needs it
least.

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

### Making a rule unnecessary beats enforcing it

A `stock` sums across space and not across time, so the engine has to stop
anyone summing one across time. That reads as a validation problem: enumerate
the ways a query could cross the time axis, and refuse each.

There is nothing to enumerate. The window restricts each group to the rows at
one instant — `max(t) over (partition by <group keys>)` computed in a subquery,
and the outer query keeping only the rows where `t` equals it — and after that a
sum *cannot* cross time, because the set contains one instant. The
guarantee holds by construction. The three group-by shapes the design had
worried about separately (no `group_by` at all, a non-time key, the time
dimension itself) all fall out of that one partition without any of them being
named in code.

This is the same move as the `EXISTS` semi-join above. The pre-aggregate's
problem was that walking a fanning edge replicates rows; the fix was not a rule
about when walking is safe, but a construct that filters without multiplying. In
both cases the property stopped being policed and started being unbreakable.

Honest limit: the stock work still raises three refusals. None of them is about
crossing time — they are about the window's mechanical reach (a second stock, a
second metric, a pre-aggregate the wrap cannot see). A construction removes the
cases it covers; it does not remove the cases outside it.

**Transferable:** when a rule is about to enumerate cases, look for a
construction that deletes the cases. An enumeration is only as complete as its
author's imagination, and the case nobody thought of is exactly the one that
returns a plausible wrong number. Then be clear about which refusals remain and
why, so the two kinds are not confused later.

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

### A design goal can be met by the thing it was aiming past

Order statistics were built into the symmetric engine because that was the one
place the default engine was strictly more capable. The work landed in both
engines — the subquery engine needed nothing but the taxonomy entry, since
pre-aggregating at the grain already leaves `percentile_disc` looking at
distinct rows.

That second, almost-free implementation is what found the pre-aggregate bug. The
capability was the goal; the verification was the by-product, and the by-product
was worth more.

**Transferable:** where a second implementation is nearly free, take it even if
the first already works. Its value is not the feature.

### The problem was well-factored in 1997 and we solved it in bug order

Lenz & Shoshani gave three necessary conditions for a valid aggregation —
**disjointness** (a value rolls up to one group), **completeness** (it rolls up
to some group), and **type compatibility** (attribute type, dimension type and
aggregate must agree).

grain enforces the first two and names neither. Disjointness is the
overlapping-groups verdict; completeness is why `_key_is_nullable` picks
`IS NOT DISTINCT FROM` over `=`. Both were derived from a bug rather than from
the framework. The third we reached last and only partially.

It also cost us a worse vocabulary. Their taxonomy is **flow / stock /
value-per-unit**; ours was `extensive | rate | ratio`. `extensive` was `flow`
with a clumsier name, `rate` and `ratio` split something the literature does
not, and **`stock` was missing entirely** — which is the semi-additive case I
had recorded as needing "a subsystem rather than a field". The framework says it
is a third value of the same field, distinguished by which dimensions it may be
summed over.

Both were adopted: the field now reads `flow | stock | value_per_unit`, and the
`stock` case is that value plus a `time_grain` marker and an `over_time` choice.
No subsystem. The framework's reading was right, and the price of having
invented a vocabulary first was a **breaking rename with no alias path** —
deliberately no alias, because a value that silently mapped an old name to a new
one is the quiet accommodation this codebase avoids. Which means the docs are
the whole migration guide, and getting them wrong leaves a reader writing an
ontology that will not load.

**Transferable:** for a problem this old, read the 1997 paper before inventing
the vocabulary. Solving in bug order produces correct code and a taxonomy shaped
by which bug arrived first — and renaming it later is cheap only while nothing
outside the repo depends on the names. Full write-up in
`docs/QUANTITY-TYPES.md`.

### The elegant invariant was not the load-bearing one

"The tool schema *is* `QuerySpec.model_json_schema()`, verbatim, so the contract
cannot drift" was stated in three places and was the design's stated selling
point. It had to be abandoned — strict mode rejects keywords Pydantic emits.

Nothing was actually lost. The property that mattered was never the identity: it
was that *one generated object defines both* and that Pydantic remains the
enforcement boundary. Both survived. The verbatim-ness was decoration.

**Transferable:** when an elegant invariant breaks, ask which part of it was
doing the work. It is often not the part that made it elegant.
