# grain

A declarative ontology layer over relational data — agents query objects, links and
grain-aware metrics, never raw SQL.

**Status: in development.** All 16 planned tasks are complete and **391 tests
pass**. The **five critical defects** a whole-branch review found on 2026-08-18
were fixed on 2026-08-24, each with a measured regression test at the facade —
see *"Defects found and fixed"* below. **I3** (recursive traversal) is fixed too,
by building the qualified-group-key mechanism it needed.

There are now **two selectable engines** (see *"Two engines"*), a **chat agent**
that emits validated `QuerySpec`s rather than SQL (see *"Chatting to it"*), and
an **independent oracle** the engines are checked against (see *"Evaluation"*).

The comparison that would justify the project — against raw text-to-SQL — has
still **not been run**, and there is still only one domain pack.

---

## The problem

A metric only means something at the grain of the table it aggregates. Join that
table to anything with higher cardinality and its rows are replicated, so the sum
silently over-counts.

Measured on the database this repo talks to:

| Query | Result |
|---|---|
| `sum(invoice.total)` at invoice grain | **2328.60** |
| `sum(unit_price × quantity)` at line grain | **2328.60** |
| `sum(invoice.total)` joined through `invoice_line` | **20848.62** |

The third is what an agent writes when it has a schema dump and no notion of grain.
It is **8.95× overstated, with no error** — right magnitude, right sign, right type.
Nothing downstream can detect it.

**Grain is not in the schema.** A foreign key records *that* two tables relate,
never *what one row means*, and cardinality is not recoverable from a constraint.
So the ontology holds the one fact the database never states.

## How it works

The agent never writes SQL and never writes Python. It emits a typed `QuerySpec`
whose every field is a name drawn from the ontology, and the engine decides whether
that request can be answered correctly.

```python
from grain.engine.api import Grain
from grain.engine.spec import QuerySpec, Hop

g = Grain.load(CHINOOK_DIR, engine)

result = g.query(QuerySpec(
    object="Customer",
    group_by=["country"],
    metrics=["revenue"],
    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
))

result.rows          # 24 groups, totalling 2328.60
result.compiled_sql  # provenance — the exact SQL that ran
result.rewrites      # what the engine changed, and which edge forced it
result.additive      # False when the groups overlap and must not be summed
```

Three outcomes per metric, decided from **declared cardinality alone** with no data
access:

- **inline** — no fanning edge downstream of the metric's grain
- **aggregate-then-join** — compute at the metric's own grain in a subquery, join back
- **refuse** — no correct query exists; the error names the legal alternative

Plus an orthogonal **`additive`** flag: a query can be correctly computed *and*
non-additive, when the path to a dimension crosses a many-to-many. `revenue` by
`Playlist` returns 12 groups summing to 5738.28 against a true 2328.60 — the total
is meaningless by construction, so it flags rather than refuses, because the
per-group numbers are what the question asked for.

That per-group guarantee has one condition, and the engine now **enforces** it
rather than asserting it: a group must be one row of the root object. So the
non-additive query above is legal only when `group_by` includes a property the
ontology declares `unique` (`Playlist.id`), and is **refused** when it groups by
`name` — which two Chinook playlists share — or by nothing at all. Grouped by
identity, playlists 1 and 8 are both 2107.71, each exactly the distinct-track
revenue it holds. Grouped by name they were one group of 4215.42. See **C1** and
**C2**.

## Layers

| Layer | Contents | Per-domain? |
|---|---|---|
| Physical | PostgreSQL + generated SQLAlchemy models | Yes |
| Logical | `ontology.yaml` — object types, links, metrics | Yes |
| Engine | resolve → grain analysis → compile, per engine | **No** |
| Adapters | library · CLI · MCP | No |

### What the ontology must declare

Two of these are load-bearing rather than descriptive, and the loader checks both
against the database's own primary keys, unique constraints and unique indexes —
a declaration nothing verifies is a silent assumption with a field name attached.

| Declaration | Where | Rule |
|---|---|---|
| `cardinality` | every **link** | Decides inline / rewrite / refuse |
| `cardinality` | every **object join** | **Required, no default.** Must be non-fanning, and the `to` side must be a key the database enforces. A fanning object join is refused: model it as a link |
| `unique` | a **property** | Declares that it identifies one row of its object. Required in the `group_by` of any non-additive query, and what lets a fanning hop be answered inline |
| `max_depth` | a **recursive link** | How far a traversal walks the chain. `1` on a hop means the immediate parent only |

