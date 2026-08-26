# Symmetric Aggregate Engine — Design

**Status:** design approved 2026-08-26, not yet implemented.

**Goal:** Add a second, independently switchable query engine that computes
grain-correct aggregates in a single pass using *symmetric aggregates*, so that
queries the current engine refuses become answerable, and so that the two
engines can cross-check each other's numbers.

**Relationship to the existing engine:** additive. The current engine
(`resolve` → `analyse` → `compile_query`) is not modified. Its files do not
move and its import paths do not change, so all 241 existing tests keep
passing unaltered. A behaviour change in the existing engine during this work
is a defect, not a refactor.

**Prior art:** Looker's symmetric aggregates. This design deliberately departs
from Looker in one respect — see §4.

---

## 1. What this buys, and what it does not

Three of the four motivations hold. One does not, and the distinction matters
enough to state before any code is written.

**Fan-out replication** is one grain row counted N times *inside a single
group*, because a join downstream of the metric's grain multiplied it.
Symmetric aggregates fix this exactly.

**Overlapping groups** is one grain row legitimately belonging to *several
groups*. `revenue by Playlist` summing to 5738.28 against a true 2328.60 is
this: a track sits on many playlists, so its revenue genuinely appears in many
playlist groups. **Symmetric aggregates do not fix this and cannot.** The
double-counting is not an artifact of the SQL; it is the question the caller
asked. Such queries stay `additive: false` under both engines, with the same
`NonAdditiveRefused` guardrails the current engine enforces. Looker has the
same limitation.

So the deliverables are:

1. `KeyBeyondGrain` (grain.py:275) stops applying. A group key further along
   the path than the metric's grain, with a fanning edge in between, is
   currently refused outright because the pre-aggregating subquery would have
   to walk across the fan to reach the key. A symmetric aggregate needs no
   subquery, so the refusal has nothing to protect.
2. One statement instead of N pre-aggregated subqueries LEFT JOINed back.
3. A differential correctness oracle (§7) — two independent engines over one
   ontology must agree, and neither's expected values need hand-computing.

## 2. Verified evidence

The technique was validated against the live chinook database before this
design was written. All figures below are measured, not predicted.

**Exactness.** The classic fan-out — `revenue` at `invoice_line` grain, joined
through `track` and `playlist_track`:

```sql
select
  round(sum(il.unit_price*il.quantity), 2)                        as naive_fanned,
  round(
    sum(distinct il.invoice_line_id::numeric * 1e30
        + coalesce(il.unit_price*il.quantity, 0))
  - sum(distinct il.invoice_line_id::numeric * 1e30)
  , 2)                                                            as symmetric
from invoice_line il
join track t           on t.track_id  = il.track_id
join playlist_track pt on pt.track_id = t.track_id;
```

`naive_fanned = 5738.28`, `symmetric = 2328.60`, true total `2328.60`. Exact to
the cent through a join that inflated the naive figure by 146%.

**Grouped exactness.** The same technique grouped by `customer.country` through
the same fanning join, compared row-by-row against the per-country truth: **24
countries, 24 exact matches, 0 mismatches.**

**Performance: not established.** Chinook is too small to time reliably. The
identical 3-metric numeric query measured 23.2 ms and 15.8 ms on separate runs,
and `K=1e30` measured *faster* than `K=1e12`, which contradicts the
digit-count hypothesis it was meant to test. Run-to-run variance exceeds the
effects being measured.

The one gap large enough to probably be real:

| query shape | `aggregate_then_join` | symmetric (numeric) |
|---|---|---|
| 1 metric | 4.2 ms | 17.9 ms |

**No cost heuristic may be built on these numbers.** Engine selection is
explicit (§3) precisely so the crossover can be measured on a realistic dataset
rather than guessed. Establishing it is follow-on work, not part of this design.

## 3. Architecture

### The seam

Engines share **only the loaded `Ontology`**. Everything from the `QuerySpec`
onward is engine-owned:

```
loader.py, ontology.py, errors.py          shared
  │
  ├── engine/            (existing)  resolve → analyse → compile_query
  └── engine_symmetric/  (new)       resolve → analyse → compile_query
  │
guard, execute, adapters                   shared
```

