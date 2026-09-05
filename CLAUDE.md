# grain — working notes

A declarative ontology layer over relational data. Agents send a typed
`QuerySpec`; the engine decides how to compute it without double-counting. The
whole project exists because a fan-out join returns a plausible wrong number,
and nothing about the output looks wrong.

Read `README.md` first for what it does. This file is about how to work on it.

## Run the tests correctly — the URL is load-bearing

```bash
export GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook"
uv run pytest -q          # 391 passing
uv run ruff check src tests tools
```

Three outcomes, two of them misleading:

| `GRAIN_DATABASE_URL` | result |
|---|---|
| unset | `270 passed, 121 skipped` — **green, and never touched a database** |
| `postgresql://…` | `4 failed, 271 passed, 116 errors` — SQLAlchemy reaches for psycopg2, not a dependency |
| `postgresql+psycopg://…` | **391 passed** — the only form that runs the measured tests |

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

## Verifying a change

In order of strength:

1. **`tools/oracle.py`** — computes answers in pure Python from raw rows,
   sharing no SQL with either engine. This is the only check that cannot inherit
   a misconception from the code it is checking. Use it whenever you touch
   aggregation.
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
and meaningless. A property now declares `quantity: extensive | rate | ratio`
and the loader refuses a `sum` over one that does not accumulate.

The rule is deliberately narrow: it inspects a summed value only when that value
is a BARE column. `sum(a * b)` is left alone, because a rate times a count IS
extensive — `revenue` is exactly that shape, and a cruder rule would refuse it.
Opaque `expr` metrics are skipped entirely; nothing can tell whether they sum.

Still open: **semi-additive** quantities (a balance, an inventory level) that
sum across accounts but not across time. dbt's MetricFlow models this with
`non_additive_dimension`; grain has no time-dimension concept, so this needs a
subsystem rather than a field.

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

## Conventions

- Python 3.12+, SQLAlchemy 2.x (`select()` style only), Pydantic v2, psycopg 3.
- Line length 100. `ruff check src tests tools` must pass.
- `engine/` never imports from `domains/` or an adapter. Adapters (CLI, MCP,
  agent) may import the engine, never the reverse.
- Every failure is raised **before a connection is acquired**, except
  `GuardTripped`.
- Plans and designs live in `docs/plans/` (dated), not `docs/superpowers/`.
- Comments explain *why*, especially why an obvious simpler thing is wrong. The
  codebase is dense with these on purpose — they are the record of what was
  already tried and found broken.

## Git

Remote is **GitHub** (`SolieSoftware/grain`) over SSH, and this repo commits
**directly to `main`** — that is the established workflow here, not an accident.
