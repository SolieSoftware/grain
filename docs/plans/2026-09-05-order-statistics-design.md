# Order Statistics in the Symmetric Engine — Design

**Status:** implemented 2026-09-05. See `docs/plans/2026-09-05-order-statistics-plan.md`.

**Goal:** Let the symmetric engine answer `median` and `percentile(p)` over a
fanning join, correctly, in one pass. This closes the only case where the
default `subquery` engine is strictly more capable.

**Scope:** `median` and `percentile` for any exactly-representable numeric
value — integer and decimal alike. Not restricted to integers.

---

## 1. Why the existing technique does not reach this

The symmetric sum works because a sum decomposes:
`SUM(DISTINCT k*K + v) - SUM(DISTINCT k*K)` recovers `Σv` over distinct rows
because addition is associative and the key contribution cancels exactly.

An order statistic does not decompose. A median is not recoverable from sums; it
needs the multiset of distinct values in order. The BI literature states this
correctly — *"a median, a percentile, a running total... has no equivalent
distinct-sum rewrite, so the math simply does not hold."*

That claim is true and narrower than it sounds. It rules out the **distinct-sum**
rewrite. It does not rule out a different encoding, and one exists.

Fan-out genuinely does corrupt an order statistic, so this is worth solving:
the median `track.milliseconds` over `track → playlist_track` returns **256026**
against a true **255634**.

## 2. The encoding

```
encoded  =  (value * 10^s)::numeric * 1e19  +  pk
result   =  floor( sorted_distinct[ greatest(1, ceil(p * n)) ] / 1e19 ) / 10^s
```

emitted as a single aggregate expression, no subquery:

```sql
floor(
  (array_agg(DISTINCT (v * 10^s)::numeric * 1e19 + pk
             ORDER BY   (v * 10^s)::numeric * 1e19 + pk))
  [greatest(1, ceil(p * count(DISTINCT pk)))]
  / 1e19
) / 10^s
```

Four things make it work:

**Ordering.** `10^s` and `1e19` are positive constants, so the encoded number
sorts in the same order as `v`. The key occupies the low-order digits and breaks
ties without disturbing the ordering.

**Deduplication.** `DISTINCT` applies to the encoded pair, which is equivalent to
deduplicating on the key — `v` is functionally determined by `pk`. This is why
the key must be in the encoding at all: `array_agg(DISTINCT v)` would collapse
two different rows that happen to share a value, and they are two data points.

**Indexing.** `greatest(1, ceil(p * n))` is Postgres's own `percentile_disc`
definition — the first value whose cumulative fraction reaches `p`. Verified
exact at p = 0, 0.25, 0.5, 0.75, 0.9 and 1.0 against `percentile_disc` on the
unfanned table.

`s` throughout is the scale derived from the reflected column (§4), never from
the ontology's declared `type`.

**Decoding.** `floor(encoded / 1e19)` strips the key. Correct for negatives too:
`floor(-5e19 + 3 / 1e19)` is `-5`.

## 3. Two bounds, and why neither needs a runtime check

This is where the design is *better* than the symmetric sum it sits beside.

**`K = 1e19` exceeds any key — by proof.** A bigint cannot exceed 9.22×10¹⁸.
The requirement is `K > max(pk)`, and the key's own type guarantees it. Nothing
is checked at load and nothing can drift as data grows.

Contrast the symmetric sum's `|v| < 5e29`, which bounds a *value* — unbounded by
type — and so is a load-time check that later writes can silently violate. That
is recorded as the design's weakest point. This encoding does not inherit it.

**`v * 10^s` is an exact integer — by construction**, when `s` is the column's
own declared scale. SQLAlchemy reflects it: `NUMERIC(10,2)` gives `scale=2`.
Multiplying by `10^2` clears the fraction exactly, with nothing to round.

**This is precisely where Looker's implementation loses digits and this one does
not.** Looker `FLOOR`-scales to a *fixed guessed* precision — 6 by default, and
its own docs advise lowering it to 5 for large values, which loses more. A
guessed scale must truncate. A scale read from the schema cannot.

## 4. What must be refused

Refusal is by `MetricNotSymmetric`, naming the `subquery` engine, which computes
all of these correctly by pre-aggregating at the grain.

