# Palantir Foundry's Ontology — Prior Art

Prior art for a declared ontology layer over enterprise data, read against the
specific problems grain exists to solve: fan-out correctness, summarizability
typing, declared-then-verified metadata, and refusal over guessing.

Written because Foundry is the most-cited "ontology over data" system in
commercial use, and because its architecture answers grain's central question in
a way grain did not consider — by removing the row, not by reasoning about it.

**Nothing here changes grain's implementation.** It is the reading behind a
design, and one correction to an assumption grain has been carrying.

Foundry is a closed commercial product. Everything below is sourced from
Palantir's public documentation, its developer community, and its own SDK
reference. All URLs accessed 2026-09-06.

---

## 0. Evidence grades

This repo's standard is that an unverified claim is a defect. Foundry cannot be
run here, so every claim below carries a grade, and the grades are used
strictly.

| Grade | Meaning |
|---|---|
| **[D]** | **Documented.** Palantir's own docs state it, quoted or closely paraphrased. |
| **[I]** | **Inferred.** Follows from two or more **[D]** facts, but no source states the conclusion. Each inference names its premises. |
| **[V]** | **Vendor claim.** Marketing or blog assertion with no mechanism given. Recorded, not relied on. |
| **[U]** | **Unsettled.** The public record does not answer the question. Said plainly rather than filled in. |

No claim in the comparison table (§10) rests on a **[V]**.

---

## 1. The data model

