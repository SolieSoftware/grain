# Quantity Types — Research

Prior art for a type system over the *quantities* metrics measure, and the rules
governing how those quantities may be combined.

Written because grain has been arriving at pieces of this independently, under
its own names, and the established framework is both older and better factored
than what we invented. This document records what exists, maps grain onto it,
and states what is genuinely missing.

**The aggregation half is now implemented; the composition half is not.** grain
took this document's vocabulary (`flow | stock | value_per_unit`) and built the
`stock` case on 2026-09-06 — see `docs/plans/2026-09-06-stock-and-time-design.md`.
Everything about composition, §5a and §6's algebra, remains reading behind a
design that has not been written.

---

## 1. Two literatures, both relevant

The idea splits cleanly into two traditions that have not much talked to each
other.

**Programming languages** solved the *composition* half: a type system where
quantities carry dimensions, and multiplication and division derive new
dimensions automatically while addition of incompatible dimensions is a type
error.

**Data warehousing** solved the *aggregation* half: under exactly what
conditions is it valid to aggregate a measure over a dimension at all.

grain needs both, and the second one turns out to be the deeper of the two.

---

## 2. Units of measure as a type system (Kennedy)

Andrew Kennedy's units-of-measure system, shipped in F#, is the canonical
programming-language treatment. [Types for Units-of-Measure: Theory and
Practice](http://typesatwork.imm.dtu.dk/material/TaW_Paper_TypesAtWork_Kennedy.pdf)
is the paper; [F# Units of
Measure](https://learn.microsoft.com/en-us/dotnet/fsharp/language-reference/units-of-measure)
is the shipped implementation.

The properties that matter for us:

**Dimensions compose by an algebra, not a lookup table.** Multiplying and
dividing derive the result's dimension automatically — divide a length by a time
and you get a velocity without anyone declaring `velocity`. Units multiply,
divide and **cancel**.

**Adding incompatible dimensions is a type error.** `float<m> + float<kg>` does
not compile. This is the property grain's `quantity` check gropes towards: a sum
over something that does not accumulate should not be expressible.

**It is fully inferrable**, integrated with Hindley–Milner unification via a
dimension-unification algorithm. Annotations are needed only at the boundaries;
the interior is inferred. Functions can be *generic* in their units.

**It is erased.** Units exist at compile time and vanish from compiled code, so
they cost nothing at runtime. For grain the analogue is that a quantity type
constrains what SQL may be *emitted* and never appears in the SQL itself — which
is exactly how `quantity` behaves today.

**What it does not give us.** Dimensional analysis says `revenue / customers` is
a `money/customer`, and says nothing about whether that number may then be
averaged across regions. Composition is not summarizability.

---

## 3. Summarizability (Lenz & Shoshani) — the deeper result

[Summarizability in OLAP and statistical databases](https://www.semanticscholar.org/paper/Summarizability-in-OLAP-and-statistical-data-bases-Lenz-Shoshani/72d5b3fec9a116f119a213633a2a75c96567d5ea)
(1997) is the foundational paper, and it is the one grain should have been read
against from the start. It gives **three necessary conditions** for an
aggregation to be valid:

### Disjointness

An attribute value must roll up to **only one** group at the higher level. If a
value belongs to two groups, aggregating over that level double-counts.

**grain already enforces this**, without using the word. It is exactly the
overlapping-groups problem: a track belongs to many playlists, so revenue by
playlist sums to 5738.28 against a true 2328.60. grain reports `additive: false`
and, where the per-group figure would also be wrong, refuses with
`NonAdditiveRefused`.

### Completeness

Each value must roll up to **some** group. A value belonging to no group is
silently dropped from the total.

**grain already handles this too**, again unnamed: `_key_is_nullable` chooses
`IS NOT DISTINCT FROM` over `=` when rejoining on a nullable key, precisely so a
NULL group key does not vanish. The reasoning in that function is a
rediscovery of the completeness condition.

### Type compatibility

The combination of **the attribute's type**, **the dimension's type**, and **the
aggregation function** must be consistent. This is the condition grain reached
last, and only partially.

Lenz & Shoshani classify summary attributes into three kinds, and these names
are better than the ones we invented:

| Their term | Meaning | Sums over space? | Sums over time? |
|---|---|---|---|
| **flow** | measured over a period — revenue, units sold | yes | **yes** |
| **stock** | a level at an instant — inventory, headcount, balance | yes | **no** |
| **value-per-unit** | a rate — unit price, exchange rate, temperature | **no** | **no** |

---

## 4. Where grain sits against this

grain now declares `quantity: flow | stock | value_per_unit` — these names, not
its own. It read `extensive | rate | ratio` when this document was written, and
the table below is what changed and why:

| grain | Lenz & Shoshani | Note |
|---|---|---|
| `flow` | **flow** | was `extensive`: the same concept under a name with no partner |
| `stock` | **stock** | was **missing entirely**; built 2026-09-06 |
| `value_per_unit` | **value-per-unit** | was `rate` and `ratio`, two names for one behaviour, now one |

Two conclusions followed, and they were the point of this document.

**The missing category was the important one.** grain had no way to say "sums
across accounts but not across time". That is `stock`, and I had previously
recorded it as *semi-additive* needing "a subsystem rather than a field". The
framework said otherwise: it is a **third value of the same field**,
distinguished by which dimensions it may be summed over. What grain lacked was
not a subsystem but a notion of *which dimension is time* — which is precisely
what dbt's `non_additive_dimension` supplies with `window_choice: max`.

That reading held. The implementation is one new value of `quantity`, one
`time_grain` marker on a property, and one `over_time: {dimension, choice}` on
the metric — no subsystem. The vocabulary is `first|last` rather than
MetricFlow's `min|max`, because `window_choice: max` reads as the largest VALUE
when it means the value at the latest DATE.

**`rate` versus `ratio` is a distinction the literature does not make**, and we
had made it only because "a rate does not accumulate" reads better in an error
than "this is non-additive". That was a real benefit and a real cost: two names
for one behaviour invite a future reader to look for a difference that is not
there. They were merged into `value_per_unit`, and the error message lost
nothing measurable: it names the metric, says a `value_per_unit` does not
accumulate, and offers `avg`, `min` or `max`. The word was never the part
carrying the reader.

---

## 5. The additivity taxonomy in data-warehouse practice

Kimball's additive / semi-additive / non-additive split is the practitioner
vocabulary for the same idea, and the DOLAP literature formalises it —
[An analysis of additivity in OLAP systems](https://dl.acm.org/doi/10.1145/1031763.1031779)
(2004) develops the taxonomy and its effect on summary data, and
[Detecting summarizability in OLAP](https://www.sciencedirect.com/science/article/abs/pii/S0169023X13001274)
addresses finding violations mechanically rather than trusting the modeller.

The recurring recommendation across that work is worth quoting in spirit:
**store the additivity properties as metadata and use them to restrict queries.**
That is precisely what grain's `quantity` field does, and the literature treats
it as the standard answer rather than an innovation.

Current implementations that carry some of this:

| Tool | What it models |
|---|---|
| **dbt MetricFlow** | `non_additive_dimension` with `window_choice: min\|max` — the `stock` case, tied to a named time dimension |
| **Cube** | non-additive measure handling, with pre-aggregation caveats |
| **Looker** | fan-out correctness via symmetric aggregates; no quantity typing |
| **grain** | fan-out correctness (both engines) + `flow`/`stock`/`value_per_unit` with windowing; no composition, no granularity |
| *nobody surveyed* | **a typed composition result** — the gap in §5a |

---

## 5a. Where MetricFlow stops — measured, not assumed

MetricFlow is the closest production system to what we want, so it is worth
being precise about the boundary.

**What it has.** `non_additive_dimension` (`name`, `window_choice: min|max`,
`window_groupings`) models the **stock** case properly, tied to a named time
dimension. It also has two composition kinds: **ratio** metrics
(numerator/denominator, each independently filterable) and **derived** metrics
(an arbitrary expression over other metrics).

**What it does not have: any typing of the composition's result.** Its
validation is syntactic — the docs say it *"warns if it is missing or references
undefined metrics"*, and that is the extent of it. There is no additivity
constraint, no `non_additive_dimension` inference, and no quantity type on a
derived or ratio metric. The docs are explicit about the consequence:

> MetricFlow does not prevent summing a ratio metric (like revenue per customer)
> across dimensions where such aggregation would be semantically incorrect. This
> places the burden of correctness on the data modeler and query author.

So `revenue_per_customer` can be summed across regions and nothing objects. That
is the same class of silent wrong number grain's `quantity` field exists to
prevent — reappearing one level up, at composition.

**This is the frontier.** Deriving the result's quantity type from its operands
is standard in Kennedy's system and absent from every semantic layer surveyed.
It is also the only mechanism that makes agent-defined metrics safe, because the
type system rather than the author decides what the result may be used for.

## 6. What a quantity type system for grain would actually need

Sketch only for the composition half — that design is not written. The first
item below is built.

**A lattice, not a flag — done.** The three conditions are independent, so a
quantity needs to say which *dimensions* it may be summed over, not merely
whether it may be summed. `flow` sums over everything; `stock` sums over
everything except time; `value-per-unit` sums over nothing. That is one field
plus a notion of which dimension is time, and it is what shipped: `time_grain`
marks the axis, `over_time` names the instant, and the engine windows to that
instant rather than checking that nobody sums past it. What is still absent is
any *granularity* meaning for `time_grain` — no `date_trunc`, so "inventory in
Q1" cannot be asked.

**A composition algebra**, which is where Kennedy comes back in. The useful
rules are few:

```
flow            / flow            -> value-per-unit    (revenue per customer)
flow            / count           -> value-per-unit    (average order value)
value-per-unit  * flow            -> flow              (price x quantity = revenue)
flow            + flow            -> flow              (same dimension only)
value-per-unit  + value-per-unit  -> TYPE ERROR
stock           + stock over time -> TYPE ERROR
```

The third line is the one grain already relies on: `revenue` is
`sum(unit_price * quantity)`, a `value-per-unit` times a `flow`, and the
`quantity` check special-cases it by inspecting only bare columns. **An algebra
would make that a derivation rather than an exception** — which is the strongest
argument for doing this properly.

**Composed metrics as a first-class kind.** A metric defined as
`revenue / customer_count` would carry a derived quantity type, and the engine
could refuse to sum the result without anyone declaring that it must not be
summed. This is the agent-defined-metric case: composition is safe *because* the
type system, not the author, decides what the result may be used for.

---

## 7. Honest assessment

**Where grain is ahead of the literature.** Lenz & Shoshani assume a star
schema, where the fact table already sits at one grain. grain's `traverse`
creates arbitrary join paths, so it faces a fan-out problem the 1997 framework
does not address at all — and both engines solve it, verified against an
independent oracle. The summarizability conditions assume the rows you are
aggregating are the right rows; grain's whole engine layer is about making that
true.

**Where grain is behind.** It rediscovered two of the three conditions by
running into them, named neither, and had only a partial version of the third.
Reading this literature first would have produced a better taxonomy and would
have identified `stock` as a missing *value* rather than a missing subsystem —
which is exactly what reading it did produce, one round later and at the cost of
a breaking rename with no alias path. Type compatibility is now complete for a
single measure; what remains behind is composition (§5a), where nobody surveyed
is ahead either.

**The transferable lesson**, for FINDINGS: the problem was well-factored in 1997
and we solved it in the order the bugs arrived. That produced correct code and a
worse vocabulary — `extensive` where the field wanted `flow`, and a `rate`/`ratio`
split the literature does not make.

---

## Sources

- Kennedy, [Types for Units-of-Measure: Theory and Practice](http://typesatwork.imm.dtu.dk/material/TaW_Paper_TypesAtWork_Kennedy.pdf)
- [F# Units of Measure](https://learn.microsoft.com/en-us/dotnet/fsharp/language-reference/units-of-measure) · [F# language specification, §9](https://fsharp.github.io/fslang-spec/units-of-measure/)
- Lenz & Shoshani, [Summarizability in OLAP and statistical data bases](https://www.semanticscholar.org/paper/Summarizability-in-OLAP-and-statistical-data-bases-Lenz-Shoshani/72d5b3fec9a116f119a213633a2a75c96567d5ea) (1997)
- [An analysis of additivity in OLAP systems](https://dl.acm.org/doi/10.1145/1031763.1031779) (DOLAP 2004)
- [Detecting summarizability in OLAP](https://www.sciencedirect.com/science/article/abs/pii/S0169023X13001274)
- [A Taxonomy of Inaccurate Summaries and Their Management in OLAP Systems](https://link.springer.com/chapter/10.1007/11568322_28)
- [Intensive and extensive properties](https://en.wikipedia.org/wiki/Intensive_and_extensive_properties)
- [dbt: measures and `non_additive_dimension`](https://docs.getdbt.com/docs/build/measures)
- [Cube: accelerating non-additive measures](https://cube.dev/docs/product/caching/recipes/non-additivity)