**The architecture test:** pointing grain at a second database must not require
editing anything under `engine/`. Adding the complete Chinook pack — 10 objects,
9 links, 6 metrics — required zero engine changes. A genuinely different database
has not been tried yet.

## Layout

```
src/grain/plan.py            the engine seam: EnginePlan + the registry
src/grain/engine/            ontology · loader · spec · resolve · grain · compile · guard · api
src/grain/engine_symmetric/  the second engine: its own resolve · grain · compile · symmetric
src/grain/agent/             the chat agent: session · tools · prompt · cli
src/grain/domains/chinook/   models.py (generated) · ontology.yaml
tests/unit/                  compile-to-SQL, no database
tests/integration/           against a loaded chinook, anchored on measured values
tools/                       the oracle and the evaluation harnesses (not the test suite)
docs/architecture.html       the design, with diagrams
docs/plans/                  the plans and designs this was built from
```

## Running it

```bash
uv venv && uv pip install -e ".[dev,mcp]"
cp .env.example .env          # then set GRAIN_DATABASE_URL — see below
set -a && . ./.env && set +a
uv run pytest -q              # 391 passing
uv run ruff check src tests   # clean
```

### The connection URL is load-bearing

Unit tests need no database. Integration tests — every test that measures a real
number against chinook — need `GRAIN_DATABASE_URL`, and two of the three ways the
run can end report something other than success:

| `GRAIN_DATABASE_URL` | `pytest -q` |
|---|---|
| unset | `270 passed, 121 skipped` |
| `postgresql://user@localhost/chinook` | `4 failed, 271 passed, 116 errors` |
| `postgresql+psycopg://user@localhost:5432/chinook` | `391 passed` |

Only the third form runs the measured integration tests:

```bash
export GRAIN_DATABASE_URL="postgresql+psycopg://user@localhost:5432/chinook"
```

The `+psycopg` suffix is what selects the psycopg 3 dialect. Given a bare
`postgresql://` scheme, SQLAlchemy reaches for psycopg2 instead — which this project
does not depend on, since it depends on `psycopg[binary]>=3.1` — and every
integration test then errors at fixture setup with `ModuleNotFoundError: No module
named 'psycopg2'`.

The unset case is the one to watch, because it reports green. **A run that skipped
121 tests is the exact failure mode this branch exists to prevent:** four of the five
criticals below returned plausible wrong numbers, so every regression test for them
asserts a measured value against the database. Skipped, they assert nothing. Check
the skip count, not the colour.

`pyproject.toml` promotes `SAWarning` to an error. That is not tidiness: a
SQLAlchemy cartesian-product warning is a *wrong answer* announcing itself, and
defect C3 shipped four such specs because nothing turned the warning into a
failure.

From a standing start on macOS, the database is:

```bash
brew install postgresql@17 && brew services start postgresql@17
curl -sSLO https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_PostgreSql_SerialPKs.sql
createdb chinook
grep -v -E '^(DROP DATABASE|CREATE DATABASE|\\c chinook_serial)' \
  Chinook_PostgreSql_SerialPKs.sql | psql -q -v ON_ERROR_STOP=1 -d chinook
```

The database is Chinook v1.4.5, loaded from `Chinook_PostgreSql_SerialPKs.sql`
(the snake_case variant; the default port uses quoted CamelCase).

## Defects found and fixed

A whole-branch review on 2026-08-18 found five criticals; all were demonstrated
with measured numbers and all were fixed on 2026-08-24. The pattern is worth
keeping: **the engine defended the *grain* axis rigorously and the *population*
axis — which rows, which groups, which entity — not at all.** Four of the five
produced correct-looking SQL, so every regression test for them asserts a number
at the facade rather than inspecting a statement.

| | Defect | Was | Now |
|---|---|---|---|
| **C1** | Non-additive metric with **no `group_by`** | 5738.28 as one unlabelled figure, `additive: false` attached, truth 2328.60 | `NonAdditiveRefused`, naming `group_by Playlist.id` |
| **C2** | Per-group correctness assumed one group = one root row | Two playlists named `Music` merged into one group of 4215.42 for 2107.71 of distinct-track revenue, while `describe()` published "each group is still correct" as fact | A non-additive query must group by a property declared `unique`, checked against the database's own keys; the rule now states its own condition |
| **C3** | Dotted filter through an *object join* | Cartesian product: 347 of 347 albums returned for a Jazz filter matching 13, a Rock-only album among them. Four shipped specs hit it | EXISTS is built from the link's target *object* and the `via` join applied inside it. 13 = 13, and `-W error::SAWarning` makes a recurrence a test failure |
| **C4** | Metric-expression guard case-sensitive, SQL identifiers not | `sum(INVOICE.TOTAL)` at `invoice_line` grain matched neither regex, loaded clean, returned **20848.62** | Case-folded comparison, and any token no classifier recognised is an error rather than a pass. Window-frame keywords also accepted, so a legal windowed metric loads |
| **C5** | `ObjectType.joins` declared **no cardinality** | Two fanning object joins returned `sum(thing.weight)` = 6 for a truth of 3, reported `additive: true` with no rewrites | `cardinality` is required on every `TableJoin`, verified against the database's keys; a *fanning* object join is refused at load, naming links as the alternative |