This is a deliberate choice of the wider seam over the narrower one. The
narrower seam (engines share `resolve()`, own only planner and compiler) would
avoid duplicating the qualified-group-key and filter resolution built in the
previous branch. The wider seam is chosen anyway, because a bug in shared
`resolve()` is invisible to a differential test — both engines would inherit it
and agree on the wrong answer.

**Accepted cost:** the qualified-key mechanism (`_group_key`, `edge_index`,
`ResolvedProperty.qualified`) and the `order_by` validation are duplicated. A
fix to one is not a fix to the other. This is a maintenance liability and is
recorded as such; if it becomes painful, narrowing the seam later is a
one-directional change that only loses differential coverage.

### Selection

Three levels, most specific winning:

1. `api.query(spec, engine="symmetric")` — explicit per call.
2. `GRAIN_ENGINE` environment variable — session default.
3. Built-in default: `subquery`. The existing engine remains the default until
   the new one has earned otherwise on measured evidence.

`QueryResult` gains an `engine: str` field. A result that does not say which
engine produced it is not usable for comparison, which is the whole point.

The CLI gains `--engine {subquery,symmetric}` on the query and explain paths.

## 4. The symmetric technique

For a metric at grain table `G` with unique key `k` and per-row value
expression `v`:

```
SUM(DISTINCT k*K + COALESCE(v,0)) - SUM(DISTINCT k*K)
```

This equals `Σ COALESCE(v,0)` over the **distinct rows of G present in the
join**, which is the wanted figure. Correctness rests on four conditions, each
of which is either proved or enforced:

**(a) `k` uniquely identifies a row of `G`.** Therefore equal `k` implies equal
`v`, so replicated rows produce an identical encoded term and collapse under
`DISTINCT`. Enforced by the loader, which already verifies `unique`
declarations against the database's own primary keys, unique constraints and
unique indexes.

**(b) Distinct `k` produce distinct encoded terms.** Sufficient condition:
`|COALESCE(v,0)| < K/2`. Proof: if `k₁K + v₁ = k₂K + v₂` with `k₁ ≠ k₂`, then
`|k₁-k₂|·K = |v₂-v₁| ≥ K`, while `|v₂-v₁| ≤ |v₁|+|v₂| < K` — a contradiction.

**(c) The arithmetic is exact.** Postgres `numeric` is arbitrary-precision
decimal, so no overflow ceiling and no truncation. `K = 1e30` by default, which
makes condition (b) `|v| < 5e29`.

**(d) `COALESCE` is mandatory.** Without it a NULL `v` makes the first term
NULL for that row — dropped by `SUM` — while `k*K` still appears in the
subtracted second term. Measured: a single NULL turns a true `10.00` into
`-1999999999999999999999999999990.00`. Loud rather than plausible, but a wrong
answer regardless.

### Where this departs from Looker

Looker hashes the key (`MD5 → bigint`) and packs it against a `FLOOR`-scaled
value inside a fixed-width `NUMERIC(38,0)`. That buys portability across MySQL,
Redshift and BigQuery, and it costs two silent failure modes: **hash
collisions** drop a row's value with no signal, and **`FLOOR` truncation** loses
digits below the chosen scale.

grain is Postgres-only (`psycopg` is the sole driver), so this design uses the
real integer key and arbitrary-precision `numeric`: no hashing, therefore no
collisions; no fixed width, therefore no truncation. A project that spent its
previous branch replacing plausible wrong numbers with enforced invariants
should not adopt a technique whose failure mode is a silently dropped row.

### Decision: enforcing condition (b)

`|v| < K/2` would otherwise be an *assumption* about the data, of exactly the
kind this codebase has been eliminating. Three options were considered:

1. **Document the bound only.** `|v| < 5e29` is absurd for money. Cheapest,
   and an assumption nonetheless.
2. **Load-time verification.** `SELECT max(abs(v)) FROM G` at load, refusing a
   metric with insufficient headroom. Consistent with how `unique` and
   `nullable` are already verified — but data changes after load, so it
   confirms rather than guarantees.
3. **Self-enforcing SQL.** Emit a guard term that raises rather than returns a
   wrong number: `sum(case when abs(v) >= K/2 then 1/0 else 0 end)`. Ugly, and
   the only option that cannot be violated by later data.