**Eligibility is decided from the REFLECTED column, not the declared `type`.**
This matters: `ValueType` has no float member, so a `double precision` column can
be — and would be — declared `decimal` in an ontology. Trusting the declaration
would let an inexact column through. The database is authoritative here, exactly
as it already is for cardinality, uniqueness and nullability.

Scale is derived as: `Integer` → `s = 0`; `Numeric` with a declared scale →
that scale; anything else → **no scale, refuse**. Float and unconstrained
`numeric` both fall into the last case naturally, without needing to be named.

| Case | Why |
|---|---|
| reflected column has no derivable scale | covers `float`, `real`, `double precision` and unconstrained `numeric` in one rule — binary floating point has no exact decimal scale, so encoding would round |
| a value that is not a bare column | a scale can be read off a column, not off `a * b` |
| non-numeric columns (text, date) | not encodable into an orderable number |
| a grain without a single-column integer key | the existing `NoIntegerKeyForGrain` condition |

The bare-column restriction mirrors the `quantity` rule added for non-additive
quantities, and for the same reason: the schema can answer a question about a
column that it cannot answer about an expression.

## 5. Ontology surface

`AggFunc` gains two members:

```yaml
median_duration:
  grain: track
  agg: median
  value: track.milliseconds
  type: integer

p90_duration:
  grain: track
  agg: percentile
  percentile: 0.9        # new field, required for agg: percentile, else refused
  value: track.milliseconds
  type: integer
```

`percentile` is a new optional `float` on `Metric`, validated in the existing
`_check_exactly_one_form` style: required when `agg: percentile`, refused
otherwise, and constrained to `0.0 <= p <= 1.0`.

**Both are fan-out sensitive**, so neither joins `FANOUT_IMMUNE`. In the
subquery engine they take `aggregate_then_join` like any other non-immune
aggregate, and `sql_expr` renders them as `percentile_disc(p) within group
(order by value)` — which is correct there because the subquery has already
reduced to distinct grain rows.

## 6. Semantics: `disc`, not `cont`

`percentile_disc` returns a value that actually occurs in the data.
`percentile_cont` interpolates between the two middle values of an even-sized
set, returning a number that may appear nowhere.

The array-index technique yields `disc` naturally, and `disc` is the more
defensible default: "the median track duration" should be a duration some track
actually has.

`cont` is a deliberate **non-goal** for now. It is reachable — take both middle
elements and average them — but it is a second specification, and adding it
without a caller asking would be speculative.

## 7. Cost, stated plainly

`array_agg(DISTINCT ...)` materialises the whole distinct set per group, where
the symmetric sum streams. For a query with few large groups this is the
dominant cost and will be worse than the subquery engine, which aggregates at
the grain before joining.

This is a capability, not an optimisation — the same conclusion the engine
evaluation reached for symmetric aggregation generally, where the measured cost
was 3–5× and widening with scale. The benchmark harness (`tools/bench.py`)
should gain an order-statistic shape so the number is measured rather than
assumed.

## 8. Testing

- **Exactness against the unfanned truth**, for integer and decimal values, at
  several percentiles, over a genuinely fanning join. The naive figure must
  differ, or the test proves nothing — 256026 against 255634 is the anchor.
- **Negative values**, which the floor-decode handles but which no chinook
  column exercises.
- **Every refusal in §4**, each asserting the error names the `subquery` engine.
- **Differential**: the corpus gains median and percentile specs, and both
  engines must agree. The subquery engine computes them by a completely
  different route, so agreement is real evidence.
- **The oracle** (`tools/oracle.py`) gains order-statistic support, since it is
  the only check that shares no SQL with either engine.

## 9. Risks

1. **`array_agg` memory on large groups.** The realistic failure is a query that
   works on chinook and exhausts memory on a real dataset. §7's benchmark should
   establish where that line is before anyone relies on it.
2. **The bare-column restriction may frustrate.** `median(price * quantity)` is a
   reasonable thing to want and is refused. The subquery engine answers it, and
   the error says so, but it is a real edge.
3. **`percentile` widens the ontology surface.** A new field that is required
   for exactly one aggregate is the kind of conditional schema that invites
   mistakes; the validator has to be as strict as `_check_exactly_one_form`.
