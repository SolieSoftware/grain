# grain — working notes

A declarative ontology layer over relational data. Agents send a typed
`QuerySpec`; the engine decides how to compute it without double-counting. The
whole project exists because a fan-out join returns a plausible wrong number,
and nothing about the output looks wrong.

Read `README.md` first for what it does. This file is about how to work on it.

## Run the tests correctly — the URL is load-bearing

```bash
export GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook"
uv run pytest -q          # 570 passing, 0 skipped
uv run ruff check src tests tools
```

**One table is not shipped with chinook.** A `stock` needs a level column and
chinook has none, so `uv run python tools/seed_inventory.py` creates and seeds
`daily_inventory` — re-runnable, and opt-in because a domain pack must never
produce schema as a side effect of being loaded. Its declarations therefore
cannot live in the chinook pack either (the loader refuses an ontology naming a
missing table, so chinook would stop loading for anyone who had not seeded);
they are in `src/grain/domains/chinook_inventory/`. Unseeded, 31 tests skip and
the rest pass — a supported state, and also the state in which nothing
independently checks stock. Seed it.

Three outcomes, two of them misleading:

| `GRAIN_DATABASE_URL` | result |
|---|---|
| unset | `391 passed, 179 skipped` — **green, and never touched a database** |
| `postgresql://…` | `4 failed, 392 passed, 174 errors` — SQLAlchemy reaches for psycopg2, not a dependency |
| `postgresql+psycopg://…` | **570 passed** — the only form that runs the measured tests |

The unset case is the trap. Most regression tests here assert *measured values*
against chinook; skipped, they assert nothing. **Check the skip count, not the
colour.**

**Verify with the command the docs give, not a convenient variant.** I ran
`python -m pytest` throughout a session while documenting `uv run pytest`. The
first puts the working directory on `sys.path` and the second does not, so
`tests/corpus.py` imported fine for me and failed at collection for everyone
else. `pythonpath = ["."]` in `pyproject.toml` fixes it; the lesson is that a
green run proves the command you ran, not the command you wrote down.

## The rule that matters most

**A wrong number is worse than no answer.** Every design decision in this repo
comes from that. Concretely:

- **Refuse rather than guess.** Every error names a legal alternative, and that
  alternative must itself resolve. `NonAdditiveRefused` naming the unique keys
  to group by is the pattern.
- **Enforce, don't assume.** Cardinality, uniqueness and nullability are
  *declared* in the ontology and *verified against the database* by the loader.
  A declaration nothing checks is a silent assumption with a field name on it.
- **Never infer from data.** Uniqueness is read from the declaration, not
  sampled from rows — a fact inferred from today's data becomes wrong the first
  time a duplicate arrives.
- **`SAWarning` is an error** (`pyproject.toml`). A SQLAlchemy cartesian-product
  warning is a wrong answer announcing itself. Do not weaken this.

## Two engines

Both answer the same `QuerySpec` over the same ontology; they share *only the
loaded ontology*.

- **`subquery`** (default) — pre-aggregates a fanned metric at its own grain and
  `LEFT JOIN`s it back. `src/grain/engine/`.
- **`symmetric`** — one pass, `SUM(DISTINCT k*K + v) - SUM(DISTINCT k*K)`.
  `src/grain/engine_symmetric/`.

**The symmetric engine carries TWO encodings, and their guarantees differ.** The
sum encoding pairs `K = 1e30` with `BOUND = 5e29` — a limit on a VALUE, which
nothing in the schema constrains, so it is only a load-time check and later
writes can violate it silently. The array encoding used for `median` and
`percentile` uses `KEY_OFFSET = 1e19`, which bounds a KEY: a bigint cannot
exceed 9.22e18, so the key's own type guarantees it. A proof, not a measurement.
Do not conflate the two when reasoning about drift.

`src/grain/plan.py` is the seam. `EnginePlan` is the entire contract; the facade
must never touch an engine's own types.

