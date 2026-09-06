# Backlog — what to build next, and why

Ordered. Each item names the problem, the prior art that settles part of it, and
what it costs if skipped. An item leaves this file when a design doc exists for
it in `docs/plans/`.

Sources: `docs/QUANTITY-TYPES.md` (summarizability and units-of-measure),
`docs/prior-art/foundry.md` (Palantir's Ontology), and `docs/FINDINGS.md`.

---

## 1. Derive a plan's claims from the plan

`analyse()` computes a metric's WINDOW and its ADDITIVITY VERDICT in the same
loop and independently of each other. Nothing structurally stops them
disagreeing, and they did: the additivity loop asks only "do the groups
overlap?", a stock's do not, so a windowed stock was reported `additive: true`
while `_window_to_boundary` was collapsing each group to its OWN boundary
instant. Every group was correct and their total was a level at no instant —
1153 by `as_of`, which is exactly the across-time sum the branch exists to
prevent, computed by grain and labelled summable.

That one case is fixed. The general problem is not: a `MetricPlan` carries
claims ABOUT its result (`additive`, `non_additive_reason`) that are written
beside the decisions they describe rather than derived from them. Any future
strategy, window or rewrite can add a way for the number and the claim about it
to part company, and the next instance will be found the same way — by a person
reading both halves and noticing.

**Neither standing safety net can see this class of defect**, which is what
makes it worth its own item rather than a note. The differential harness needs
two engines to disagree, and a claim made in the plan layer is not SQL —
worse, the symmetric engine refuses a stock outright, so there is no second
answer at all. `tools/oracle.py` answers the same per-group question and agrees
per group; it has no opinion about a total nobody computed. This is the third
Critical of this shape (C1: 5738.28 as one unlabelled figure; C5: 6 for a truth
of 3, reported additive) and the first two were also found by review, not by a
test.

**The shape of a fix.** Make the verdict a function of the finished plan rather
than a variable accumulated alongside it — `additive(metric_plan)` reading
`strategy`, `window`, `prefix` and `group_by`, so a new field that affects
summability has one place it must be handled and a missing case is an
unhandled branch rather than a silent `True`. Prior art is thin: MetricFlow and
Cube both attach additivity to a metric DEFINITION, not to a query result, so
neither has this problem or its solution. `docs/QUANTITY-TYPES.md`'s
summarizability conditions are the closest thing — they are conditions on a
(measure, dimension, aggregate) triple, which is nearer to a derivation than a
flag.

**Cost of skipping:** every wrong number this system can still produce goes out
with a correct figure attached to a false claim about it, and the caller is an
agent that was told to trust the claim.

## 2. A usable time dimension

The stock work shipped a time **axis**. Two things stop it being a time
**dimension**, and neither is optional:

**2a. A time-valued filter.** `FilterScalar` is `str | int | float | bool`. A
`datetime.date` raises five validation errors at the spec boundary, and a string
bind compiles to `invoice_date < $1::VARCHAR`, for which Postgres has no
operator. So a caller cannot bound a range at all. Pinned in
`tests/integration/test_shared_limits.py`; documented in `CLAUDE.md` as the
second exception to the raise-before-connecting rule.

**2b. Granularity re-bucketing.** No `date_trunc`, so grouping by a timestamp
groups by the exact instant. "Inventory by month" is inexpressible. `time_grain`
exists and is verified against the column type precisely so this has somewhere to
attach.

Together these mean *"inventory in Q1"* fails twice over. 2a lands first — a
grain you cannot filter to is not much of a grain.

**Cost of care:** `FilterScalar` widens `QuerySpec`, and
`QuerySpec.model_json_schema()` **is** the agent's tool `input_schema`. That
identity is deliberate: the contract the model is held to and the contract the
engine enforces cannot drift. Widening it is an agent-facing change, and strict
mode has already broken twice on this project — once on a schema node with no
`type`, once on `minimum`/`maximum`.

**Both engines.** Truncation is a group-key transformation upstream of the
aggregate, so unlike stock windowing there is no evident reason the symmetric
engine cannot serve it. It should. If it turns out it cannot, that deserves the
same scrutiny the stock refusal got — and serving it restores the differential
harness for the time dimension, which stock lost.

## 3. Continuous verification instead of load-time-only checks

**This is the item Foundry actually settled, and it targets the design's stated
weakest point.**

The symmetric engine's sum encoding pairs `K = 1e30` with `BOUND = 5e29`. That
bounds a **value**, which nothing in the schema constrains, so it is a *load-time*
check and later writes can violate it silently. Contrast the array encoding used
for `median`/`percentile`: `KEY_OFFSET = 1e19` bounds a **key**, and a bigint
cannot exceed 9.22e18, so the key's own type guarantees it. A proof, not a
measurement. `CLAUDE.md` already names the gap and says a self-enforcing SQL
guard is designed and held in reserve.

Foundry's OSv2 primary-key check is **continuous**: a duplicate primary key fails
the index build, not a load-time sample ([foundry.md](prior-art/foundry.md)). That
is the same pattern, demonstrated in production. It is evidence the approach is
viable, which is what was missing.

Extend the idea past the encoding bound to every declaration the loader currently
verifies once: cardinality, uniqueness, nullability. A `CHECK` constraint or a
generated column makes the database itself the enforcer, so a declaration cannot
drift from the data between loads.

**Why it matters here specifically:** grain's rule is "enforce, don't assume — a
declaration nothing checks is a silent assumption with a field name on it." A
declaration checked *once* is a weaker version of the same problem.

## 4. Aggregation by identity — a third strategy

Foundry does not solve fan-out; it **declines to have it**. An object set is an
unordered collection of objects of one type, identified by primary key, and
`searchAround` returns an object set rather than a row product. There is no row to
replicate, so each object is counted once by construction.

For the path it covers that is a better answer than either of grain's engines,
and grain never considered it — because grain treats "the substrate is
relational" as a premise rather than a choice.

The transferable piece: where a metric's grain has a single-column key and the
traversal only needs *membership* rather than row multiplicity, the answer can be
computed by deduplicating on identity, with no pre-aggregate subquery and no
symmetric encoding. Cheaper than both, and correct by construction rather than by
a check.

**Do not oversell it.** Foundry's cost is that cross-grain measures must be
pre-decided as derived properties, capped at three hops — grain's traversal
queries are inexpressible there. So this is a third *strategy* for `grain.py` to
select, not a replacement for either engine. Scope it to where it is provably
equivalent, and refuse rather than approximate elsewhere.

## 5. Composition, with a derived quantity type

The frontier, and what the taxonomy exists to serve.

`docs/QUANTITY-TYPES.md` §5a establishes with MetricFlow's own docs that it has
ratio and derived metrics but performs **no typing of the composed result** — its
validation is syntactic, and its docs concede it will not stop you summing
`revenue_per_customer` across regions. Foundry is one step further back: it has
no metric object at all, so there is nothing to type — while carrying *more*
stock machinery than grain (`Last`, time-weighted average, integral, cross-series
sum) with no typing over any of it.

Nothing surveyed does this. Kennedy's units-of-measure system does the analogous
thing for physical dimensions, by an algebra rather than a lookup table.

```
flow           / flow            -> value_per_unit
flow           / count           -> value_per_unit
value_per_unit * flow            -> flow
flow           + flow            -> flow
value_per_unit + value_per_unit  -> TYPE ERROR
stock          + stock over time -> TYPE ERROR
```

**The payoff is not the new feature.** `revenue = sum(unit_price * quantity)` is
a `value_per_unit` times a `flow`, and today it is a **special case** in the
loader — the quantity rule deliberately inspects only bare columns so that
`revenue` is not refused. An algebra turns that exception into a derivation. That
is a better argument for building it than any gap in a competitor.

It is also what makes agent-defined metrics safe: the type system, not the
author, decides what a composed result may be used for.

**Verification warning.** These rules live entirely in the loader — the shared
layer. The inferred-stock defect proved the differential harness is blind there:
both engines inherited the missing rule and **agreed on the wrong number**. For
this item the oracle and adversarial review must carry the verification, and that
should be planned for from the start rather than discovered.

## 6. Semi-additive `window_groupings`

MetricFlow's remaining semi-additive feature: name entities to group by *before*
windowing — take each account's latest balance, then sum across accounts. grain
currently windows over the query's own groups only.

Small next to the others, and it composes with item 2 rather than competing.

---

## 7. Publish stock-ness as a structured field

`src/grain/engine/describe.py` tells the agent that a `sum` over a level is
refused — but the agent can only learn that a given metric IS a level by reading
the metric's free-text `description`, and nothing enforces that the ontology
author wrote it there. So the rule names a fact the agent cannot reliably see.

The same reasoning already settled this for `unique`, which IS published as a
structured field: a rule naming a fact the agent cannot see is not actionable.

Both the implementer and the reviewer of the documentation task reached this
conclusion independently, from opposite directions. The constraint at the time
was that the published metric dicts must not change the tool's `input_schema` —
which is `QuerySpec.model_json_schema()` and is deliberately identical to the
contract the engine enforces. Publishing a field on the *described metric* is not
a change to the *query* schema, so the constraint may not actually bind. Check
that before designing.

## 8. `_group_key` cannot see the links, so it misdiagnoses a bad qualifier

Fourth instance of one defect class, and the last one still open.

`group_by=["Customer.name"]`, where `Customer` is neither the query's root object
nor any traversed link, advises *"add the Customer hop to traverse"* — which
fails with `UnknownName: Unknown link 'Customer'`. The later alternatives in the
same message do name real links, so the reader recovers; it costs a turn, never a
wrong number.

**Pre-existing.** The same line is on `main`; the stock branch did not introduce
it. It survived because `_group_key` has no view of `onto.links` and so cannot
tell "a link you did not traverse" from "not a link at all". Fixing it is a
signature change in **both** copied resolvers (`engine/resolve.py` and
`engine_symmetric/resolve.py` — deliberately duplicated, so a fix to one is not a
fix to the other).

The pattern is worth naming, since it has now appeared four times on one feature:
**an error that guesses at the user's intent will eventually guess wrong, and a
confident wrong guess is worse than an honest "I don't know what you meant."**
Each instance was caught the same way — by building and running the alternative
the message named, never by reading the message.

## Not doing, and why

**Object-storage substrate.** Foundry's fan-out immunity comes from not having
rows at the query surface. Adopting that wholesale means giving up arbitrary
traversal, which is grain's reason to exist. Item 4 takes the idea where it is
provably equivalent and no further.

**Cardinality refusal in a generated typed client.** Foundry's OSDK makes
`.selectProperty()` across a many-link a compile error — the strictest
cardinality-driven refusal found anywhere. But it reads a declaration nobody
verified, so it is confident about something unchecked. grain refuses at compile
time with an error naming a resolving alternative, against a declaration the
loader has verified. That is the better trade for now.
