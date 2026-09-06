# Stock Quantities and a Time Dimension — Design

**Status:** implemented 2026-09-06. See
`docs/plans/2026-09-06-stock-and-time-plan.md`. §6 is still out of scope, as
written; §5's engine asymmetry landed as designed, with the symmetric engine
refusing a stock.

**Goal:** Let grain express a quantity that sums across space but not across
time — an inventory level, a balance, a headcount — and compute it correctly by
windowing to a period boundary rather than summing.

**Prior art:** dbt MetricFlow's `non_additive_dimension`, and Lenz & Shoshani's
`flow / stock / value-per-unit` taxonomy. Background in
`docs/QUANTITY-TYPES.md`, which establishes that `stock` is the one category
grain's `quantity` field is missing.

---

## 1. Why this is a rename as well as a feature

`quantity` currently reads `extensive | rate | ratio`. That vocabulary was
invented before the literature was read, and it is wrong in two ways.

**`rate` and `ratio` are one thing.** Nothing in the codebase branches on the
difference — the only use of either is interpolating the word into an error
message. The literature does not make the distinction. And under the composition
algebra that comes later, "dimensionless" versus "dimensioned" becomes
*derivable* (do the units cancel?), so declaring it would be redundant and could
contradict the derivation.

**`extensive` has no partner.** `flow` and `stock` are a matched pair from
economics; `extensive` beside `stock` reads as two half-adopted vocabularies.

So the field becomes:

```
quantity: flow | stock | value_per_unit
```

Doing this now rather than later is deliberate: it is a one-field change to one
domain pack today, and it becomes a much larger migration once composition rules
are written against the names.

## 2. Where the declaration lives

**`quantity` moves from `Property` to `Metric`.**

The reason is headcount, which is the textbook `stock` and which a column-level
field cannot express at all: `count_distinct(employee.employee_id)` has no
quantity column — `employee_id` is an identifier. Stock-ness is a property of
what the metric's *result* means, not of a column it reads.

**Inference keeps the existing declarations working.** For a metric that is a
`sum` over a bare column, an omitted `quantity` is taken from that column's
property if the property declares one. So chinook's four annotations
(`invoice.total` and `invoice_line.quantity` as `flow`; `invoice_line.unit_price`
and `track.unit_price` as `value_per_unit`) continue to do their job, and the loader's refusal is unchanged for them.

Precedence is one-directional and stated: **a metric's own `quantity` wins**; the
property is consulted only when the metric is silent. Where both are present and
disagree, the metric is authoritative and no error is raised — the metric is
closer to the meaning of the number.

`Property.quantity` stays, because it is still the right place to say what a
*column* is, and it is what makes the inference possible.

## 3. The time dimension

grain has `type: date` and `type: datetime` but nothing marking a property as
*the time axis*. A property now may:

```yaml
Inventory:
  primary: daily_inventory
  properties:
    as_of:  {column: daily_inventory.as_of_date, type: date, time_grain: day}
    units:  {column: daily_inventory.units_on_hand, type: integer}
```

`time_grain` declares both that this property IS a time axis and the granularity
at which the underlying data is recorded. Legal values: `day`, `week`, `month`,
`quarter`, `year`. The loader verifies the column's reflected type is a date or
timestamp — a `time_grain` on a text column is refused, since the whole point is
that ordering and comparison are meaningful.

`time_grain` is recorded but **not yet used to re-bucket** anything; see §6.

## 4. The stock declaration and its semantics

```yaml
inventory_level:
  grain: daily_inventory
  agg: sum
  value: "daily_inventory.units_on_hand"
  quantity: stock
  over_time: {dimension: as_of, choice: last}
  type: integer
```

`over_time` is required when `quantity: stock` and refused otherwise — the same
shape as the `percentile` field added for order statistics, and validated the
same way. `dimension` must name a property of the metric's own object that
declares a `time_grain`. `choice` is `first | last`.