**`engine_symmetric/resolve.py` is a deliberate copy of `engine/resolve.py`.**
Not an accident, and not to be deduplicated: a shared resolver would make the
differential harness blind to resolver bugs, because both engines would inherit
the bug and agree. `tests/unit/test_resolver_parity.py` makes drift visible. A
fix to one is not a fix to the other — this is a known, accepted cost.

**The symmetric engine is a specialist, not a superset.** It has no
`aggregate_then_join`. A metric it cannot serve is refused with a pointer to the
other engine, never silently served another way — an engine that quietly
switched strategy would make the differential harness meaningless.

**It now refuses a `stock` as well as an opaque `expr`.** Windowing to a
boundary instant needs a window function read from the WHERE of the select that
computes it, which requires a subquery, which is the one thing that engine does
not do. So `inventory_level` sits in the corpus's `DIVERGENT` set rather than
its agreeing one, and (2) below cannot see that path at all: **for a stock the
oracle is not the strongest independent check, it is the only one.** A change to
the windowing that the oracle does not cover is a change nothing covers.

## Verifying a change

In order of strength:

1. **`tools/oracle.py`** — computes answers in pure Python from raw rows,
   sharing no SQL with either engine. This is the only check that cannot inherit
   a misconception from the code it is checking. Use it whenever you touch
   aggregation. For a `stock` it is not the strongest check, it is the ONLY one:
   the symmetric engine refuses the shape, so (2) below is blind to it.
   Extending it costs an entry in `METRICS` and, if the grain table's key is
   composite, a tuple in `PK` — registering one column of a composite key does
   not fail, it silently answers a smaller question.
2. **`tests/integration/test_engine_agreement.py`** — both engines, same corpus,
   identical rows asserted.
3. **The measured anchors** (`test_measured_anchors.py`, `test_defect_anchors.py`,
   `test_symmetric_anchors.py`) — hand-verified figures. `revenue` is `2328.60`;
   the naive fanned join gives `5738.28`.

**Do not hand-write comparison SQL casually.** I introduced a fan-out bug in a
comparison query *while explaining fan-out bugs* — `album → track →
invoice_line` fans the track rows, and `sum(track.unit_price)` came back
inflated. That is exactly defect C5. Prefer the oracle.

## Known limitations — pinned, not forgotten

`tests/integration/test_shared_limits.py` proves what neither engine can do. If
you think you have fixed one, you must delete a test, not just assert it.

**Fixed, and the test was deleted rather than inverted.** grain used to validate
a metric's *grain* — that its rows are not replicated — with no concept of
whether the *quantity* accumulated, so `sum(track.unit_price)` came back perfect
and meaningless. A **metric** now declares `quantity: flow | stock |
value_per_unit` and the loader refuses a `sum` over one that does not
accumulate. It lives on the metric because headcount — the textbook `stock` — is
`count_distinct(employee.employee_id)`, which reads no quantity column at all:
`employee_id` is an identifier. Stock-ness is a fact about what the result means.
`Property.quantity` stays, because it is still the right place to say what a
*column* is, and a silent metric summing a bare column inherits it, so the
common case needs one word in one place.
Where both speak, the metric wins, silently; refusing the disagreement would be
the more conservative reading of this codebase's own rules and is recorded as
debatable rather than settled.

The rule is deliberately narrow: it inspects a summed value only when that value
is a BARE column. `sum(a * b)` is left alone, because a value-per-unit times a
count IS a flow — `revenue` is exactly that shape, and a cruder rule would refuse it.
Opaque `expr` metrics are skipped entirely; nothing can tell whether they sum.

**Semi-additive quantities are no longer open — for one instant.** A level
(`quantity: stock`) declares `over_time: {dimension, choice: first|last}`, and
the subquery engine windows to that instant before aggregating, so it sums
across accounts and never across time. A **grouped** level is reported
`additive: false`: the window partitions by the query's own group keys, so each
group holds its own boundary instant and their total is a level at no instant.
That verdict is computed beside the window rather than derived from it — the
one known case is fixed and the general problem is the first item in
`docs/BACKLOG.md`. dbt's MetricFlow spells the same thing
`non_additive_dimension`; the vocabulary here is `first|last` rather than
`min|max` because `window_choice: max` reads as the largest VALUE when it means
the value at the latest DATE.