**Decided: (2) plus (1).** The loader verifies headroom at load and the bound
is documented in the ontology reference. (3) is held in reserve: a
division-by-zero guard on every symmetric metric is a large readability cost
against a contingency this remote, and `|v| < 5e29` is unreachable for
monetary and count data. Revisit if a non-monetary domain pack lands, or if
(2) ever fires in practice — one real hit would mean the bound is closer than
this reasoning assumes.

Note precisely what (2) does and does not give: it confirms the bound against
the data *present at load*, and the engine reloads on startup. It is a check,
not a guarantee, and §10 records the residual risk.

## 5. Aggregate taxonomy

The current planner decides strategy from path cardinality alone (grain.py:264)
and never inspects the metric. That is why `count(distinct <pk>)` metrics —
`track_count`, `customer_count`, `employee_count` in the chinook pack — are
pushed through a pre-aggregating subquery despite being immune to fan-out.
Classifying the aggregate is the larger part of the versatility win:

| aggregate | fan-out behaviour | treatment |
|---|---|---|
| `min`, `max` | **immune** — min of replicated rows is min of distinct rows | plain inline, no rewrite |
| `count(distinct k)` | **immune** — `DISTINCT` on the key already dedupes | plain inline, no rewrite |
| `sum` | replicates | symmetric form above |
| `count(*)` | replicates | `count(distinct k)` |
| `avg` | replicates | `sym_sum / count(distinct k) FILTER (WHERE v IS NOT NULL)` |
| opaque `expr` | unknown | not eligible; **refused**, see below |

The symmetric engine implements **only** the treatments in this table. It has
no `aggregate_then_join` of its own — duplicating that rewrite as well would
double the most intricate function in the codebase for no differential gain,
since the subquery engine already owns it and is one flag away. A metric the
table cannot serve is therefore *refused* by this engine (§8), naming the
`subquery` engine as the alternative. The symmetric engine is a specialist, not
a superset.

`avg` takes the `FILTER` because `avg` ignores NULL values, so the divisor must
count only rows contributing to the numerator — `COALESCE`-to-zero in the
numerator would otherwise drag the mean down.

## 6. Ontology change

The symmetric form must wrap the per-row value and leave the aggregate function
outside it, so a metric declared as one opaque string cannot be used:

```yaml
revenue:
  grain: invoice_line
  agg: sum                                              # new
  value: "invoice_line.unit_price * invoice_line.quantity"   # new
  type: decimal
```

`expr` remains valid and is unchanged in meaning. A metric declaring `expr`
is not symmetric-eligible: the subquery engine serves it exactly as it does
today, and the symmetric engine refuses it (§8). Nothing in the chinook pack
must be migrated for the existing engine to keep working; metrics are migrated
to gain symmetric eligibility.

This closes a gap the original implementation plan already recorded: *"the spec
structured join conditions (R1) but left metric `expr` as a SQL fragment"*
(`docs/plans/2026-08-17-grain-engine.md`).

**Validation, in the loader, shared by both engines:**

- `agg` must be one of the taxonomy's functions.
- `agg`/`value` and `expr` are mutually exclusive; exactly one form per metric.
- Every `table.column` token in `value` must belong to the metric's own grain
  table — the same rule `expr` already obeys and `_metric_expr` relies on.
- `type: float`/`double` with `agg: sum` is refused for symmetric use: binary
  floating point is not exact under the encoding. Refused rather than silently
  losing digits.
- The grain table must have a single-column integer **primary key**, as read
  from the database by the loader — not merely a property declared `unique`.
  Condition (a) of §4 needs row identity, which is what a primary key is.
  Composite and text keys are a non-goal (§9).

## 7. Differential testing

Two independent engines over one ontology are a correctness oracle for each
other. The test that matters most is not a fixed expected value but an
agreement assertion:

- For every spec in a shared corpus, run both engines and assert the returned
  rows are identical. A disagreement is a defect in one of them, surfaced
  without anyone hand-computing the answer.
- Where an engine refuses, assert *which* refusal — a query the symmetric
  engine answers and the subquery engine refuses with `KeyBeyondGrain` is the
  feature working, and must be asserted as such rather than skipped.
- The existing measured anchors (`test_measured_anchors.py`,
  `test_defect_anchors.py`) run against both engines. Their hand-verified
  figures remain the ground truth that stops both engines agreeing on a wrong
  number.