**`first | last`, deliberately not MetricFlow's `min | max`.** `window_choice:
max` reads as "the largest value" when it means "the value at the latest date".
Two readings, one of them wrong, in a field whose entire job is to disambiguate.

**Semantics.** Within each group the query asks for, restrict to the rows whose
time value is the first/last in that group, and *then* aggregate. A `sum` over a
stock therefore never sums across time, by construction rather than by a check.

Three cases, all well defined:

| Query groups by | Window is over | Result |
|---|---|---|
| nothing | the whole population | the level at the global latest date |
| a non-time dimension | each group | that group's level at its own latest date |
| the time dimension itself | each group, which holds one date | a no-op; the window changes nothing |

## 5. Compilation, and the engine asymmetry

The window needs the maximum time value *per group* before the aggregate runs,
which means a window function inside a subquery:

```sql
select key, sum(units) from (
  select *, max(as_of) over (partition by key) as pick from t
) s
where as_of = pick
group by key
```

**The subquery engine can do this; the symmetric engine cannot.** The symmetric
engine is defined as one pass over the join, and this shape is not expressible
without a subquery. It therefore refuses a `stock` metric with
`MetricNotSymmetric`, naming the `subquery` engine.

That is consistent with the engine being a specialist rather than a superset,
and it is the second such asymmetry (opaque `expr` metrics are the first). It is
stated here rather than discovered, because it means the differential harness
cannot cross-check stock windowing — the corpus entry for it belongs in
`DIVERGENT` with this reason recorded.

**Losing the differential check matters**, and the mitigation is that
`tools/oracle.py` gains stock support. The oracle computes in Python from raw
rows and does not care whether a subquery was involved, so it remains an
independent judge even where the two engines cannot be compared. Given the
pre-aggregate defect the harness found, an unverifiable path is a real cost and
the oracle is the thing that makes it acceptable.

## 6. Deliberately out of scope

**Granularity re-bucketing.** grain cannot group by month: there is no
`date_trunc`, and grouping by a timestamp groups by the exact instant. So
*"inventory in Q1"* is not expressible, and neither is any period other than the
data's own grain.

What v1 does deliver is the case that needs no bucketing: *"total inventory
now"*, and *"inventory by genre now"*. That is the useful half, not all of it,
and `time_grain` is declared now so the later granularity work has somewhere to
attach.

**`window_groupings`.** MetricFlow lets a semi-additive measure name entities to
group by before windowing — take each user's latest MRR, then sum. Reachable
later; omitted now because it is a second specification and nothing has asked
for it.

**Stock in the symmetric engine.** §5. Would need a formulation that is not one
pass, which contradicts what that engine is.

**Composition.** A derived metric's quantity is the next phase and is what this
taxonomy exists to serve. Not here.

## 7. Test data — and what it changes about a domain pack

chinook has no stock. No inventory, no balances, no snapshot table; its three
datetime columns are all event stamps. So a `daily_inventory` table is added:

```
daily_inventory(track_id int, as_of_date date, units_on_hand int)
primary key (track_id, as_of_date)
```

created by a migration shipped with the domain pack, over a handful of tracks
across a handful of dates — small enough that end-of-period figures can be
computed by hand and asserted, which is the standard every other feature here
meets.

**This crosses a line worth naming.** Until now a domain pack has only
*described* a database it did not own. Shipping a migration makes the pack a
producer of schema, which changes what a domain pack is and what loading one
might do to a database. The migration is therefore explicitly opt-in, run by a
tool rather than by `Grain.load`, and the pack's ontology does not reference
`daily_inventory` unless the table exists.

The alternative considered was injecting synthetic stock metrics at test time, as
the `price_sum` and `median_price` tests already do. Rejected because the
windowing would then be exercised only against data that is not actually a
stock, so the anchors would assert mechanism and not meaning.

## 8. Testing

- **Measured anchors.** End-of-period inventory computed by hand from the seeded
  table and asserted. The naive figure — summing across dates — must differ, or
  the test proves nothing.
- **The three windowing cases** of §4, each asserted separately.
- **`choice: first`** as well as `last`, since a design that only ever tested
  the default would not know the field was read.
- **The refusals:** `over_time` missing on a stock; `over_time` present on a
  flow; `dimension` naming a property with no `time_grain`; `time_grain` on a
  non-temporal column; a stock metric under the symmetric engine.
- **Inference:** a `sum` over a bare column with no metric-level `quantity`
  still refuses when the property says `value_per_unit`, proving the migration
  did not silently drop the existing guarantee.
- **The oracle** gains stock support, per §5.

## 9. Risks

1. **The rename touches a public field.** Any ontology using
   `extensive|rate|ratio` stops loading. Only chinook exists today, so the blast
   radius is four lines, but there is no deprecation path and none is proposed —
   a `quantity` value that silently mapped from an old name to a new one is the
   kind of quiet accommodation this codebase avoids.
2. **Stock is unverifiable by the differential harness** (§5). The oracle is the
   mitigation and it is a weaker one than two engines disagreeing.
3. **The seeded table is small.** Hand-computable is the point, but a windowing
   bug that only appears at scale — a tie on the maximum date across many rows,
   say — would not surface. The tie case should be seeded deliberately.
4. **`time_grain` is declared but unused in v1.** A field nothing reads is
   exactly what this codebase calls a silent assumption with a field name on it.
   It is accepted here only because the loader *does* verify it against the
   column's type, so it is checked even while its granularity meaning is unused.