Three things that leaves standing. **The symmetric engine refuses a stock** —
the window needs a window function inside a subquery and that engine is one
pass — so the differential harness cannot see this path at all and
`tools/oracle.py` is the only independent judge of it. **No period other than
"now" is expressible**: `FilterScalar` has no date member, so nothing can
restrict the population to March before windowing (pinned in
`test_shared_limits.py`). And `time_grain` is verified against the column but
its granularity meaning is still unused.

Also standing: the symmetric encoding's `|v| < 5e29` bound is checked only at
load, so data written later can cross it silently. This is the design's weakest
point; a self-enforcing SQL guard is designed and held in reserve.

## The agent

`src/grain/agent/` — `grain-chat`. The model's only channel into the engine is
one tool whose `input_schema` **is** `QuerySpec.model_json_schema()`, so the
contract it is held to and the contract the engine enforces cannot drift. It
cannot write SQL and there is no tool that accepts it.

`claude-opus-5`, adaptive thinking (`budget_tokens` is rejected on this model).
The `anthropic` SDK is an optional `[agent]` extra, imported lazily — the
library, CLI and MCP paths must keep working without it, and a test pins that
the engine never imports upward.

**It has never made a real API call.** No credential was available on the
machine it was built on. Treat first use as the real test.

## What this taught

`docs/FINDINGS.md` records the difficulties and their resolutions — written
while the work was happening, not reconstructed afterwards. Read it before
modelling a new dataset: the shapes recur under different names, and several
entries are mistakes made twice before being understood once.

Add to it as you go. An entry is: what was believed, what turned out to be true,
what it cost.

## Before extending the quantity model

`docs/QUANTITY-TYPES.md` is the prior art for metric quantity typing — Kennedy's
units-of-measure type system for the composition half, Lenz & Shoshani's
summarizability conditions for the aggregation half. Read it before adding to
`quantity`.

Two things it established, one of which is now spent. grain's field used to read
`extensive | rate | ratio`, which mapped onto the established **flow / stock /
value-per-unit** with `stock` missing — the semi-additive case I had recorded as
needing a subsystem, and which the framework said was a third value of one
field. That reading was right: `quantity` now reads
`flow | stock | value_per_unit`, and the whole of the stock case is that value
plus `time_grain` on a property and `over_time` on a metric. The rename was
breaking with **no alias path**, pinned by
`tests/unit/test_quantity_kind.py`, so the docs are the only migration guide
there is — an ontology written from the old names does not load.

What is still open is the other half: `revenue = sum(unit_price * quantity)` — a
value-per-unit times a flow — is a special case in the loader that a composition
algebra would make a derivation. §5a of that document is where the surveyed
tools all stop, and it is the next phase.

## Conventions

- Python 3.12+, SQLAlchemy 2.x (`select()` style only), Pydantic v2, psycopg 3.
- Line length 100. `ruff check src tests tools` must pass.
- `engine/` never imports from `domains/` or an adapter. Adapters (CLI, MCP,
  agent) may import the engine, never the reverse.
- Every failure is raised **before a connection is acquired**, except
  `GuardTripped` — and, since the time work, a **date-valued filter**.
  `FilterScalar` has no date member, so the ISO-string workaround compiles to
  `invoice_date < $1::VARCHAR`, for which Postgres has no operator: that
  failure is a raw `ProgrammingError` from the driver, not a `GrainError` at
  all. Pinned in `tests/integration/test_shared_limits.py`. Named here because
  an undocumented exception is how a stated invariant stops being trusted. It
  goes away when `FilterScalar` gains a date member — a change to the agent's
  tool schema, and so a decision of its own.
- Plans and designs live in `docs/plans/` (dated), not `docs/superpowers/`.
- Comments explain *why*, especially why an obvious simpler thing is wrong. The
  codebase is dense with these on purpose — they are the record of what was
  already tried and found broken.

## Git

Remote is **GitHub** (`SolieSoftware/grain`) over SSH, and this repo commits
**directly to `main`** — that is the established workflow here, not an accident.