- The corpus includes the non-additive cases from §1, asserting both engines
  return `additive: false` and the *same* figures — the guarantee that this
  feature did not quietly change what a non-additive query means.

The benchmark harness and the correctness harness are the same harness: it
already runs every spec through both engines, so timing is an added column.

## 8. Errors

New failures follow the existing convention — a typed error naming a legal
alternative that itself resolves:

- `MetricNotSymmetric(metric, reason)` — the symmetric engine was asked for a
  metric it cannot serve. Names the reason (opaque `expr`, float type, no
  single-column integer primary key) and names the `subquery` engine as the
  alternative, since that engine answers every such metric today.

  It fires however the engine was chosen — per-call argument, `GRAIN_ENGINE`, or
  future default. Selecting an engine is not a hint to be silently overridden:
  an engine that quietly answered via a different strategy than the one asked
  for would make the differential harness (§7) meaningless, because agreement
  would no longer prove two implementations agree.
- `NoIntegerKeyForGrain(metric, grain_table)` — the grain table's primary key is
  composite or non-integer, so §4's condition (a) cannot be met. Distinct from
  `MetricNotSymmetric` because the fix is different: this one is a property of
  the schema, not of the metric declaration, and no ontology edit resolves it.

`KeyBeyondGrain` is *not* removed. It remains correct for the subquery engine
and must keep firing there; the symmetric engine simply never raises it.

## 9. Non-goals

- **Cross-dialect portability.** Postgres only, consistent with the rest of the
  engine. The hashed encoding that would buy portability is rejected in §4.
- **Composite or text primary keys.** Refused with `NoIntegerKeyForGrain`, not
  half-supported. Text keys would require the hashing this design rejects.
- **Automatic engine selection by cost.** Explicitly deferred — the measurements
  to justify a heuristic do not exist (§2).
- **Fixing overlapping-group non-additivity.** Not possible (§1).
- **Migrating the chinook pack's metrics wholesale.** Metrics move to
  `agg`/`value` as they need symmetric eligibility.

## 10. Implementation phasing

The design is one coherent change but too large for a single sitting. Four
phases, each independently testable and each leaving the tree green:

1. **Taxonomy in the existing engine.** Recognise `min`/`max`/`count(distinct k)`
   as fan-out immune in the current `analyse`, removing subqueries from queries
   that never needed them. No new package, no ontology change, immediate win.
   Requires the `agg` declaration (§6) to be readable, so §6's loader work
   lands here.
2. **The `Engine` seam.** `api` and CLI gain engine selection, `QueryResult`
   gains `engine`, with `subquery` the only registered implementation. Proves
   the seam without a second engine behind it.
3. **The symmetric engine.** `engine_symmetric/` with its own resolve, analyse
   and compile; symmetric `sum`/`count`/`avg`; `KeyBeyondGrain` absent;
   refusals per §8.
4. **The differential harness.** §7, including the timing column.

Phase 1 is worth landing and measuring on its own even if phases 2–4 are
deferred.

## 11. Risks

1. **Duplicated resolver.** Accepted in §3. The qualified-key logic now exists
   twice and can drift. Mitigation: the differential corpus covers qualified
   keys specifically, so drift shows up as disagreement rather than silence.
2. **Performance unknown.** The feature may be slower than the strategy it
   supplements for the common single-metric query. Mitigation: it is opt-in,
   the default stays `subquery`, and the harness measures rather than assumes.
3. **`SUM(DISTINCT ...)` on wide numerics** sorts the full fanned row set. This
   is the plausible mechanism behind the one real measured gap and may not
   amortise at scale. It is a property of the technique, not of this
   implementation; if it dominates, the honest outcome is that the symmetric
   engine is a capability tool rather than a performance one.
4. **The `|v| < K/2` bound survives only as a load-time check.** Data written
   after load can cross it, and the failure mode is a wrong number rather than
   an error, since condition (b) fails silently. This is the weakest point in
   the design and the reason option (3) is kept in reserve rather than
   discarded.
5. **`1e30` as a literal.** Postgres types `1e30` as `numeric`, verified. Must
   not be allowed to become `double precision` through a cast or a driver
   coercion — a float there would silently reintroduce the inexactness §4
   exists to avoid. Asserted in a compile-level test on the emitted SQL.