Of the important items: **I1** (`order_by` accepted and never read — "top 10 by
revenue" returned 10 arbitrary countries, omitting USA, the largest) is
implemented, with an unhonourable key now a typed error and a `limit_reached`
flag on the result. **I2** (a dotted filter changes the *population*, not the
rows) is intended behaviour that was documented nowhere; it is now published to
the agent as a rule in `describe()`. **I4** (the AST boundary test missed
`from X import Y` and every relative-import form) and **I5** (two CLI tests
failing rather than skipping without a database) are fixed.

**I3, recursive traversal, is fixed by building the feature it needed.** A
`traverse` hop over a recursive link used to join each row to *its own* CTE row:
it added no column a caller could name — `resolve()` built `group_by` from the
root object only — and its only observable effect was silently dropping rows not
reachable from a root. Three parts, described below: qualified group keys, an
ancestor-shaped CTE, and a per-position alias environment in the compiler.

## Traversing a hierarchy

```python
g.query(QuerySpec(
    object="Employee",
    group_by=["Employee_Manager.id", "Employee_Manager.last_name"],
    metrics=["employee_count"],
    traverse=[Hop(link="Employee_Manager")],
    order_by=[OrderBy(key="employee_count", desc=True)],
))
# (1, 'Adams', 7), (2, 'Edwards', 3), (6, 'Mitchell', 2)   additive=False
```

Checked against hand-written recursive SQL. Three things make it work:

- **A group key may be qualified by a traversed link.** `Employee_Manager.last_name`
  is a property of the object that hop lands on, not of the queried object. The
  link must be traversed, and must appear once — a qualified key names a link, not
  a hop.
- **A recursive traversal walks the closure, so its effective cardinality is
  `many_to_many`** whatever its one-hop `cardinality` declares. A row has many
  ancestors and an ancestor has many rows beneath it, so the query is non-additive
  and needs a `unique` group key — the same machinery C2 added. `Hop(max_depth=1)`
  means the immediate parent, which is additive and sums to 7.
- **A fanning edge pinned by a unique key at its own position needs no rewrite.**
  Its copies land in distinct groups, so within any group the metric's row appears
  exactly once. This is the mirror of `revenue` by `Playlist`, which the engine
  already answered that way: correct per group, meaningless as a total.

Without a pinning key the same query is **refused** (`KeyBeyondGrain`): a
pre-aggregate has to carry every group key, and walking far enough to reach one
past a fanning edge would replicate the metric's rows inside the subquery built to
prevent exactly that. Those refusals are the "our mechanism cannot express this"
class the symmetric-aggregates decision is waiting to have counted.

Deliberately narrow: the key must sit at the fanning edge's *own* position. A key
further along determines the row at that edge only when every edge in between is
functional in reverse — `Playlist → Playlist_Tracks (m2m) → Track_Album (m2o)`
grouped by album is the counterexample, since one album does not determine which
track, so a playlist holding two tracks from one album would appear twice in that
album's group. The wider rule is a non-goal until something measures that it is
needed.

## Two engines

Both answer the same `QuerySpec` over the same ontology, and differ only in how
they keep a fanned join from double-counting.

| | `subquery` (default) | `symmetric` |
|---|---|---|
| Method | pre-aggregate the metric at its own grain, `LEFT JOIN` it back | one pass, `SUM(DISTINCT k*K + v) - SUM(DISTINCT k*K)` |
| Metric forms | `expr` and `agg`/`value` | `agg`/`value` only |
| Key beyond the grain | refused (`KeyBeyondGrain`) | answered |
| Non-unique group key over many-to-many | refused (`NonAdditiveRefused`) | answered |
| Speed | faster for a single metric | unestablished — see below |

```bash
grain query --engine symmetric --spec '{"object":"Playlist", ...}'
```

```python
Grain.load(domain_dir, engine, engine_name="symmetric")
```

The symmetric engine is a **specialist, not a superset**: it implements only the
aggregate taxonomy, and refuses a metric it cannot serve (opaque `expr`, an
inexact type, or a grain table without a single-column integer primary key)
rather than quietly falling back. An engine that answered via a different
strategy than the one asked for would make the differential harness meaningless.

**What it fixes.** Fan-out replication — one grain row counted several times
*inside* one group. Through `Playlist → Track → InvoiceLine` the naive sum
reports 5738.28 where the true total is 2328.60; the symmetric engine returns
2328.60 exactly. It also answers two shapes the subquery engine has to refuse,
because the encoding dedupes by the grain's primary key: defect **C2**'s
non-unique group key (the two playlists named *Music* return 2107.71 each,
correctly) and a group key reached across a fan.

**What it does not fix.** Overlapping groups — one grain row legitimately
belonging to *several* groups. Revenue by playlist still sums to 5738.28 across
groups against a true 2328.60, because a track sits on many playlists. That
double-counting is the question, not the SQL, so it stays `additive: false`
under both engines. Looker has the same limitation.

**Why not Looker's version.** Looker hashes the key and `FLOOR`-scales the value
into a fixed-width `NUMERIC(38,0)`, buying portability across MySQL, Redshift
and BigQuery at the cost of two silent failure modes: hash collisions drop a
row's value, and truncation loses digits. grain is Postgres-only, where `numeric`
is arbitrary-precision, so it uses the real integer key unscaled — no collisions,
no truncation.

**Speed is not a reason to switch.** chinook is too small to time: the same
query measured 15.8 ms and 23.2 ms on separate runs. The one gap large enough to
trust has the symmetric engine **4× slower** on a single-metric query (17.9 ms
against 4.2 ms), because `SUM(DISTINCT ...)` sorts the whole fanned row set.
Choose it for what it can answer, not for how fast.

### What neither engine can do

`tests/integration/test_shared_limits.py` pins the limitations, so a later claim
to have fixed one has to delete a test rather than merely assert. Each is a hard
limit in the BI literature, measured here against chinook.

| Limitation | What happens |
|---|---|
| ~~A quantity that was never additive~~ | **Fixed.** A property declares `quantity: extensive \| rate \| ratio`, and the loader refuses a `sum` over one that does not accumulate. Only bare-column values are inspected, so `sum(price * qty)` stays legal. |
| **Semi-additive quantities** | A balance sums across accounts but not across time. grain has no time-dimension concept, so it cannot express the constraint. |
| **Overlapping groups** | Revenue by playlist sums to 5738.28 against a true 2328.60. Both flag `additive: false`; neither can give a correct total. |
| **Branching traversal** | `traverse` is a path, not a tree, so the chasm trap is inexpressible. Both refuse — with an error that describes a chain, not the branch that was asked for. |
| **Medians and percentiles** | No distinct-sum rewrite exists, so `AggFunc` offers none. As an opaque `expr` the subquery engine answers; the symmetric engine refuses. |
| **`limit` without `order_by`** | Arbitrary rows, legally. The two engines may return *different* rows for one spec — the only place their output may differ without either being wrong. |

grain validates a metric's **grain** — that its rows are not replicated — and
now also whether the **quantity** accumulates at all. That second check is a
declaration, not an aggregate technique: no amount of clever SQL can tell you a
price should not be totalled, so the ontology has to say it and the loader has
to enforce it.

### The engines check each other

`tests/integration/test_engine_agreement.py` runs a shared corpus through both
and asserts identical rows. Because they share nothing below the loaded ontology
— each owns its own resolver — a disagreement is a real defect in one of them,
found without anyone hand-computing the expected figure. It caught a type leak
on its first run: `units_sold` declares `integer`, and the encoding was returning
`Decimal`.

## Chatting to it

`grain chat` puts an agent in front of the engine. It answers questions in plain
English by emitting a `QuerySpec` — never SQL.

```bash
uv pip install -e ".[agent]"
export ANTHROPIC_API_KEY=...
grain-chat --engine symmetric --show-spec
```

**The model has no way to write SQL, and that is the design.** Its only channel
into the engine is one tool whose input schema *is*
`QuerySpec.model_json_schema()` — the same object the engine validates against,
so the contract the model is held to cannot drift from the contract that is
enforced. Three layers stand behind it: strict tool use constrains the
generation, Pydantic validates it, and `resolve`/`analyse` refuse anything that
names something undeclared or has no correct answer. A bad generation's worst
case is a typed error at the door, not a plausible wrong number.

The system prompt is `describe()` — grain's own output, not the DDL and not a
hand-written summary that could drift from it. A refused query is fed back with
its `alternatives` and the agent retries; grain's errors were built to name a
legal next move, which is exactly what makes them usable as repair instructions.
The budget is three attempts, after which the agent explains rather than
retries.

Caveats travel with the data, attached by code rather than left to the prompt: a
non-additive result carries an explicit instruction not to total it, and a
truncated one says so. A model that summed a non-additive column would undo the
point of the engine, and an instruction alone is not a strong enough guarantee.

**Its weak point is metric selection, and it is the same weak point the engine
has.** The engine guarantees the number is computed correctly; nothing
guarantees the agent picked the metric the person meant. Where two metrics both
sound like "revenue", only `ai_context` prose distinguishes them. Design notes:
[`docs/plans/2026-08-28-query-agent-design.md`](docs/plans/2026-08-28-query-agent-design.md).

## Evaluation

Two engines agreeing proves only that they agree. [`tools/oracle.py`](tools/oracle.py)
computes each answer in **pure Python from raw rows**, sharing no SQL with either
engine, so it cannot inherit a misconception from the code it checks. Everything
below is measured, not asserted.

**Correctness.** [`tools/sweep.py`](tools/sweep.py) enumerates every (root, path,
group key, metric) combination and checks all three answers:

| | |
|---|---|
| 61 combinations | **56 both correct · 5 symmetric-only · 0 wrong · 0 regressions** |

All five divergences are one shape — a non-unique group key over a many-to-many.
The subquery engine refuses it; hand-writing the `aggregate_then_join` it would
have emitted confirms the refusal is justified, not over-cautious (`4215.42`
against a true `2107.71`).

**Performance.** chinook is too small to time — the same query measured 15.8 ms
and 23.2 ms on separate runs — so [`tools/bench.py`](tools/bench.py) benchmarks
both SQL shapes on synthetic data:

| grain rows | joined rows | symmetric | pre-aggregate | ratio |
|---|---|---|---|---|
| 1,000 | 5,000 | 6.1 ms | 1.5 ms | 4.06× |
| 100,000 | 500,000 | 320 ms | 68 ms | 4.68× |
| 1,000,000 | 5,000,000 | 3,508 ms | 668 ms | **5.25×** |

The ratio *widens* with scale, so this is not overhead that amortises away.
Looker's docs give the mechanism: a symmetric sum costs "on the order of a
`COUNT(DISTINCT)`". **Choose the symmetric engine for what it can answer, never
for speed.**

**Against Looker.** [`tools/stress.py`](tools/stress.py) compares grain's
encoding with Looker's shape. grain is exact where Looker's `FLOOR`-scaling
loses up to `1.8×10⁻⁶`. The magnitude advantage grain's design claims is **not
demonstrated** — the reimplementation did not overflow at any magnitude tested
up to 10²⁸.

## What is not done

- **No second domain.** The reuse claim above is untested against a real alternative.
- **No golden set and no ablation.** Whether this beats raw text-to-SQL on a fixed
  question set is the project's actual claim, and it is unmeasured.
- **No writeback**, no inference, no entity resolution, no caching. Read-only.
- **The symmetric encoding's bound is only checked at load.** It needs
  `|v| < 5e29`; the loader measures the observed maximum, but rows written
  afterwards can cross it and condition (b) then fails silently. A
  self-enforcing SQL guard is designed and held in reserve.
- **The two resolvers are a copy.** `engine_symmetric/resolve.py` duplicates
  `engine/resolve.py` deliberately, so a resolution bug shows up as disagreement
  rather than being inherited by both. A fix to one is not a fix to the other;
  `test_resolver_parity` makes drift visible but cannot prevent it.
- **The agent has never been run against the live API.** It is unit-tested
  against a scripted client — the loop, the repair budget, the caveat handling
  and the schema are all covered — but no credential was available on the
  machine it was built on, so the request has not once been accepted by
  Anthropic. Treat first use as the real test.
- **No evaluation set for the agent.** Nothing measures how often it picks the
  right metric, which is the thing most likely to be wrong.
- **Metric selection is a known limit.** The engine guarantees the answer is
  *computed* correctly. It cannot guarantee the *right question was asked* — if two
  metrics both sound like "revenue", only prose in `ai_context` distinguishes them.

## What this taught

[`docs/FINDINGS.md`](docs/FINDINGS.md) — the difficulties and their resolutions,
written as the work happened. Verification traps, modelling gaps, encoding
trade-offs, and the places an elegant invariant turned out not to be the
load-bearing one. Written to transfer to other datasets rather than to describe
this one.

## Design notes

The full design, its corrections, and a postmortem of the mistakes made building it
live outside this repo, in the author's notes under
`Projects/Ontology Engine/grain/`.
