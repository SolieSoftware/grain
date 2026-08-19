# grain

A declarative ontology layer over relational data — agents query objects, links and
grain-aware metrics, never raw SQL.

**Status: in development.** 14 of 16 planned tasks complete. The engine answers
grain-correct queries over the Chinook sample database. The comparison that would
justify it — against raw text-to-SQL — has **not been run yet**.

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
`Playlist` returns 10 correct groups summing to 5738.28 against a true 2328.60 —
every number right, the total meaningless. It flags rather than refuses, because
the per-group numbers are what the question asked for.

## Layers

| Layer | Contents | Per-domain? |
|---|---|---|
| Physical | PostgreSQL + generated SQLAlchemy models | Yes |
| Logical | `ontology.yaml` — object types, links, metrics | Yes |
| Engine | resolve → grain analysis → compile → guard → execute | **No** |
| Adapters | library · CLI · MCP | No |

**The architecture test:** pointing grain at a second database must not require
editing anything under `engine/`. Adding the complete Chinook pack — 10 objects,
9 links, 5 metrics — required zero engine changes. A genuinely different database
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
uv run pytest -q              # 123 passing
```

Unit tests need no database. Integration tests skip without `GRAIN_DATABASE_URL`,
so a bare `pytest` run reporting green may have tested nothing — check the skip
summary.

The database is Chinook v1.4.5, loaded from `Chinook_PostgreSql_SerialPKs.sql`
(the snake_case variant; the default port uses quoted CamelCase).

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
