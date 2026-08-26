# grain

A declarative ontology layer over relational data — agents query objects, links and
grain-aware metrics, never raw SQL.

**Status: in development.** All 16 planned tasks are complete and **241 tests
pass**. The **five critical defects** a whole-branch review found on 2026-08-18
were fixed on 2026-08-24, each with a measured regression test at the facade —
see *"Defects found and fixed"* below. **I3** (recursive traversal) is fixed too,
by building the qualified-group-key mechanism it needed.

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
| Engine | resolve → grain analysis → compile → guard → execute | **No** |
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
src/grain/engine/     ontology · loader · spec · resolve · grain · compile · guard · api
src/grain/domains/chinook/   models.py (generated) · ontology.yaml
tests/unit/           compile-to-SQL, no database
tests/integration/    against a loaded chinook, anchored on measured values
docs/architecture.html       the design, with diagrams
docs/plans/                  the implementation plan this was built from
```

## Running it

```bash
uv venv && uv pip install -e ".[dev,mcp]"
cp .env.example .env          # then set GRAIN_DATABASE_URL
set -a && . ./.env && set +a
uv run pytest -q              # 241 passing
uv run ruff check src tests   # clean
```

Unit tests need no database. Integration tests skip without `GRAIN_DATABASE_URL`,
so a bare `pytest` run reporting green may have tested nothing — check the skip
summary (`190 passed, 51 skipped` is the no-database baseline).

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

## What is not done

- **No second domain.** The reuse claim above is untested against a real alternative.
- **No golden set and no ablation.** Whether this beats raw text-to-SQL on a fixed
  question set is the project's actual claim, and it is unmeasured.
- **No writeback**, no inference, no entity resolution, no caching. Read-only.
- **Metric selection is a known limit.** The engine guarantees the answer is
  *computed* correctly. It cannot guarantee the *right question was asked* — if two
  metrics both sound like "revenue", only prose in `ai_context` distinguishes them.

## Design notes

The full design, its corrections, and a postmortem of the mistakes made building it
live outside this repo, in the author's notes under
`Projects/Ontology Engine/grain/`.
