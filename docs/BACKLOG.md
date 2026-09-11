# Backlog — what to build next, and why

Ordered. Each item names the problem, the prior art that settles part of it, and
what it costs if skipped. An item leaves this file when a design doc exists for
it in `docs/plans/`.

Sources: `docs/QUANTITY-TYPES.md` (summarizability and units-of-measure),
`docs/prior-art/foundry.md` (Palantir's Ontology), and `docs/FINDINGS.md`.

---

## 1. A `many_to_one` edge in a metric's prefix is not tested for

Both engines decide whether a metric's groups overlap by walking the metric's
own prefix and asking one question of each edge:
`effective_cardinality == "many_to_many"`. That question is too narrow. A
**`many_to_one`** hop means the grain rows are the *parents*: one grain row is
reachable from many root rows, so it lands in several groups at once and the
column cannot sum to the total. Same defect, same mechanism, different
cardinality — and nothing anywhere tests for it.

The result is the exact shape the derived-additivity work exists to prevent: a
correct per-group figure carrying a **false claim** about itself, delivered to an
agent that `agent/prompt.py` instructs to trust the claim. `additive: true` is
precisely the licence to total the column, and `agent/tools.py::_caveats` stays
silent.

**Measured, both engines, no caveat emitted:**

- `Customer --Customer_SupportRep--> Employee`, `employee_count` grouped by
  `email`. Reported `additive: true`; 59 groups of 1, totalling **59**. The true
  headcount is **3** — `select count(distinct support_rep_id) from customer`.
- `Track --Track_Album--> Album --Album_Tracks--> Track --Track_InvoiceLines-->
  InvoiceLine`, `revenue` grouped by `composer`. Reported `additive: true`; 826
  groups, totalling **35368.77** (subquery) and **9346.71** (symmetric), against
  the **2328.60** anchor.

`Customer_SupportRep` and `Track_Album` are the `many_to_one` hops.

**PRE-EXISTING, not introduced by the derived-additivity branch.** Both repros
are byte-identical at `9014e49`. The restructuring made the verdict a function of
the plan; it did not change which facts the plan records, and this hole is in the
fact-gathering.

**Neither standing safety net can see it**, which is why it is first rather than
a note. The differential harness needs the two engines to *disagree*, and here
they share the gap — the `many_to_many`-only test is duplicated into
`engine_symmetric/grain.py` along with everything else, so both report
`additive: true` and agree. `tools/oracle.py` answers the same per-group question
and **agrees per group**, correctly: every group IS right. It is the total that is
wrong, and no per-group cross-check has an opinion about a total nobody computed.
That is the third time this pairing has been blind to the same class of defect
(C1, C5, and the windowed level).

**Related and not the same: the empty-prefix hole.** A metric measured at the
root has no prefix at all, which `engine/grain.py` already records beside
`_ADDITIVITY_INPUTS` and which `KeyBeyondGrain` catches by accident. Fixing that
one — following the path to the GROUP KEY, as
`engine_symmetric/grain.py::_overlap` already does — **would not close this one**:
both measured cases have a non-empty prefix and are grouped by a bare root key,
so `_overlap` skips them (`edge_index is None`). Two holes, one location.

**Prior art is thin, and that is informative.** MetricFlow and Cube attach
additivity to a metric *definition*, so neither asks this question of a traversal
at all. `docs/QUANTITY-TYPES.md`'s summarizability conditions are the closest
thing: they are conditions on a (measure, dimension, aggregate) triple, and
**disjointness** — does each grain row belong to exactly one group? — is the
condition being got wrong here, named and stated there. Read §3 before designing.

**Also in scope for the same design, because it is the same conflation:**
`median_duration` and `p90_duration` grouped by any dimension report
`additive: true` (ungrouped `255634` against a per-genre sum of `16715217`).
grain's `additive` means "the groups partition the rows" and is read as "this
quantity sums"; an order statistic never sums, however the rows are partitioned.
That is §3's **type compatibility** condition, on the aggregate rather than the
dimension, and grain has no equivalent of it. Pre-existing and identical on
`main`.

**Cost of skipping:** grain's single rule is that a wrong number is worse than no
answer. This is worse than either — a right number with a wrong licence attached,
where the caller doing the arithmetic is an agent, in a process no harness is
watching. Every other item on this list is a capability; this one is a defect
that ships wrong totals today.

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