**[D]** The Ontology's building blocks are *object types* ("the schema definition
of a real-world entity or event"), *properties* ("the schema definition of a
characteristic of a real-world entity or event"), *link types* ("the schema
definition of a relationship between two object types"), *action types*,
*functions*, and *interfaces*
([core concepts](https://www.palantir.com/docs/foundry/ontology/core-concepts)).

**[D]** An object type has a primary key: "the property that acts as a unique
identifier for each instance of an object type", and "Each row in the backing
datasource must have a different value for this property"
([create an object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type)).

**[D]** Link cardinality is declared, and the four cases are named. A link type
is created by choosing a *relationship type* first, which fixes the available
cardinality: object-type foreign keys give one-to-one and many-to-one; a join
table dataset gives many-to-many
([create a link type](https://www.palantir.com/docs/foundry/object-link-types/create-link-type)).
For a one-to-one or one-to-many link, "a property of one object type (the foreign
key) refers to the primary key property of the other object type"
([link types overview](https://www.palantir.com/docs/foundry/object-link-types/link-types-overview)).
Many-to-many links are backed by a join table of primary-key pairs, with an
explicit mapping of which column refers to which side
([link type metadata](https://www.palantir.com/docs/foundry/object-link-types/link-type-metadata)).

So the *shape* of grain's ontology is present and, in one respect, better
factored: Foundry distinguishes many-to-one from one-to-many as separate
declarations rather than one symmetric `cardinality` field, which is the
direction information grain's resolver has to recover from the traversal
direction.

**[D]** Properties may carry *value types* — "semantic wrappers around a field
type that include metadata and constraints". The available constraints are enum,
range (numeric bounds; string length; array size), regex, RID and UUID
validation, array element uniqueness, and struct element constraints
([value types](https://www.palantir.com/docs/foundry/object-link-types/value-types-overview),
[value type constraints](https://www.palantir.com/docs/foundry/object-link-types/value-type-constraints)).
**[D]** These constraints are enforced: value types "enforce their validation
constraints on data in Builder pipelines and the ontology".

There is **no unit-of-measure or dimension concept** among them, and no
row-level uniqueness constraint — the array uniqueness constraint is *within one
array value*, not across rows. **[D]**

---

## 2. Declared, and *not* verified — the sharpest documented answer

grain's third principle is that a declaration nothing checks is a silent
assumption with a field name on it. Foundry's public record answers this
directly, and the answer is that cardinality is trusted.

**[D]** Cardinality "indicates to applications if each object type in the link
type has one or many objects"
([link type metadata](https://www.palantir.com/docs/foundry/object-link-types/link-type-metadata)) —
metadata *for applications*, not a constraint on data.

**[D]** And explicitly: "The one-to-one cardinality serves as an indicator of the
intended relationship, but the one-to-one cardinality is not enforced"
([create a link type](https://www.palantir.com/docs/foundry/object-link-types/create-link-type)).

**[U]** What happens when a declared many-to-one link is violated by the data —
whether the singular side resolves arbitrarily, errors, or returns one row
non-deterministically — is not documented anywhere I could find. This matters,
because it is precisely the case grain's loader refuses at load time.

Primary-key uniqueness is the one place Foundry does verify, and it verifies it
at a different layer than grain does:

**[D]** On Object Storage v2, "if new rows are added to your dataset, and it
results in duplicate values for the column being used as the primary key, then
this will cause an indexing failure so the duplicate objects will not show up".
On Object Storage v1 (Phonograph), "no indexing failure will occur. Instead, a
random row will be picked and this might change between syncs"
([community: how do I make sure my primary keys are unique?](https://community.palantir.com/t/how-do-i-make-sure-my-primary-keys-are-unique/307)).
The docs add the same split and the instruction "Be sure to check your backing
datasources for duplicates before assigning a primary key"
([create an object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type)).

**[D]** Actions enforce it at write time: "a `[Actions] ObjectsAlreadyExist`
error will be thrown if you try to create a new object with a duplicate primary
key" (same community thread).

This is worth crediting honestly. **Foundry's primary-key check is stronger than
grain's uniqueness check in one respect: it is continuous.** grain's loader
verifies declared `unique` against the database's own keys once, at load; OSv2
re-verifies on every incremental index build and fails the build. That is the
shape of the self-enforcing guard grain has designed and held in reserve for the
symmetric encoding's `|v| < 5e29` bound. Foundry did not solve *that* problem,
but it demonstrates the pattern working in production for a different one.

**[D]** Note also that the recommended way to get the guarantee on OSv1 is a
*data expectation* in the pipeline that fails the build on duplicate primary
keys — verification pushed into the pipeline, not the ontology (same thread).

---

## 3. Aggregation across links — the central question

Foundry's answer is architectural, and it is different from both of grain's.

### 3.1 There is no row

**[D]** "An object set represents an unordered collection of objects of a single
type"
([object sets](https://www.palantir.com/docs/foundry/functions/api-object-sets)).
Static object sets are "saved as primary key lists"
([object backend overview](https://www.palantir.com/docs/foundry/object-backend/overview)).
"Objects can also be uniquely identified by their object type and primary key"
([object identifiers](https://www.palantir.com/docs/foundry/functions/object-identifiers)).

**[D]** Link traversal from an object set produces another object set of the
target type — `searchAroundToOtherObjectType()` "returns a new object set of the
target type" — and this is limited to three hops: "the number of Search Around
operations you can conduct in a single search is currently limited to 3", and
beyond that "the search will fail at runtime"
([object sets](https://www.palantir.com/docs/foundry/functions/api-object-sets)).

**[D]** Aggregation is always over one object set of one type. The available
operations are `.count()`, `.average()`, `.max()`, `.min()`, `.sum()`,
`.cardinality()`, with bucketing by top values, exact values, ranges, fixed
width, and time units, and `.groupBy()` / `.segmentBy()` (same page).

**[I]** *Therefore the classic fan-out cannot arise on this path.* Premises: an
object set is a collection of objects of a single type; objects are identified by
primary key; traversal yields an object set rather than a row product;
aggregation consumes an object set. There is no join product to sum over, so
`sum(p)` over an object set of type `T` counts each `T` object once by
construction, however many paths reached it.

This is a genuinely different answer from grain's, and a stronger one for the
shape it covers. grain has two engines — a pre-aggregate-and-rejoin and a
symmetric-aggregate encoding — whose entire job is to undo a row multiplicity
that Foundry's query model never creates. Foundry did not solve the fan-out
problem; it declined to have it, by refusing to expose a relational row at the
query layer at all.

The cost is expressiveness, and it is the exact cost grain's `traverse` was built
to avoid paying. In Foundry, "revenue by customer country" is not one query over
a join path. It is either an aggregation over an `Invoice` object set grouped by
a country property that must already be *on* the invoice, or a derived property
(§3.2) that pulls the aggregate down onto `Customer`. grain's answer — one
`QuerySpec` naming a root object, a traversal path, a group key and a metric,
with the engine choosing inline / pre-aggregate / refuse — has no counterpart.

**[I]** Foundry's derived-property mechanism *is* grain's `aggregate_then_join`,
with the strategy choice moved from the engine to the modeller. Premises: a
derived property aggregates over linked objects at the linked type's own grain
(§3.2) and is then available for aggregation at the base type's grain. grain
decides between inline and pre-aggregate from declared cardinality; Foundry
requires the modeller to have decided, and encodes the decision in the ontology.

### 3.2 Derived properties, and the one real refusal

**[D]** Derived properties are "calculated at runtime based on the values of
other properties or links on objects. This includes aggregating on or selecting
properties of linked objects"
([derived properties](https://www.palantir.com/docs/foundry/ontology/derived-properties)),
they traverse "up to 3 levels of linked objects", and the available aggregations
are count, average, sum, min, max, approximate and exact cardinality, collect
list and collect set
([derived properties reference](https://www.palantir.com/docs/foundry/object-link-types/derived-properties)).

**[D] The key rule: when a link has many cardinality, "you must select an
Aggregation to combine the values"** (same page).

**[D]** And this is enforced *in the type system* in the TypeScript OSDK. The
derived-property builder carries a cardinality flag in its type —
`DerivedProperty.Builder<Employee, false>`. After a one-to-many pivot "the flag
flips from `false` to `true` … where you must call `.aggregate()` … because
there's no single value to select". `.selectProperty()` across a many-cardinality
link does not compile
([OSDK advanced queries](https://palantir.github.io/osdk-ts/react/advanced-queries/)).

```typescript
// legal: many-to-one, single value exists
managerName: (base: DerivedProperty.Builder<Employee, false>) =>
  base.pivotTo("manager").selectProperty("fullName")

// required after a one-to-many pivot
reportCount: (base: DerivedProperty.Builder<Employee, false>) =>
  base.pivotTo("reports").aggregate("$count")
```

**This is the closest thing in any surveyed system to grain's grain analysis.**
It is a static, cardinality-driven refusal, decided with no data access, and it
is checked by a compiler rather than at runtime. It is narrower than grain's — it
decides only *"is a single value available here"*, never *"may this value be
summed"* or *"is this group one root row"* — but within its scope it is a
refusal, not a warning, and it is not weakenable by the caller.

It is also only as good as the declaration it reads, and §2 established that the
declaration is not verified. **[I]** A link declared many-to-one over a
non-unique foreign key type-checks, and `.selectProperty()` is permitted on it.
Premises: the flag is derived from declared cardinality; cardinality is not
enforced. What the runtime then returns is **[U]**.

### 3.3 Where the fan-out reappears

**Documented, and reachable with no guardrail at all: Ontology SQL.**

**[D]** Foundry exposes the Ontology through standard Spark SQL. Object types are
queried by RID, and many-to-many link types are exposed as their own relation
tables, joined by hand:

```sql
SELECT c.* FROM `ri.ontology.main.relation.0` AS linkTable
INNER JOIN car AS c ON c.carId = linkTable.person_vehicles
WHERE linkTable.car_drivers = 'person-123'
```

([Ontology SQL](https://www.palantir.com/docs/foundry/sql-warehousing/ontology-sql),
[SQL in Foundry](https://www.palantir.com/docs/foundry/sql-warehousing/overview)).

On this path Foundry offers exactly what a warehouse offers: hand-written joins
over link tables, with `SUM` applied to the product. grain's anchor number is
reproducible here. Nothing in the Ontology's declared cardinality participates.
Ontology SQL is a billed first-class query type alongside search-around and
aggregation queries
([Ontology query compute](https://www.palantir.com/docs/foundry/ontologies/query-compute-usage)),
so this is not a legacy corner.

**Inferred, and the more interesting case: pulling a one-side property down.**

**[I]** The fanned sum is expressible without SQL. Premises, both **[D]**:
(a) `.selectProperty()` across a many-to-one link is legal and yields a scalar
property on the many side; (b) derived properties are then available for
aggregation. Composing them, a derived property `InvoiceLine.invoiceTotal =
pivotTo(invoice).selectProperty(total)` is legal, and `sum(invoiceTotal)` over an
`InvoiceLine` object set is legal — and is grain's 20848.62 against a true
2328.60. The object-set model prevents fan-out from a *join*; it does not prevent
a value at one grain from being copied to a finer grain and then totalled, which
is the same wrong number by a different route.

I could not find this case discussed anywhere in Palantir's documentation. The
conclusion is mine, not theirs, and the composition step (that such a derived
property is summable in the same request) is **[U]** — I have documentation that
derived properties are aggregatable and documentation that many-to-one
`selectProperty` is legal, and no source that puts them together.

**[D]** For completeness on the absence side: Foundry's ontology-design guidance
— best practices and a named anti-pattern catalogue (the Kitchen Sink, the God
Object, the Golden Hammer, deep inheritance chains, the Time Machine) — contains
no discussion of grain, granularity, aggregation correctness, or double-counting
across links
([best practices and anti-patterns](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices-and-anti-patterns),
[best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices)).
In a documentation corpus this thorough, that absence is itself evidence: the
problem is not one Foundry frames for its modellers.

---

## 4. Metrics — there is no metric concept

**[D]** The Ontology's type list (§1) has no metric, measure, or aggregate type.
Aggregation is a *query-time verb* over an object set, or a derived property, or
custom code in a Function. "Metric" in Foundry's documentation refers to
platform observability
([AIP observability metrics](https://www.palantir.com/docs/foundry/aip-observability/metrics))
or to deprecated model-evaluation metric sets
([MetricSet reference](https://www.palantir.com/docs/foundry/evaluate-models/metric-sets-reference)) —
not to a declared business measure.

**[V]** Palantir is explicit that this is deliberate, and rejects the framing:
"The Ontology is not a 'semantic layer'; the fourfold integration and
operationalization of data, logic, action, and security cannot be accomplished
with a thin semantic layer or a monolithic design"
([the Ontology system](https://www.palantir.com/docs/foundry/architecture-center/ontology-system)).

The consequence for this comparison is direct. grain's `metric` — a named
aggregate with a declared grain, an `ai_context`, a `quantity`, and a resolver
that decides how to compute it — has **no counterpart in the Ontology**. A
Foundry metric is whatever the caller wrote at the call site, or whatever a
modeller froze into a derived property.

---

## 5. Summarizability and quantity typing — absent

**[D]** No additivity, summarizability, or quantity concept appears anywhere in
the Ontology's type system. Value-type constraints (§1) cover value *ranges* and
*formats*, not what an aggregation may do with the value. Nothing prevents
`sum()` over a unit price, an exchange rate, a temperature, or a ratio.

This is not a close call and it is not a gap I had to hunt for; the vocabulary is
simply not present. Against §3 of `QUANTITY-TYPES.md`:

| Lenz & Shoshani condition | Foundry |
|---|---|
| **Disjointness** | Not modelled. **[I]** A many-to-many link means an object legitimately belongs to several groups; aggregation over the object set is correct per group, and Foundry says nothing about whether the groups may be totalled. Premises: object sets dedupe (§3.1); no additivity metadata exists (§5). grain reports `additive: false` and, where the per-group figure would also be wrong, refuses. |
| **Completeness** | **[U]** for grouping. Foundry's aggregation does report `excludedItems` counts in some responses ([aggregate objects](https://www.palantir.com/docs/foundry/api/ontology-resources/objects/aggregate-objects)), which is a partial signal, but whether a null group key is dropped is not documented. |
| **Type compatibility** | **Not modelled at all.** No flow / stock / value-per-unit distinction. |

So on the axis `QUANTITY-TYPES.md` identifies as the frontier — typing the result
of composition — Foundry is not behind dbt MetricFlow. It is a step further back:
MetricFlow at least has a `non_additive_dimension` field and a syntactic check on
derived metrics. Foundry has neither, because it has no metric object to hang
them on. **[D]**/**[I]**

---

## 6. Time

Foundry has substantially *more* temporal machinery than grain, and no typing
over it.

**[D]** Object types can declare an **event** capability, which designates start
and end timestamp properties, so applications know the object denotes an interval
([Map: events](https://www.palantir.com/docs/foundry/map/events)).

**[D]** Properties can be **time series properties** — "a specific type of object
property that stores a history of timestamped values, unlike a conventional
object property which contains a single value", where "each timestamp and value
pair represents a quantity at a point in time"
([time series properties](https://www.palantir.com/docs/foundry/time-series/time-series-properties),
[time series overview](https://www.palantir.com/docs/foundry/time-series/time-series-overview)).
Sensor object types may additionally declare a units property and an
interpolation method
([sensor object type setup](https://www.palantir.com/docs/foundry/time-series/create-sensor-ot)).

**[D]** Time series aggregation in Quiver offers, per series: Sum, Mean, Standard
deviation, Max, Min, Difference, Relative difference, Product, Count, **First**,
**Last**, **Time-weighted average**, **Integral**; and across series: Sum, Mean,
Standard deviation, Max, Min, Product
([time series aggregations](https://www.palantir.com/docs/foundry/quiver/timeseries-aggregations)).
A linked-series aggregation card "generates a new time series by performing a
linear aggregation on time series data from linked objects"
([linked series aggregation](https://www.palantir.com/docs/foundry/quiver/card-linked-series-aggregation)).

**This is the `stock` case's machinery, complete and unguarded.** `Last` over a
window is `window_choice: max`. `Time-weighted average` and `Integral` are the
correct reductions for a level and for a rate respectively — grain has neither.
Cross-series `Sum` sums a level across space, which is exactly right for a
balance. And the aggregation reference presents all of these as an undifferentiated
menu: I found no flow / stock / rate distinction in it, and no restriction tying
a permitted aggregation to a kind of measurement. Summing a stock across time, or
a rate across anything, is one menu selection with no objection. (Absence, so
**[D]** for the menu's contents and **[I]** for the conclusion that nothing
restricts them — the sensor `units` property exists but no source connects it to
aggregation validity.)

So Foundry and grain are wrong in opposite directions on the semi-additive case.
grain cannot *express* a stock and refuses to pretend otherwise. Foundry can
express it, can compute it correctly, and cannot tell you when you have not.

**[U]** Foundry's dataset-level versioning and transaction history are real, but
I found nothing connecting them to the aggregation layer — no documented "as of"
or point-in-time object query. The commonly repeated modelling guidance is that
objects are mutable and history belongs in time series properties or events
rather than in versioned object copies, and Palantir names the alternative the
"Time Machine" anti-pattern
([best practices and anti-patterns](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices-and-anti-patterns)).
Time-travel over the *ontology* for the purpose of aggregation is not something
the public record establishes.

---

## 7. The compute path

**[D]** Ontology queries execute against purpose-built object storage, not by
emitting SQL to one warehouse. Object Storage v2 separates indexing from
querying, uses incremental object indexing, and features "fully parallelized
Spark compute as part of a query"; an Object Data Funnel orchestrates writes from
datasources and user edits into object databases
([object backend overview](https://www.palantir.com/docs/foundry/object-backend/overview),
[Ontology query compute](https://www.palantir.com/docs/foundry/ontologies/query-compute-usage)).
**[D]** OSv2 spins up on-demand Spark clusters for search-arounds over 100,000
objects.

How much of grain's problem this removes, and how much returns:

- **Removed.** Row multiplicity at the query layer, for the object-set path
  (§3.1) — because the unit of the answer is an object, not a row. **[I]**
- **Returns, unchanged.** On Ontology SQL, in full, with hand-written joins over
  link tables (§3.3). **[D]**
- **Returns, relocated.** Object types are materialised by pipelines. A fanned
  join inside Pipeline Builder or a code transform produces a *correct-looking
  object type at the wrong grain*, and nothing downstream can detect it — the
  Ontology inherits the number as fact. **[I]**, premise: object types are backed
  by datasources built by pipelines
  ([link types overview](https://www.palantir.com/docs/foundry/object-link-types/link-types-overview)).
  This is grain's failure mode moved one layer earlier, where there is no
  declared cardinality to consult.
- **Returns, new.** Approximation. See §8.

---

## 8. Refusal vs. best-effort — the philosophical difference

This is the sharpest divergence, and Palantir documents it plainly, which is to
their credit.

**[D]** "In Foundry, depending on the complexity and volume of data you are
working with, applications may not display results with full accuracy (also
known as 'inexact aggregations') due to the nature of aggregations with high
cardinality."

**[D]** Callers may set `AggregationExecutionMode` to `PREFER_ACCURACY`, where
"the API response is slower but provides more accurate results **without full
accuracy guarantee**". "Object Explorer and Workshop requests to OSS do not
specify `AggregationExecutionMode`, and OSS defaults to `PREFER_SPEED`." The
response carries an `AggregateResultAccuracy` field "to indicate whether the
result is accurate."

**[D]** Users may see: "Too many values for `column`, not all are displayed";
"Showing approximate results due to computational limitations"; "Only loading
first 1,000 values per property. Filter your data for more accurate results."

**[D]** The OSDK and Functions paths always use `PREFER_ACCURACY` and guarantee
an `ACCURATE` response within their limited aggregation complexity; Functions
cannot group and order simultaneously and must sort in memory. Aggregations cap
at 10,000 buckets, and top-values bucketing approximates above 1,000 distinct
values
([aggregation considerations](https://www.palantir.com/docs/foundry/object-backend/aggregation-considerations),
[object sets](https://www.palantir.com/docs/foundry/functions/api-object-sets)).

Read against grain's governing rule — *a wrong number is worse than no answer* —
Foundry's default posture on its two most-used interactive surfaces is the
opposite: **return a number, attach a flag.** The flag is real, machine-readable,
and honestly documented. It is also opt-in to *read*, defaulted to the fast and
less accurate mode, and it answers a different question than grain's `additive`
flag: Foundry's flag says *"the engine may have approximated"*, grain's says
*"the arithmetic you are about to do is invalid"*.

The refusals Foundry does have, collected — this is the complete set I could
document:

| Refusal | Enforced where | Grade |
|---|---|---|
| `.selectProperty()` across a many-cardinality link | OSDK type system, compile time | **[D]** |
| A derived property over a many link must name an aggregation | Ontology / builder UI | **[D]** |
| More than 3 search-around hops | Runtime failure | **[D]** |
| Duplicate primary key (OSv2) | Indexing / build failure | **[D]** |
| Duplicate primary key on create | `[Actions] ObjectsAlreadyExist` | **[D]** |
| Value-type constraint violation | Builder pipelines and the ontology | **[D]** |

Every one of these is structural or type-level. **None of them concerns whether
an aggregate is semantically meaningful**, and none names a legal alternative in
grain's sense — an error that resolves. **[I]**

**[V]** Palantir's AI framing claims the Ontology constrains model output by
construction. The mechanism it describes is the same one grain relies on — a
typed object/link/action surface rather than free SQL
([Reducing hallucinations with the Ontology in Palantir AIP](https://blog.palantir.com/reducing-hallucinations-with-the-ontology-in-palantir-aip-288552477383);
returned HTTP 403 to automated fetch, so this is cited from its title and search
snippet only, and is graded accordingly). Whatever its merits, it constrains
*which objects and properties* a model may name. It does not constrain what
arithmetic the model may then perform on them, because §4 and §5 established
there is nothing in the Ontology that could.

---

## 9. One thing I could not settle

**[U]** A user reported an OSDK `groupBy` + `count` returning 158 (the row count)
where 1 was expected for a field with a single distinct value, resolved by
switching to `exactDistinct`, with the closing remark "When you group and count
you don't expect Foundry to do an unwind"
([community thread](https://community.palantir.com/t/group-by-and-count-in-the-osdk-is-not-functioning-correctly/6049)).
The root cause is not established in the thread — array-property unwinding and a
simple misreading of count-of-objects versus count-of-distinct-values are both
consistent with it. I record it because it is the only public report I found of
Foundry aggregation semantics surprising a user in a counting direction, and
because leaving it out would be selective. It is not evidence of fan-out.

---

## 10. The comparison

| Problem | grain | Foundry Ontology |
|---|---|---|
| **Fan-out on a join path** | Solved twice: pre-aggregate-and-rejoin, and symmetric aggregates. Verified against an independent oracle. **2328.60, not 5738.28.** | **Does not arise on the object-set path** — no row exists to replicate **[I]**. **Arises in full on Ontology SQL** **[D]**. **Arises, inferred, by copying a one-side value to a finer grain and summing** **[I]**. **Arises in the pipelines that build object types**, one layer below the Ontology **[I]**. |
| **Traversal-shaped queries** | `traverse` path + `group_by` + metric; engine picks inline / pre-aggregate / refuse from declared cardinality | No counterpart. Aggregation is over one object type's set; cross-grain measures must be pre-decided as derived properties, capped at 3 hops **[D]** |
| **Cardinality declared** | Yes, on every link and object join; required, no default | Yes, four cases, direction-explicit **[D]** |
| **Cardinality verified** | **Yes** — against the database's own primary keys, unique constraints and unique indexes, at load, before any query runs | **No.** "not enforced"; "indicates to applications" **[D]** |
| **Uniqueness declared and verified** | Declared `unique` on properties, verified against real keys; required in the `group_by` of a non-additive query | Primary key only. Verified **continuously** on OSv2 (indexing failure); silently arbitrary on OSv1 **[D]** |
| **A metric concept** | Yes: named, grained, typed, with `ai_context` | **None** **[D]** |
| **Quantity / summarizability typing** | `extensive \| rate \| ratio` on a property (Lenz & Shoshani's *flow* and *value-per-unit*; `stock` unimplemented); `sum` over a non-accumulating quantity refused at load | **None** **[D]** |
| **Semi-additive / stock** | Cannot express it; refuses to pretend | Rich machinery — `Last`, time-weighted average, integral, cross-series sum — and **no typing that restricts which applies** **[D]** |
| **Time dimension** | Absent (the open frontier) | Event capability, time series properties, units, interpolation **[D]**. Point-in-time *object* queries: **[U]** |
| **Composition typing** (`QUANTITY-TYPES.md` §5a) | Absent, identified as the frontier | Absent, and further back than dbt MetricFlow — no metric object to type **[D]** |
| **Refusal posture** | Refuse rather than guess; every error names an alternative that itself resolves | Best-effort with an accuracy flag; `PREFER_SPEED` by default on interactive surfaces **[D]**. Six structural refusals **[D]**, none semantic **[I]** |
| **Static cardinality-driven refusal** | Grain analysis at resolve time | **Yes, and in a compiler** — the OSDK cardinality flag. Narrower than grain's, but stricter within its scope **[D]** |

---

## 11. Honest assessment

**Where Foundry is ahead, and it is not a small thing.** Its object-set query
model *dissolves* the fan-out problem for the path most users take, rather than
correcting it. grain built two engines, an oracle, a differential harness and a
sweep to be sure it undoes row multiplicity correctly; Foundry arranged for row
multiplicity not to be representable. That is the better answer where it applies,
and grain never considered it — the assumption grain has carried is that a
relational engine is the substrate, and Foundry shows the assumption is a choice.
Its OSDK cardinality flag is also the only static, compiler-enforced,
cardinality-driven refusal I have found in any system, and its OSv2 primary-key
check is the continuous-verification pattern grain has designed and not built.
On the `stock` case, Foundry has all the reduction primitives grain lacks.

**Where Foundry is behind, and it is the whole of grain's thesis.** It has no
metric, so it has nothing to attach semantics to; no summarizability typing, so
nothing refuses a meaningless total; no verified cardinality, so its one real
refusal reads a declaration nobody checked; and no refusal posture — its default
interactive mode is approximate-and-flag. Its own modelling guidance never raises
grain or aggregation correctness as a concern. The correctness it does deliver is
*structural* — you may not name what does not exist, you may not select where
there is no single value — never *semantic*. And its escape hatch, Ontology SQL,
hands back every problem the object model removed.

**Has Foundry solved anything grain has not?** Yes, one thing, and it is
architectural rather than conceptual: **the fan-out problem, by eliminating the
row from the query surface** — for the object-set path only, and at the cost of
being unable to express grain's traversal queries at all. It has also
demonstrated continuous verification of a declared key in production, which grain
has designed and deferred. Everything else in grain's problem statement —
declared-and-verified cardinality, a typed metric, quantity typing, composition
typing, refusal that names a resolving alternative — is absent from Foundry's
public record. On the frontier `QUANTITY-TYPES.md` identifies, Foundry is not a
competitor; it is one further step back than dbt MetricFlow, because it lacks the
object a type would attach to.

**What this should change in grain.** Nothing in the implementation. One thing in
the design notes: grain has been treating "the substrate is relational, so
fan-out is inevitable and must be corrected" as a premise. It is a choice, and a
system with a set-valued query surface pays a different price. grain's price —
`traverse`, and two engines to make it safe — buys expressiveness Foundry does
not have. That trade is now measured rather than assumed.

---

## Sources

All accessed 2026-09-06.

**Ontology model**
- [Core concepts](https://www.palantir.com/docs/foundry/ontology/core-concepts) · [Ontology overview](https://www.palantir.com/docs/foundry/ontology/overview) · [The Ontology system](https://www.palantir.com/docs/foundry/architecture-center/ontology-system)
- [Create an object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type) · [Link types overview](https://www.palantir.com/docs/foundry/object-link-types/link-types-overview) · [Create a link type](https://www.palantir.com/docs/foundry/object-link-types/create-link-type) · [Link type metadata](https://www.palantir.com/docs/foundry/object-link-types/link-type-metadata)
- [Value types](https://www.palantir.com/docs/foundry/object-link-types/value-types-overview) · [Value type constraints](https://www.palantir.com/docs/foundry/object-link-types/value-type-constraints) · [Property reducers](https://www.palantir.com/docs/foundry/object-link-types/property-reducers)
- [Interface link types](https://www.palantir.com/docs/foundry/interfaces/interface-link-types-overview)

**Aggregation and query**
- [Aggregation considerations](https://www.palantir.com/docs/foundry/object-backend/aggregation-considerations)
- [Object backend overview](https://www.palantir.com/docs/foundry/object-backend/overview) · [Ontology query compute](https://www.palantir.com/docs/foundry/ontologies/query-compute-usage)
- [Object sets (Functions)](https://www.palantir.com/docs/foundry/functions/api-object-sets) · [Objects and links (Functions)](https://www.palantir.com/docs/foundry/functions/api-objects-links) · [Object identifiers](https://www.palantir.com/docs/foundry/functions/object-identifiers)
- [Aggregate objects API](https://www.palantir.com/docs/foundry/api/ontology-resources/objects/aggregate-objects)
- [Derived properties (Ontology)](https://www.palantir.com/docs/foundry/ontology/derived-properties) · [Derived properties (reference)](https://www.palantir.com/docs/foundry/object-link-types/derived-properties) · [Derived properties (Workshop)](https://www.palantir.com/docs/foundry/workshop/derived-properties) · [OSDK advanced queries](https://palantir.github.io/osdk-ts/react/advanced-queries/)
- [Ontology SQL](https://www.palantir.com/docs/foundry/sql-warehousing/ontology-sql) · [SQL in Foundry](https://www.palantir.com/docs/foundry/sql-warehousing/overview)

**Time**
- [Time series overview](https://www.palantir.com/docs/foundry/time-series/time-series-overview) · [Time series properties](https://www.palantir.com/docs/foundry/time-series/time-series-properties) · [Sensor object type setup](https://www.palantir.com/docs/foundry/time-series/create-sensor-ot)
- [Time series aggregations (Quiver)](https://www.palantir.com/docs/foundry/quiver/timeseries-aggregations) · [Linked series aggregation](https://www.palantir.com/docs/foundry/quiver/card-linked-series-aggregation)
- [Events (Map)](https://www.palantir.com/docs/foundry/map/events)

**Design guidance**
- [Ontology design: best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices) · [Best practices and anti-patterns](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices-and-anti-patterns)

**Community and vendor**
- [How do I make sure my primary keys are unique?](https://community.palantir.com/t/how-do-i-make-sure-my-primary-keys-are-unique/307)
- [Group by and count in the OSDK](https://community.palantir.com/t/group-by-and-count-in-the-osdk-is-not-functioning-correctly/6049)
- [Reducing hallucinations with the Ontology in Palantir AIP](https://blog.palantir.com/reducing-hallucinations-with-the-ontology-in-palantir-aip-288552477383) — **[V]**, 403 to automated fetch; cited from title/snippet only
- [AIP observability metrics](https://www.palantir.com/docs/foundry/aip-observability/metrics) · [MetricSet reference (deprecated)](https://www.palantir.com/docs/foundry/evaluate-models/metric-sets-reference)
