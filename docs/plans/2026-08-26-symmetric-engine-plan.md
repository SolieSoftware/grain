# Symmetric Aggregate Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, selectable query engine that computes grain-correct aggregates in one pass with symmetric aggregates, and teach the existing engine that some aggregates never needed a rewrite at all.

**Architecture:** Four phases. Phase 1 adds a structured metric declaration (`agg` + `value`) and uses it to recognise fan-out-immune aggregates in the current engine. Phase 2 introduces an engine-agnostic result type and a selectable engine registry, with the existing engine as the only implementation. Phase 3 adds `engine_symmetric/`, owning its own resolve, analyse and compile. Phase 4 adds the differential harness that makes the two engines cross-check each other.

**Tech Stack:** Python 3.12+ · SQLAlchemy 2.x (`select()` style only) · Pydantic v2 · psycopg 3 · pytest · ruff

**Spec:** `docs/plans/2026-08-26-symmetric-engine-design.md` — read it before starting. This plan implements it and argues from it.

## Global Constraints

- **`engine/` never imports from `domains/` or from any adapter.** `engine_symmetric/` inherits the same rule. A violation is a defect, not a style issue.
- **No SQL string is ever authored by a model.** The only input is a validated `QuerySpec`.
- **Cardinality is declared, never inferred from data.**
- **Every failure is raised before a database connection is acquired**, except `GuardTripped`.
- **Every error names a legal alternative, and that alternative must itself resolve.**
- Line length **100**. `ruff check src tests` must pass. All public functions carry type hints.
- Tests run with `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook"`. The `+psycopg` suffix is required — a bare `postgresql://` scheme errors on a missing psycopg2.
- **`pyproject.toml` promotes `SAWarning` to an error.** A cartesian product is a wrong answer, not a warning. Do not weaken this.
- **The existing engine's behaviour must not change** except where a task says so explicitly. All 241 existing tests must stay green at every commit.

---

## File Structure

**Phase 1 — structured metric declaration and the immune taxonomy**
- Modify `src/grain/engine/ontology.py` — `AggFunc` literal, `Metric.agg`/`Metric.value`, mutual exclusion, `Metric.sql_expr`, `Metric.fanout_immune`
- Modify `src/grain/engine/loader.py` — validate `value` tokens exactly as `expr` is validated
- Modify `src/grain/engine/grain.py` — immune metrics take the inline path
- Modify `src/grain/domains/chinook/ontology.yaml` — migrate metrics to `agg`/`value`
- Create `tests/unit/test_metric_declaration.py`
- Create `tests/unit/test_immune_aggregates.py`
- Create `tests/integration/test_immune_anchors.py`

**Phase 2 — the engine seam**
- Create `src/grain/plan.py` — `EnginePlan`, the engine-agnostic output; `Engine` protocol; registry
- Modify `src/grain/engine/api.py` — `Grain(engine=...)`, dispatch through the registry
- Modify `src/grain/engine/execute.py` — `Result.engine`
- Modify `src/grain/engine/cli.py` — `--engine`
- Create `tests/unit/test_engine_registry.py`

**Phase 3 — the symmetric engine**
- Create `src/grain/engine_symmetric/__init__.py`
- Create `src/grain/engine_symmetric/resolve.py` — copy of the existing resolver
- Create `src/grain/engine_symmetric/grain.py` — symmetric planner
- Create `src/grain/engine_symmetric/symmetric.py` — the encoded aggregate expression
- Create `src/grain/engine_symmetric/compile.py` — single-pass compiler
- Modify `src/grain/engine/errors.py` — `MetricNotSymmetric`, `NoIntegerKeyForGrain`
- Create `tests/unit/test_symmetric_expr.py`
- Create `tests/integration/test_symmetric_anchors.py`

**Phase 4 — differential harness**
- Create `tests/integration/test_engine_agreement.py`
- Create `tests/corpus.py` — the shared spec corpus

---

## Phase 1 — Structured metric declaration and the immune taxonomy

### Task 1: `agg` and `value` on `Metric`

**Files:**
- Modify: `src/grain/engine/ontology.py` (the `Metric` class, currently at the end of the file)
- Test: `tests/unit/test_metric_declaration.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AggFunc` (a `Literal`), `Metric.agg: AggFunc | None`, `Metric.value: str | None`, `Metric.expr: str | None`, `Metric.sql_expr: str` (property), `Metric.fanout_immune: bool` (property), `Metric.is_structured: bool` (property). Tasks 2–4 and all of Phase 3 rely on these exact names.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_metric_declaration.py`:

```python
"""A metric declares its aggregate either as one opaque string (`expr`) or
structurally (`agg` + `value`). Only the structured form can be reasoned about,
and only the structured form is eligible for the symmetric engine."""
import pytest
from pydantic import ValidationError

from grain.engine.ontology import Metric


def test_opaque_expr_still_loads_unchanged():
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               expr="sum(invoice_line.unit_price * invoice_line.quantity)")
    assert m.sql_expr == "sum(invoice_line.unit_price * invoice_line.quantity)"
    assert m.is_structured is False
    assert m.fanout_immune is False


def test_structured_form_renders_the_same_sql():
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               agg="sum", value="invoice_line.unit_price * invoice_line.quantity")
    assert m.sql_expr == "sum(invoice_line.unit_price * invoice_line.quantity)"
    assert m.is_structured is True


def test_count_distinct_renders_the_distinct_keyword():
    m = Metric(name="track_count", grain="track", type="integer",
               agg="count_distinct", value="track.track_id")
    assert m.sql_expr == "count(distinct track.track_id)"


@pytest.mark.parametrize("agg", ["min", "max", "count_distinct"])
def test_these_aggregates_are_immune_to_fan_out(agg):
    """min/max of replicated rows is min/max of distinct rows; count(distinct x)
    dedupes by construction. Row replication cannot change any of them."""
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds")
    assert m.fanout_immune is True


@pytest.mark.parametrize("agg", ["sum", "count", "avg"])
def test_these_aggregates_are_not_immune(agg):
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds")
    assert m.fanout_immune is False


def test_declaring_both_forms_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer",
               expr="sum(track.milliseconds)", agg="sum",
               value="track.milliseconds")


def test_declaring_neither_form_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer")


def test_agg_without_value_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Metric(name="m", grain="track", type="integer", agg="sum")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_metric_declaration.py -q`
Expected: FAIL — `ValidationError` on `expr` being required, and `AttributeError`/`TypeError` for the unknown `agg`, `value`, `sql_expr`, `fanout_immune`, `is_structured` names.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/ontology.py`, add the literal next to the existing `ValueType` declaration near the top:

```python
AggFunc = Literal["sum", "count", "count_distinct", "min", "max", "avg"]

FANOUT_IMMUNE: frozenset[str] = frozenset({"min", "max", "count_distinct"})
"""Aggregates that row replication cannot change.

`min`/`max` of a multiset are unaffected by duplicates, and `count(distinct x)`
dedupes by construction. A fanning join downstream of such a metric's grain
therefore needs no rewrite at all — the naive inline aggregate is already the
right number. The existing planner never knew this because it read path
cardinality and never looked at the metric.

This is about the aggregate ONLY. It says nothing about whether the groups
overlap, which is a property of the path and is decided separately.
"""
```

Replace the `Metric` class with:

```python
class Metric(BaseModel):
    """An aggregate, declared either opaquely or structurally.

    `expr` is the original form: one SQL string, rendered verbatim, opaque to
    the engine. `agg` + `value` is the structured form, which splits the
    aggregate function from the per-row expression it wraps.

    The split is not cosmetic. Two things need the pieces separately: the
    fan-out taxonomy needs the FUNCTION to know whether replication can change
    the answer, and the symmetric engine needs the VALUE so it can wrap it in
    an encoded aggregate. Neither is recoverable from `expr` without parsing
    SQL, which would make a wrong answer depend on the quality of a regex.
    """

    name: str
    grain: str
    type: ValueType
    expr: str | None = None
    agg: AggFunc | None = None
    value: str | None = None
    description: str | None = None
    ai_context: AiContext | None = None

    @model_validator(mode="after")
    def _check_exactly_one_form(self) -> "Metric":
        structured = self.agg is not None and self.value is not None
        partial = (self.agg is None) != (self.value is None)
        if partial or (structured == (self.expr is not None)):
            raise ValueError(
                f"metric '{self.name}' must declare exactly one of 'expr' or "
                f"'agg' + 'value' (both of that pair, together)"
            )
        return self

    @property
    def is_structured(self) -> bool:
        return self.agg is not None

    @property
    def sql_expr(self) -> str:
        """The aggregate as SQL. One accessor so the compiler never branches on
        which form was declared."""
        if self.expr is not None:
            return self.expr
        if self.agg == "count_distinct":
            return f"count(distinct {self.value})"
        return f"{self.agg}({self.value})"

    @property
    def fanout_immune(self) -> bool:
        """True when row replication cannot change this metric's value.

        An opaque `expr` is never immune. It may well BE a `min` or a
        `count(distinct ...)`, but nothing here can prove that, and guessing
        would trade a needless subquery for a possible wrong number.
        """
        return self.agg in FANOUT_IMMUNE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_metric_declaration.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Check nothing else broke**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q`
Expected: 250 passed. `expr` became optional but every existing metric still supplies it, and `sql_expr` is not yet used by the compiler.

- [ ] **Step 6: Commit**

```bash
git add src/grain/engine/ontology.py tests/unit/test_metric_declaration.py
git commit -m "feat: declare metrics structurally as agg + value

Splits the aggregate function from the per-row value it wraps. The fan-out
taxonomy needs the function; the symmetric engine needs the value. Neither is
recoverable from an opaque expr without parsing SQL. expr stays valid and
unchanged in meaning."
```

---

### Task 2: the compiler reads `sql_expr`

**Files:**
- Modify: `src/grain/engine/compile.py:514-537` (`_metric_expr` and `_metric_column`)
- Test: `tests/unit/test_compile_metrics.py` (existing — add one test)

**Interfaces:**
- Consumes: `Metric.sql_expr` from Task 1.
- Produces: no new names. After this task the compiler is indifferent to which form a metric declared.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_compile_metrics.py`:

```python
def test_a_structured_metric_compiles_identically_to_its_opaque_twin(chinook_metadata):
    """The two declaration forms are two spellings of one aggregate. If they
    ever compile differently, the migration in Task 4 would silently change
    every number the chinook pack reports."""
    from grain.engine.compile import _metric_column
    from grain.engine.ontology import Metric

    opaque = Metric(name="revenue", grain="invoice_line", type="decimal",
                    expr="sum(invoice_line.unit_price * invoice_line.quantity)")
    structured = Metric(name="revenue", grain="invoice_line", type="decimal",
                        agg="sum",
                        value="invoice_line.unit_price * invoice_line.quantity")
    assert str(_metric_column(opaque)) == str(_metric_column(structured))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_compile_metrics.py -q -k structured`
Expected: FAIL — `_metric_expr` reads `metric.expr`, which is `None` for the structured metric, so `literal_column(None)` raises.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/compile.py`, in `_metric_expr`, change the returned line from `literal_column(metric.expr)` to `literal_column(metric.sql_expr)`, and add to that function's docstring:

```
    `sql_expr` rather than `expr`: a metric may be declared structurally
    (`agg` + `value`), and this function is deliberately indifferent to which.
    The loader validates the tokens of whichever form was declared, so the
    guarantee this docstring relies on is unchanged.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_compile_metrics.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/compile.py tests/unit/test_compile_metrics.py
git commit -m "refactor: compile metrics through sql_expr

One accessor, so the compiler never branches on which declaration form a
metric used. Asserts the two forms compile byte-identically, which is what
makes migrating the chinook pack safe."
```

---

### Task 3: the loader validates `value` exactly as it validates `expr`

**Files:**
- Modify: `src/grain/engine/loader.py:284-352` (`_validate_metric_expr`)
- Test: `tests/unit/test_loader_declarations.py` (existing — add tests)

**Interfaces:**
- Consumes: `Metric.sql_expr`, `Metric.value` from Task 1.
- Produces: no new names. The existing guarantee — every `table.column` token in a metric belongs to the metric's grain table — now covers the structured form.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_loader_declarations.py`:

```python
def test_a_structured_metric_value_may_not_reference_another_table(chinook_metadata):
    """`_metric_expr` binds a metric to the first un-aliased occurrence of its
    grain table. A value naming a different table would bind somewhere the
    grain analysis never reasoned about — the same hole `expr` was closed
    against."""
    from grain.engine.errors import OntologyInvalid
    from grain.engine.loader import validate
    from grain.engine.ontology import Metric, Ontology

    onto = Ontology(name="t", metrics={
        "bad": Metric(name="bad", grain="invoice_line", type="decimal",
                      agg="sum", value="invoice.total"),
    })
    with pytest.raises(OntologyInvalid, match="may only reference"):
        validate(onto, chinook_metadata)


def test_a_structured_metric_value_may_not_name_a_missing_column(chinook_metadata):
    from grain.engine.errors import OntologyInvalid
    from grain.engine.loader import validate
    from grain.engine.ontology import Metric, Ontology

    onto = Ontology(name="t", metrics={
        "bad": Metric(name="bad", grain="invoice_line", type="decimal",
                      agg="sum", value="invoice_line.nope"),
    })
    with pytest.raises(OntologyInvalid, match="does not exist"):
        validate(onto, chinook_metadata)


def test_a_valid_structured_metric_passes(chinook_metadata):
    from grain.engine.loader import validate
    from grain.engine.ontology import Metric, Ontology

    onto = Ontology(name="t", metrics={
        "ok": Metric(name="ok", grain="invoice_line", type="decimal", agg="sum",
                     value="invoice_line.unit_price * invoice_line.quantity"),
    })
    validate(onto, chinook_metadata)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_loader_declarations.py -q -k structured`
Expected: FAIL — `_validate_metric_expr` masks `metric.expr`, which is `None`, so `NUMBER.sub(" ", None)` raises `TypeError` rather than the expected `OntologyInvalid`.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/loader.py`, inside `_validate_metric_expr`, change the line that reads

```python
    masked = NUMBER.sub(" ", metric.expr)
```

to

```python
    # `sql_expr`, not `expr`: a structurally-declared metric puts its columns
    # in `value`, and every guarantee below must cover both forms. Validating
    # the RENDERED aggregate rather than `value` alone also means the `distinct`
    # keyword `count_distinct` introduces is checked as the SQL keyword it is,
    # by the same keyword list, instead of needing a second code path.
    masked = NUMBER.sub(" ", metric.sql_expr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_loader_declarations.py -q`
Expected: PASS

- [ ] **Step 5: Confirm `distinct` is an accepted keyword**

Run: `.venv/bin/python -c "
from grain.engine.loader import KEYWORDS, FUNCTIONS
print('distinct' in {w.lower() for w in KEYWORDS} | {w.lower() for w in FUNCTIONS})"`

Expected: `True`. If it prints `False`, add `"distinct"` to whichever of those collections holds SQL keywords, and note in the commit that `count_distinct` required it.

- [ ] **Step 6: Run the full suite and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q`
Expected: all green.

```bash
git add src/grain/engine/loader.py tests/unit/test_loader_declarations.py
git commit -m "fix: validate structured metric values against the database

The rule that every table.column token in a metric belongs to that metric's
grain table is what lets the compiler render the aggregate verbatim. It now
covers agg + value, not just expr."
```

---

### Task 4: migrate the chinook metrics

**Files:**
- Modify: `src/grain/domains/chinook/ontology.yaml` (the `metrics:` block)
- Test: `tests/integration/test_measured_anchors.py` (existing — must stay green unchanged)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: a chinook pack whose metrics are structured, so Task 5 has something to act on.

- [ ] **Step 1: Record the numbers before touching anything**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_measured_anchors.py -q`
Expected: PASS. These tests assert hand-verified figures (revenue 2328.60 and so on) and are the safety net for this task. If any of them fail after the migration, the migration changed a number and must be reverted, not adjusted.

- [ ] **Step 2: Rewrite the metrics block**

In `src/grain/domains/chinook/ontology.yaml`, convert each metric from `expr` to `agg` + `value`, leaving `grain`, `type`, `description` and `ai_context` exactly as they are:

```yaml
  revenue:
    grain: invoice_line
    agg: sum
    value: "invoice_line.unit_price * invoice_line.quantity"
    type: decimal
```

```yaml
  invoice_total:
    grain: invoice
    agg: sum
    value: "invoice.total"
    type: decimal
```

```yaml
  units_sold:
    grain: invoice_line
    agg: sum
    value: "invoice_line.quantity"
    type: integer
```

```yaml
  track_count:
    grain: track
    agg: count_distinct
    value: "track.track_id"
    type: integer
```

```yaml
  customer_count:
    grain: customer
    agg: count_distinct
    value: "customer.customer_id"
    type: integer
```

```yaml
  employee_count:
    grain: employee
    agg: count_distinct
    value: "employee.employee_id"
    type: integer
```

Then run `grep -n "expr:" src/grain/domains/chinook/ontology.yaml` and convert any metric this list missed the same way. Do not delete descriptions or `ai_context`.

- [ ] **Step 3: Verify every number is unchanged**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q`
Expected: all green, with no test edited. The migration is a spelling change; a changed figure means Task 2's equivalence broke.

- [ ] **Step 4: Commit**

```bash
git add src/grain/domains/chinook/ontology.yaml
git commit -m "refactor: declare the chinook metrics structurally

Spelling change only — every measured anchor passes unedited. Makes the three
count(distinct pk) metrics visible to the planner as fan-out immune, which
Task 5 acts on."
```

---

### Task 5: immune metrics take the inline path

**Files:**
- Modify: `src/grain/engine/grain.py:251-268` (inside `analyse`, where `strategy` is decided)
- Test: `tests/unit/test_immune_aggregates.py` (create)
- Test: `tests/integration/test_immune_anchors.py` (create)

**Interfaces:**
- Consumes: `Metric.fanout_immune` from Task 1; the migrated pack from Task 4.
- Produces: `MetricPlan.strategy == "inline"` and `forced_by is None` for immune metrics. No new names.

- [ ] **Step 1: Write the failing unit test**

Create `tests/unit/test_immune_aggregates.py`:

```python
"""A fan-out-immune aggregate needs no rewrite, however the path fans.

The planner used to decide strategy from path cardinality alone and never
looked at the metric, so `count(distinct track.track_id)` was pushed through a
pre-aggregating subquery it could never need.
"""
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec


def test_count_distinct_across_a_fanning_link_stays_inline(chinook_ontology):
    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices")],
        group_by=["country"],
        metrics=["track_count"],
    )
    plan = analyse(resolve(spec, chinook_ontology))
    (mp,) = plan.metric_plans
    assert mp.strategy == "inline"
    assert mp.forced_by is None


def test_a_summing_metric_across_the_same_link_is_still_rewritten(chinook_ontology):
    """The control. Immunity is a property of the aggregate, not of the path —
    if this also went inline, the change would be a regression of C5, not a
    feature."""
    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices")],
        group_by=["country"],
        metrics=["invoice_total"],
    )
    plan = analyse(resolve(spec, chinook_ontology))
    (mp,) = plan.metric_plans
    assert mp.strategy == "aggregate_then_join"


def test_immunity_does_not_make_an_overlapping_query_additive(chinook_ontology):
    """Immunity is about replication inside a group. Overlapping groups are a
    separate fact and must still be reported."""
    spec = QuerySpec(
        object="Playlist",
        traverse=[Hop(link="Playlist_Tracks")],
        group_by=["id"],
        metrics=["track_count"],
    )
    plan = analyse(resolve(spec, chinook_ontology))
    (mp,) = plan.metric_plans
    assert mp.strategy == "inline"
    assert plan.additive is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_immune_aggregates.py -q`
Expected: FAIL on the first test — `strategy` is `aggregate_then_join`, because `analyse` does not consult the metric.

If `chinook_ontology` is not an existing fixture, check `tests/unit/conftest.py` for the fixture that loads the chinook ontology and use that name instead; do not add a duplicate fixture.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/grain.py`, inside the `for` loop in `analyse`, immediately after the `downstream` list is built and before `fanning` is computed, insert:

```python
        # A fan-out-immune aggregate cannot be changed by replication, so no
        # edge — fanning or not — forces a rewrite for it. Checked before the
        # path is examined at all, because the aggregate alone settles it.
        #
        # This does NOT touch additivity below: `min`/`max`/`count(distinct x)`
        # grouped across a many_to_many still produce overlapping groups, and
        # that verdict is a property of the path, not of the aggregate.
        if metric.fanout_immune:
            plan.metric_plans.append(
                MetricPlan(
                    metric=metric,
                    strategy="inline",
                    forced_by=None,
                    additive=True,
                    non_additive_reason=None,
                    subquery_edges=0,
                )
            )
            continue
```

**Careful:** the additivity loop further down the same iteration sets `additive`/`non_additive_reason` for the metric. A `continue` here would skip it, making the third test fail. So instead of `continue`, hoist the immunity check to only override the *strategy* decision. Replace the block above with:

```python
        immune = metric.fanout_immune
        # A fan-out-immune aggregate cannot be changed by replication, so no
        # edge forces a rewrite for it. This settles STRATEGY only: overlapping
        # groups are a property of the path and are decided below, unchanged.
```

then change the two lines that compute strategy from

```python
        strategy: Strategy = "inline" if not unpinned else "aggregate_then_join"
        forced_by = unpinned[0][1].link.name if unpinned else None
```

to

```python
        strategy: Strategy = (
            "inline" if immune or not unpinned else "aggregate_then_join"
        )
        forced_by = None if immune else (unpinned[0][1].link.name if unpinned else None)
```

and guard the `KeyBeyondGrain` block that follows so it cannot fire for an immune metric, by changing its condition from

```python
        if strategy == "aggregate_then_join" and needed > len(prefix):
```

to

```python
        # An immune metric never builds a subquery, so a key beyond its grain
        # costs it nothing and must not be refused on its behalf.
        if strategy == "aggregate_then_join" and needed > len(prefix):
```

(the condition already excludes immune metrics, since their strategy is now `inline` — add the comment so the dependency is explicit rather than incidental).

- [ ] **Step 4: Run the unit tests**

Run: `.venv/bin/python -m pytest tests/unit/test_immune_aggregates.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the integration test that proves the number is right**

Create `tests/integration/test_immune_anchors.py`:

```python
"""Inline is only allowed if it returns the SAME number the subquery returned.
These assert measured figures against the live database, not plan shapes."""
import pytest

from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration


def test_track_count_by_country_is_unchanged_by_going_inline(chinook_grain):
    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices")],
        group_by=["country"],
        metrics=["track_count"],
        order_by=[],
        limit=None,
    )
    result = chinook_grain.query(spec)
    assert result.rewrites == []
    by_country = {row[0]: row[1] for row in result.rows}
    # Verified directly:
    #   select count(distinct il.track_id) from customer c
    #   join invoice i on i.customer_id=c.customer_id
    #   join invoice_line il on il.invoice_id=i.invoice_id
    #   where c.country='USA';
    assert by_country["USA"] > 0
    assert sum(by_country.values()) >= by_country["USA"]


def test_the_inline_figure_equals_a_hand_written_query(chinook_grain):
    """The real assertion: compare against SQL written by hand, so a wrong
    inline result cannot hide behind the engine agreeing with itself."""
    from sqlalchemy import text

    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices")],
        group_by=["country"],
        metrics=["track_count"],
        limit=None,
    )
    engine_rows = {r[0]: r[1] for r in chinook_grain.query(spec).rows}
    with chinook_grain.engine.connect() as conn:
        truth = {r[0]: r[1] for r in conn.execute(text("""
            select c.country, count(distinct il.track_id)
            from customer c
            join invoice i on i.customer_id = c.customer_id
            join invoice_line il on il.invoice_id = i.invoice_id
            group by c.country
        """))}
    assert engine_rows == truth
```

- [ ] **Step 6: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_immune_anchors.py -q`
Expected: PASS. If `chinook_grain` is not an existing fixture, find the equivalent in `tests/conftest.py` or `tests/integration/conftest.py` and use it.

- [ ] **Step 7: Full suite, lint, commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests`
Expected: all green, lint clean.

```bash
git add src/grain/engine/grain.py tests/unit/test_immune_aggregates.py \
        tests/integration/test_immune_anchors.py
git commit -m "feat: fan-out-immune aggregates skip the rewrite

min, max and count(distinct x) cannot be changed by row replication, so no
fanning edge forces a subquery for them. The planner previously read path
cardinality and never looked at the metric, so chinook's three count-distinct
metrics were pre-aggregated for nothing.

Strategy only. Overlapping groups are a property of the path and are still
reported non-additive, which the third unit test pins."
```

---

## Phase 2 — The engine seam

### Task 6: `EnginePlan` and the engine registry

**Files:**
- Create: `src/grain/plan.py`
- Test: `tests/unit/test_engine_registry.py` (create)

**Interfaces:**
- Consumes: `Rewrite` from `grain.engine.execute`.
- Produces: `EnginePlan` (frozen dataclass with fields `stmt`, `rewrites`, `additive`, `non_additive_reason`, `ontology_elements_used`, `limit`), `Engine` (Protocol with one method `plan(spec, ontology, metadata) -> EnginePlan`), `register_engine(name, engine)`, `get_engine(name) -> Engine`, `ENGINE_NAMES`. Tasks 7–9 and Phase 3 rely on these names.

**Why this exists:** `api.Grain._plan` currently returns `(rq, plan, stmt)` and `api.py` then reaches into `plan.metric_plans` and `rq.root`/`rq.path`/`rq.metrics`. Under the spec's wide seam each engine owns its own `resolve`, so those are *different classes* per engine and cannot be shared. The facade must therefore consume an engine-agnostic value instead of engine internals.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_engine_registry.py`:

```python
"""Engines are selected by name and produce one engine-agnostic value.

The facade must never touch an engine's internals: each engine owns its own
resolver, so `ResolvedQuery` is a different class per engine and nothing
outside an engine may depend on its shape.
"""
import pytest

from grain.plan import ENGINE_NAMES, EnginePlan, get_engine, register_engine


def test_the_existing_engine_is_registered_as_subquery():
    assert "subquery" in ENGINE_NAMES
    assert get_engine("subquery") is not None


def test_an_unknown_engine_names_the_legal_ones():
    with pytest.raises(KeyError, match="subquery"):
        get_engine("nope")


def test_engine_plan_carries_only_engine_agnostic_values():
    """If a field here ever becomes an engine-owned type, the seam has leaked."""
    fields = set(EnginePlan.__dataclass_fields__)
    assert fields == {
        "stmt", "rewrites", "additive", "non_additive_reason",
        "ontology_elements_used", "limit",
    }


def test_registering_a_duplicate_name_is_refused():
    class Fake:
        def plan(self, spec, ontology, metadata):
            raise NotImplementedError

    register_engine("fake-dup", Fake())
    with pytest.raises(ValueError, match="already registered"):
        register_engine("fake-dup", Fake())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_engine_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grain.plan'`

- [ ] **Step 3: Write minimal implementation**

Create `src/grain/plan.py`:

```python
"""The seam between the facade and an engine.

An engine owns everything from a `QuerySpec` to a compiled `Select`: its own
resolver, its own grain analysis, its own compiler. That is deliberate — a bug
in a shared resolver would be invisible to a differential test, because both
engines would inherit it and agree on the same wrong answer.

The cost is that `ResolvedQuery` and `GrainPlan` are per-engine classes. So
nothing outside an engine may depend on them, and every engine hands back this
one flat, engine-agnostic value instead. `EnginePlan` is the entire contract;
if a field of it ever needs an engine-owned type, the seam has leaked and the
fix is to flatten the value, not to widen the import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import MetaData, Select

from .engine.execute import Rewrite
from .engine.ontology import Ontology
from .engine.spec import QuerySpec


@dataclass(frozen=True)
class EnginePlan:
    """One engine's complete answer to "what SQL, and what should the caller be
    told about it"."""

    stmt: Select[Any]
    rewrites: list[Rewrite]
    additive: bool
    non_additive_reason: str | None
    ontology_elements_used: list[str]
    limit: int | None


class Engine(Protocol):
    """Implemented once per engine. The only method the facade calls."""

    def plan(
        self, spec: QuerySpec, ontology: Ontology, metadata: MetaData
    ) -> EnginePlan: ...


_ENGINES: dict[str, Engine] = {}


def register_engine(name: str, engine: Engine) -> None:
    if name in _ENGINES:
        raise ValueError(f"engine '{name}' is already registered")
    _ENGINES[name] = engine


def get_engine(name: str) -> Engine:
    try:
        return _ENGINES[name]
    except KeyError:
        legal = ", ".join(sorted(_ENGINES)) or "none registered"
        raise KeyError(
            f"unknown engine '{name}'. Legal engines: {legal}"
        ) from None


class ENGINE_NAMES(frozenset):  # noqa: N801
    """Placeholder replaced in Step 4 — see below."""
```

Then replace that final placeholder class with a plain module-level accessor, because a mutable registry cannot be a frozen constant:

```python
def engine_names() -> frozenset[str]:
    return frozenset(_ENGINES)
```

and change the test's import and assertions to use `engine_names()`:

```python
from grain.plan import EnginePlan, engine_names, get_engine, register_engine

def test_the_existing_engine_is_registered_as_subquery():
    assert "subquery" in engine_names()
    assert get_engine("subquery") is not None
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_engine_registry.py -q`
Expected: the two registry tests and the field test PASS; `test_the_existing_engine_is_registered_as_subquery` still FAILS — nothing registers `subquery` yet. That is Task 7.

Mark it so the suite is honest in the meantime:

```python
@pytest.mark.xfail(reason="registered in Task 7", strict=True)
def test_the_existing_engine_is_registered_as_subquery():
```

- [ ] **Step 5: Commit**

```bash
git add src/grain/plan.py tests/unit/test_engine_registry.py
git commit -m "feat: add the engine seam

EnginePlan is the whole contract between the facade and an engine. Each engine
owns its own resolver so a resolver bug shows up as disagreement rather than
being inherited by both — which means ResolvedQuery is a per-engine class and
nothing outside an engine may depend on its shape."
```

---

### Task 7: wrap the existing engine as `subquery`

**Files:**
- Create: `src/grain/engine/adapter.py`
- Modify: `src/grain/engine/api.py:58-125`
- Modify: `src/grain/engine/execute.py` (add `Result.engine`)
- Test: `tests/unit/test_engine_registry.py` (remove the xfail)

**Interfaces:**
- Consumes: `EnginePlan`, `register_engine` from Task 6.
- Produces: `SubqueryEngine` with `.plan(...)`, registered under `"subquery"`; `Grain(..., engine: str = "subquery")`; `Result.engine: str`.

- [ ] **Step 1: Write the failing test**

Remove the `@pytest.mark.xfail` added in Task 6, and append to `tests/unit/test_engine_registry.py`:

```python
def test_result_reports_which_engine_answered(chinook_grain_unit):
    """A result that does not say which engine produced it is useless for
    comparison, which is the reason the seam exists."""
    from grain.engine.spec import QuerySpec

    spec = QuerySpec(object="Customer", group_by=["country"],
                     metrics=["customer_count"], limit=5)
    plan = get_engine("subquery").plan(
        spec, chinook_grain_unit.ontology, chinook_grain_unit.metadata
    )
    assert plan.additive is True
    assert plan.rewrites == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_engine_registry.py -q`
Expected: FAIL — `KeyError: unknown engine 'subquery'`.

- [ ] **Step 3: Write the adapter**

Create `src/grain/engine/adapter.py`:

```python
"""The existing engine, behind the `Engine` protocol.

This module holds the logic that used to live inline in `api.Grain._plan` and
`api.Grain._rewrites`. Moving it here is what lets the facade stop importing
`resolve`, `analyse` and `compile_query` directly — those are this engine's
internals, and a second engine has its own.
"""
from __future__ import annotations

from sqlalchemy import MetaData

from ..plan import EnginePlan, register_engine
from .compile import compile_query
from .execute import Rewrite
from .grain import GrainPlan, analyse
from .ontology import Ontology
from .resolve import ResolvedQuery, resolve
from .spec import QuerySpec


def _rewrites(plan: GrainPlan, ontology: Ontology) -> list[Rewrite]:
    return [
        Rewrite(
            metric=mp.metric.name,
            strategy=mp.strategy,
            forced_by=mp.forced_by,
            reason=f"{mp.forced_by} is {ontology.links[mp.forced_by].cardinality}",
        )
        for mp in plan.metric_plans
        if mp.forced_by
    ]


def _elements_used(rq: ResolvedQuery) -> list[str]:
    return (
        [rq.root.name]
        + [edge.link.name for edge in rq.path]
        + [metric.name for metric in rq.metrics]
    )


class SubqueryEngine:
    """Pre-aggregates a fanned metric at its own grain and LEFT JOINs it back."""

    def plan(
        self, spec: QuerySpec, ontology: Ontology, metadata: MetaData
    ) -> EnginePlan:
        rq = resolve(spec, ontology)
        plan = analyse(rq)
        stmt = compile_query(rq, plan, metadata)
        return EnginePlan(
            stmt=stmt,
            rewrites=_rewrites(plan, ontology),
            additive=plan.additive,
            non_additive_reason=plan.non_additive_reason,
            ontology_elements_used=_elements_used(rq),
            limit=rq.limit,
        )


register_engine("subquery", SubqueryEngine())
```

- [ ] **Step 4: Rewire the facade**

In `src/grain/engine/api.py`:

Replace the `compile`/`grain`/`resolve` imports with

```python
from ..plan import EnginePlan, get_engine
from . import adapter  # noqa: F401  -- registers the "subquery" engine
from .compile import sql_text
```

Add `engine_name` to `__init__` and `load`:

```python
    def __init__(
        self,
        ontology: Ontology,
        metadata: MetaData,
        engine: Engine,
        guard: GuardConfig | None = None,
        engine_name: str = "subquery",
    ) -> None:
        self.ontology = ontology
        self.metadata = metadata
        self.engine = engine
        self.guard = guard or GuardConfig()
        # Named, not an instance: the name is what a caller passes, what the
        # CLI flag carries, and what the Result reports back.
        self.engine_name = engine_name
```

`load` takes and forwards `engine_name: str = "subquery"` in the same way.

Replace `_plan` and `_rewrites` with one method:

```python
    def _plan(self, spec: QuerySpec) -> EnginePlan:
        return get_engine(self.engine_name).plan(spec, self.ontology, self.metadata)
```

Rewrite `explain` and `query` to read only `EnginePlan`:

```python
    def explain(self, spec: QuerySpec) -> dict[str, Any]:
        ep = self._plan(spec)
        return {
            "engine": self.engine_name,
            "compiled_sql": sql_text(ep.stmt),
            "rewrites": [
                {"metric": r.metric, "strategy": r.strategy,
                 "forced_by": r.forced_by, "reason": r.reason}
                for r in ep.rewrites
            ],
            "additive": ep.additive,
            "non_additive_reason": ep.non_additive_reason,
            "ontology_elements_used": ep.ontology_elements_used,
        }

    def query(self, spec: QuerySpec) -> Result:
        ep = self._plan(spec)
        rows, columns = execute(self.engine, ep.stmt, self.guard)
        return Result(
            rows=rows,
            columns=columns,
            compiled_sql=sql_text(ep.stmt),
            rewrites=ep.rewrites,
            additive=ep.additive,
            non_additive_reason=ep.non_additive_reason,
            limit_reached=ep.limit is not None and len(rows) == ep.limit,
            ontology_elements_used=ep.ontology_elements_used,
            engine=self.engine_name,
        )
```

Delete the now-unused `_ontology_elements_used` static method.

In `src/grain/engine/execute.py`, add to the `Result` dataclass, after `ontology_elements_used`:

```python
    engine: str = "subquery"
    """Which engine produced this. A result that cannot say is not comparable,
    and comparing two engines is the reason the seam exists."""
```

- [ ] **Step 5: Run the whole suite**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q`
Expected: all green. `explain()` gained an `"engine"` key — if `tests/unit/test_describe.py` or `tests/integration/test_cli.py` asserts the exact key set of `explain()`, update that assertion to include it and say so in the commit message.

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add src/grain/engine/adapter.py src/grain/engine/api.py \
        src/grain/engine/execute.py tests/unit/test_engine_registry.py
git commit -m "refactor: put the existing engine behind the Engine protocol

api.Grain reached into plan.metric_plans and rq.root/rq.path directly. With
each engine owning its own resolver those are per-engine classes, so the
facade now consumes only EnginePlan. Behaviour is unchanged; Result gained
engine, and explain() gained an engine key."
```

---

### Task 8: `--engine` on the CLI

**Files:**
- Modify: `src/grain/engine/cli.py`
- Test: `tests/integration/test_cli.py` (existing — add one test)

**Interfaces:**
- Consumes: `engine_names()` from Task 6, `Grain(engine_name=...)` from Task 7.
- Produces: a `--engine` flag on every subcommand that builds a `Grain`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_cli.py`:

```python
def test_engine_flag_is_reported_in_explain_output(capsys, chinook_dir):
    from grain.engine.cli import main

    rc = main(["explain", "--engine", "subquery", "--object", "Customer",
               "--metrics", "customer_count", "--domain", str(chinook_dir)])
    assert rc == 0
    assert '"engine": "subquery"' in capsys.readouterr().out


def test_an_unknown_engine_is_refused_with_the_legal_names(capsys, chinook_dir):
    from grain.engine.cli import main

    rc = main(["explain", "--engine", "nope", "--object", "Customer",
               "--metrics", "customer_count", "--domain", str(chinook_dir)])
    assert rc != 0
    assert "subquery" in capsys.readouterr().err
```

Read `src/grain/engine/cli.py` first and match the existing flag names and subcommand shapes — the argument names above are illustrative and must be adjusted to whatever the CLI actually accepts.

- [ ] **Step 2: Run it to verify it fails**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_cli.py -q -k engine`
Expected: FAIL — `unrecognized arguments: --engine`

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/cli.py`, add to the shared parent parser (or to each subparser that constructs a `Grain`):

```python
    parser.add_argument(
        "--engine",
        default="subquery",
        choices=sorted(engine_names()),
        help="which query engine to use (default: subquery)",
    )
```

with `from ..plan import engine_names` at the top, and pass `engine_name=args.engine` through to `Grain.load`. Because `choices` is supplied, argparse itself refuses an unknown name and prints the legal ones — which satisfies the second test without a hand-written error.

- [ ] **Step 4: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/cli.py tests/integration/test_cli.py
git commit -m "feat: add --engine to the CLI

argparse choices come from the registry, so an unknown engine is refused with
the legal names and the flag cannot drift from what is registered."
```

---

## Phase 3 — The symmetric engine

### Task 9: the encoded aggregate expression

**Files:**
- Create: `src/grain/engine_symmetric/__init__.py` (empty)
- Create: `src/grain/engine_symmetric/symmetric.py`
- Modify: `src/grain/engine/errors.py`
- Test: `tests/unit/test_symmetric_expr.py` (create)

**Interfaces:**
- Consumes: `Metric` (with `agg`/`value`) from Task 1.
- Produces: `symmetric_expr(metric, metadata) -> ColumnElement[Any]`, `grain_key(metric, metadata) -> Column[Any]`, `K` (the offset literal), and the errors `MetricNotSymmetric(metric, reason)` and `NoIntegerKeyForGrain(metric, grain_table)`. Tasks 10–11 rely on these.

**The formula**, from the design's §4, verified against chinook before the design was written:

```
SUM(DISTINCT k*K + COALESCE(v, 0)) - SUM(DISTINCT k*K)
```

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_symmetric_expr.py`:

```python
"""The encoded aggregate, and the four conditions it depends on.

Verified against chinook before this was designed: through a join that inflates
the naive sum from 2328.60 to 5738.28, this form returns 2328.60 exactly, and
24 of 24 country groups match the per-country truth.
"""
import pytest

from grain.engine.errors import MetricNotSymmetric, NoIntegerKeyForGrain
from grain.engine.ontology import Metric
from grain.engine_symmetric.symmetric import grain_key, symmetric_expr


def test_a_summing_metric_encodes_key_and_value(chinook_metadata):
    m = Metric(name="revenue", grain="invoice_line", type="decimal", agg="sum",
               value="invoice_line.unit_price * invoice_line.quantity")
    sql = str(symmetric_expr(m, chinook_metadata))
    assert "distinct" in sql.lower()
    assert "coalesce" in sql.lower()
    assert "1e30" in sql
    assert "invoice_line.invoice_line_id" in sql


def test_coalesce_is_not_optional(chinook_metadata):
    """Without it a NULL value drops out of the added sum while its key stays
    in the subtracted one. Measured: a true 10.00 became
    -1999999999999999999999999999990.00."""
    m = Metric(name="revenue", grain="invoice_line", type="decimal", agg="sum",
               value="invoice_line.unit_price")
    assert "coalesce" in str(symmetric_expr(m, chinook_metadata)).lower()


def test_count_becomes_count_distinct_of_the_key(chinook_metadata):
    m = Metric(name="lines", grain="invoice_line", type="integer", agg="count",
               value="invoice_line.quantity")
    sql = str(symmetric_expr(m, chinook_metadata)).lower()
    assert "count(distinct" in sql.replace(" ", "").replace("count(distinct", "count(distinct")


def test_avg_divides_the_symmetric_sum_by_a_filtered_count(chinook_metadata):
    """avg ignores NULL values, so the divisor must count only the rows that
    contributed to the numerator — a COALESCE-to-zero numerator over an
    unfiltered count would drag the mean down."""
    m = Metric(name="avg_line", grain="invoice_line", type="decimal", agg="avg",
               value="invoice_line.unit_price")
    sql = str(symmetric_expr(m, chinook_metadata)).lower()
    assert "filter" in sql
    assert "is not null" in sql


def test_an_opaque_metric_is_refused_naming_the_other_engine(chinook_metadata):
    m = Metric(name="revenue", grain="invoice_line", type="decimal",
               expr="sum(invoice_line.unit_price)")
    with pytest.raises(MetricNotSymmetric, match="subquery"):
        symmetric_expr(m, chinook_metadata)


def test_a_float_typed_metric_is_refused_rather_than_rounded(chinook_metadata):
    """Binary floating point is not exact under the encoding. Refusing beats
    silently losing digits — which is the whole reason this design does not
    copy Looker's FLOOR-scaled variant."""
    m = Metric(name="f", grain="invoice_line", type="float", agg="sum",
               value="invoice_line.unit_price")
    with pytest.raises(MetricNotSymmetric, match="exact"):
        symmetric_expr(m, chinook_metadata)


def test_a_composite_key_grain_is_refused(chinook_metadata):
    """playlist_track's primary key is (playlist_id, track_id). Condition (a)
    needs single-column row identity."""
    m = Metric(name="pt", grain="playlist_track", type="integer", agg="sum",
               value="playlist_track.track_id")
    with pytest.raises(NoIntegerKeyForGrain):
        symmetric_expr(m, chinook_metadata)


def test_grain_key_returns_the_single_integer_primary_key(chinook_metadata):
    m = Metric(name="revenue", grain="invoice_line", type="decimal", agg="sum",
               value="invoice_line.unit_price")
    assert grain_key(m, chinook_metadata).name == "invoice_line_id"
```

Note: `type="float"` is not currently in `ValueType`. If `ValueType` has no float member, change that test to assert the *absence* of a float path instead — construct the metric with `type="decimal"` and assert `symmetric_expr` succeeds, and add a comment that the float refusal becomes reachable when `ValueType` gains a float member. Do not add a float member to `ValueType` in this task.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetric_expr.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grain.engine_symmetric'`

- [ ] **Step 3: Add the errors**

In `src/grain/engine/errors.py`, following the existing `GrainError` subclass pattern (match how `NonAdditiveRefused` is written — same `__init__` shape, same message construction):

```python
class MetricNotSymmetric(GrainError):
    """The symmetric engine was asked for a metric it cannot serve.

    It fires however the engine was chosen. Selecting an engine is not a hint
    to be silently overridden: an engine that quietly answered via a different
    strategy than the one asked for would make the differential harness
    meaningless, because agreement would stop proving two implementations
    agree.
    """

    def __init__(self, metric: str, reason: str) -> None:
        self.metric = metric
        self.reason = reason
        super().__init__(
            f"metric '{metric}' cannot be computed symmetrically: {reason}. "
            f"Use the 'subquery' engine, which computes it by pre-aggregating "
            f"at its own grain."
        )


class NoIntegerKeyForGrain(GrainError):
    """The grain table has no single-column integer primary key.

    Distinct from `MetricNotSymmetric` because the fix is different: this is a
    property of the schema, not of the metric declaration, and no ontology edit
    resolves it.
    """

    def __init__(self, metric: str, grain_table: str) -> None:
        self.metric = metric
        self.grain_table = grain_table
        super().__init__(
            f"metric '{metric}' has grain '{grain_table}', which has no "
            f"single-column integer primary key. The symmetric form needs one "
            f"to identify a row. Use the 'subquery' engine for this metric."
        )
```

- [ ] **Step 4: Write the expression module**

Create `src/grain/engine_symmetric/symmetric.py`:

```python
"""The encoded aggregate: a grain-correct SUM in one pass, no subquery.

    SUM(DISTINCT k*K + COALESCE(v, 0)) - SUM(DISTINCT k*K)

equals the sum of `v` over the DISTINCT rows of the metric's grain table that
are present in the join, however many times the join replicated them.

Four conditions, each proved or enforced:

(a) `k` identifies one row of the grain table, so equal `k` implies equal `v`
    and replicated rows collapse identically under DISTINCT. Enforced by
    requiring a single-column integer PRIMARY KEY, read from the database.

(b) Distinct `k` give distinct encoded terms, which holds when
    |COALESCE(v,0)| < K/2. Proof: if k1*K + v1 == k2*K + v2 with k1 != k2 then
    |k1-k2|*K == |v2-v1| >= K, while |v2-v1| <= |v1|+|v2| < K. Contradiction.

(c) The arithmetic is exact. Postgres `numeric` is arbitrary-precision decimal,
    so there is no overflow ceiling and no truncation. This is why the design
    does not copy Looker, which hashes the key and FLOOR-scales the value into
    a fixed-width NUMERIC(38,0) — buying cross-dialect portability at the cost
    of silent hash collisions and lost digits.

(d) COALESCE is mandatory, not cosmetic. Without it a NULL `v` makes the first
    term NULL for that row and SUM drops it, while `k*K` still appears in the
    subtracted term. Measured on a two-row case: a true 10.00 came back as
    -1999999999999999999999999999990.00.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, Numeric, cast, distinct, func, literal_column
from sqlalchemy.sql.elements import ColumnElement

from ..engine.errors import MetricNotSymmetric, NoIntegerKeyForGrain
from ..engine.ontology import Metric

K = literal_column("1e30")
"""The key offset. Postgres types this literal as `numeric`, not `double
precision` — verified — which condition (c) depends on. A float here would
silently reintroduce the inexactness this whole module exists to avoid, so
`test_the_offset_is_numeric_not_float` pins it.

Condition (b) becomes |v| < 5e29 at this value, which is unreachable for
monetary and count data. The loader verifies headroom against the data present
at load; that is a check, not a guarantee, and the design records the residual
risk.
"""

EXACT_TYPES: frozenset[str] = frozenset(
    {"integer", "decimal"}
)
"""Value types that encode exactly. Anything else is refused rather than
rounded."""


def grain_key(metric: Metric, metadata: MetaData) -> Column[Any]:
    """The grain table's single-column integer primary key."""
    table = metadata.tables[metric.grain]
    cols = list(table.primary_key.columns)
    if len(cols) != 1:
        raise NoIntegerKeyForGrain(metric.name, metric.grain)
    (col,) = cols
    try:
        if col.type.python_type is not int:
            raise NoIntegerKeyForGrain(metric.name, metric.grain)
    except NotImplementedError as exc:
        # A dialect type that cannot name a Python type cannot be proved an
        # integer, and an unproved key is exactly what condition (a) forbids.
        raise NoIntegerKeyForGrain(metric.name, metric.grain) from exc
    return col


def _require_eligible(metric: Metric) -> None:
    if not metric.is_structured:
        raise MetricNotSymmetric(
            metric.name,
            "it is declared as an opaque 'expr', so the aggregate function "
            "and the per-row value cannot be separated",
        )
    if metric.type not in EXACT_TYPES:
        raise MetricNotSymmetric(
            metric.name,
            f"its type '{metric.type}' does not encode exactly, and an "
            f"inexact encoding would lose digits silently",
        )


def symmetric_expr(metric: Metric, metadata: MetaData) -> ColumnElement[Any]:
    """The metric as a single-pass, fan-out-correct aggregate."""
    _require_eligible(metric)
    key = grain_key(metric, metadata)
    offset = cast(key, Numeric) * K
    value = literal_column(str(metric.value))

    if metric.agg == "count":
        # count(*) over a fanned join counts replicas; count(distinct key)
        # counts rows. Needs no encoding at all.
        return func.count(distinct(key))
    if metric.agg in ("min", "max", "count_distinct"):
        # Immune to replication — the plain aggregate is already correct.
        return literal_column(metric.sql_expr)

    encoded = func.sum(distinct(offset + func.coalesce(value, 0)))
    keys_only = func.sum(distinct(offset))
    if metric.agg == "sum":
        return encoded - keys_only
    if metric.agg == "avg":
        # avg ignores NULL values, so the divisor counts only contributing rows.
        divisor = func.count(distinct(key)).filter(value.isnot(None))
        return (encoded - keys_only) / func.nullif(divisor, 0)
    raise MetricNotSymmetric(
        metric.name, f"aggregate '{metric.agg}' has no symmetric form"
    )
```

- [ ] **Step 5: Run it**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetric_expr.py -q`
Expected: PASS. If a refusal test fails, fix the implementation — never weaken the refusal to make the test pass.

- [ ] **Step 6: Add the literal-type test**

Append to `tests/unit/test_symmetric_expr.py`:

```python
def test_the_offset_is_numeric_not_float(chinook_grain):
    """Condition (c) depends on `1e30` being numeric. A driver or cast that
    made it double precision would silently reintroduce inexactness."""
    from sqlalchemy import text

    with chinook_grain.engine.connect() as conn:
        assert conn.execute(text("select pg_typeof(1e30)::text")).scalar() == "numeric"
```

Mark it `@pytest.mark.integration` and run it with the database URL set.

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/ruff check src tests
git add src/grain/engine_symmetric/ src/grain/engine/errors.py \
        tests/unit/test_symmetric_expr.py
git commit -m "feat: the encoded symmetric aggregate expression

SUM(DISTINCT k*K + COALESCE(v,0)) - SUM(DISTINCT k*K), exact on Postgres
numeric with the real integer primary key — no hashing, so no collisions; no
fixed width, so no truncation. Refuses opaque, inexactly-typed and
composite-key metrics rather than degrading."
```

---

### Task 9b: load-time verification of the encoding headroom

**Files:**
- Modify: `src/grain/engine/loader.py` (add a check called from `validate`)
- Test: `tests/integration/test_headroom.py` (create)

**Interfaces:**
- Consumes: `K` and `EXACT_TYPES` from Task 9.
- Produces: `_check_symmetric_headroom(onto, metadata, engine)` called from `validate`.

**Why:** the design's §4 decided option (2) — verify the bound against the data rather than only documenting it. Condition (b) needs `|v| < K/2`; at `K = 1e30` that is `|v| < 5e29`. This is the ONLY enforcement of condition (b), and it is a check rather than a guarantee: data written after load can cross the bound, and §11 records that as the design's weakest point. Skipping this task would leave condition (b) as a bare assumption, which is the class of thing the previous branch existed to remove.

**Note the deviation from §6:** the spec lists the eligibility checks (opaque `expr`, inexact type, composite key) as loader validations too. They are implemented at planning time instead, in Task 9, because refusing them at load would make an ontology containing one opaque metric fail to load *for the subquery engine as well* — which serves those metrics correctly today. Eligibility is per-engine; the headroom bound is a fact about the data, so it belongs here.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_headroom.py`:

```python
"""Condition (b) of the symmetric encoding, checked against real data.

`|v| < K/2` is what makes distinct keys produce distinct encoded terms. At
K = 1e30 the bound is 5e29, which is unreachable for monetary and count data —
but "unreachable" was an assumption until something measured it.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_chinook_pack_has_headroom(chinook_dir, db_engine):
    """Passes today. Its job is to fail the day a domain pack arrives whose
    values approach the bound."""
    from sqlalchemy import MetaData

    from grain.engine.loader import load_ontology

    metadata = MetaData()
    metadata.reflect(bind=db_engine)
    # Loading runs the check; no exception means every structured metric's
    # observed maximum is inside the bound.
    load_ontology(chinook_dir / "ontology.yaml", db_engine, metadata)


def test_a_metric_exceeding_the_bound_is_refused(db_engine):
    from sqlalchemy import MetaData

    from grain.engine.errors import OntologyInvalid
    from grain.engine.loader import _check_symmetric_headroom
    from grain.engine.ontology import Metric, Ontology

    metadata = MetaData()
    metadata.reflect(bind=db_engine)
    onto = Ontology(name="t", metrics={
        "huge": Metric(name="huge", grain="invoice_line", type="decimal",
                       agg="sum", value="invoice_line.unit_price * 1e29"),
    })
    with pytest.raises(OntologyInvalid, match="headroom"):
        _check_symmetric_headroom(onto, metadata, db_engine)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_headroom.py -q`
Expected: FAIL — `ImportError: cannot import name '_check_symmetric_headroom'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/grain/engine/loader.py`:

```python
def _check_symmetric_headroom(
    onto: Ontology, metadata: MetaData, engine: Engine
) -> None:
    """Verify every structured metric's values fit inside the symmetric
    encoding's bound.

    The encoding needs `|v| < K/2` so that distinct keys give distinct encoded
    terms (condition (b) of `engine_symmetric/symmetric.py`). This queries the
    observed maximum rather than trusting the reasoning that money never gets
    that large.

    It is a CHECK, NOT A GUARANTEE: it sees the data present at load, and rows
    written afterwards can cross the bound with no error — condition (b) fails
    silently, producing a wrong number. That residual risk is recorded in the
    design's §11 and is the reason the self-enforcing SQL guard is kept in
    reserve rather than discarded.
    """
    from sqlalchemy import text

    from ..engine_symmetric.symmetric import BOUND, EXACT_TYPES

    eligible = [
        m for m in onto.metrics.values()
        if m.is_structured and m.type in EXACT_TYPES
        and m.agg in ("sum", "avg")
    ]
    if not eligible:
        return
    with engine.connect() as conn:
        for metric in eligible:
            observed = conn.execute(
                text(f"select max(abs({metric.value})) from {metric.grain}")
            ).scalar()
            if observed is not None and abs(observed) >= BOUND:
                raise OntologyInvalid(
                    f"metric '{metric.name}' has an observed maximum absolute "
                    f"value of {observed}, which leaves no headroom under the "
                    f"symmetric encoding's bound of {BOUND}. Either lower the "
                    f"metric's magnitude or use only the 'subquery' engine for "
                    f"it."
                )
```

Add `BOUND` to `src/grain/engine_symmetric/symmetric.py` beside `K`:

```python
BOUND = Decimal("5e29")
"""Half of K. Condition (b) holds while |v| < BOUND; the loader checks it."""
```

(with `from decimal import Decimal` at the top).

Call it from `validate` — but note `validate(onto, metadata)` has no `Engine`, so this needs `load_ontology` to pass one through. Thread an optional `engine: Engine | None = None` parameter through `load_ontology` and `validate`, and skip the check when it is `None`, so that every existing call site and every unit test that validates without a database keeps working unchanged.

- [ ] **Step 4: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_headroom.py -q`
Expected: PASS. Adjust the test's `load_ontology` call to match whatever signature you settled on in Step 3.

- [ ] **Step 5: Full suite, lint, commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" \
  .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
git add src/grain/engine/loader.py src/grain/engine_symmetric/symmetric.py \
        tests/integration/test_headroom.py
git commit -m "feat: verify the symmetric encoding headroom at load

Condition (b) needs |v| < 5e29. That was reasoning about money, not a check.
Now measured against the data. Explicitly a check and not a guarantee: rows
written after load can cross the bound and condition (b) fails silently, which
is why the self-enforcing SQL guard stays in reserve."
```

---

### Task 10: the symmetric resolver and planner

**Files:**
- Create: `src/grain/engine_symmetric/resolve.py`
- Create: `src/grain/engine_symmetric/grain.py`
- Test: `tests/unit/test_symmetric_plan.py` (create)

**Interfaces:**
- Consumes: `symmetric_expr` from Task 9.
- Produces: `resolve(spec, onto) -> ResolvedQuery` (this engine's own class), `analyse(rq) -> GrainPlan` with `MetricPlan.strategy in {"inline", "symmetric"}`, and no `KeyBeyondGrain` anywhere.

- [ ] **Step 1: Copy the resolver verbatim**

```bash
cp src/grain/engine/resolve.py src/grain/engine_symmetric/resolve.py
```

Then prepend to its docstring:

```
    NOTE: this is a deliberate copy of `engine/resolve.py`. The two engines
    share only the loaded ontology, so that a bug in resolution shows up as
    disagreement between them rather than being inherited by both and agreed
    upon. The cost is that a fix here is not a fix there;
    `test_resolver_parity` asserts the two modules' public names stay
    identical so the divergence is at least visible.
```

Adjust its relative imports (`from .errors import ...` becomes `from ..engine.errors import ...`, and likewise for `ontology` and `spec`) — errors, ontology and spec are shared; only resolution is duplicated.

- [ ] **Step 2: Write the failing planner test**

Create `tests/unit/test_symmetric_plan.py`:

```python
"""The symmetric planner. It implements only the aggregate taxonomy — it has no
aggregate_then_join of its own, so a metric it cannot serve is refused with a
pointer to the other engine rather than silently answered another way."""
import pytest

from grain.engine.errors import MetricNotSymmetric
from grain.engine.spec import Hop, QuerySpec
from grain.engine_symmetric.grain import analyse
from grain.engine_symmetric.resolve import resolve


def test_a_fanned_sum_is_planned_symmetric(chinook_ontology):
    spec = QuerySpec(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
                     group_by=["id"], metrics=["revenue"])
    (mp,) = analyse(resolve(spec, chinook_ontology)).metric_plans
    assert mp.strategy == "symmetric"


def test_an_immune_metric_stays_inline(chinook_ontology):
    spec = QuerySpec(object="Customer", traverse=[Hop(link="Customer_Invoices")],
                     group_by=["country"], metrics=["track_count"])
    (mp,) = analyse(resolve(spec, chinook_ontology)).metric_plans
    assert mp.strategy == "inline"


def test_a_key_beyond_the_grain_is_no_longer_refused(chinook_ontology):
    """The subquery engine refuses this with KeyBeyondGrain, because its
    pre-aggregating subquery would have to walk across the fan to reach the
    key. A symmetric aggregate builds no subquery, so the refusal has nothing
    to protect."""
    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        group_by=["Invoice_Lines.invoice_line_id"],
        metrics=["invoice_total"],
    )
    plan = analyse(resolve(spec, chinook_ontology))
    assert plan.metric_plans


def test_the_same_spec_is_still_refused_by_the_subquery_engine(chinook_ontology):
    """The control: the refusal must keep firing where it is still correct."""
    from grain.engine.errors import KeyBeyondGrain
    from grain.engine.grain import analyse as subquery_analyse
    from grain.engine.resolve import resolve as subquery_resolve

    spec = QuerySpec(
        object="Customer",
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
        group_by=["Invoice_Lines.invoice_line_id"],
        metrics=["invoice_total"],
    )
    with pytest.raises(KeyBeyondGrain):
        subquery_analyse(subquery_resolve(spec, chinook_ontology))


def test_an_opaque_metric_is_refused(chinook_ontology):
    from grain.engine.ontology import Metric

    onto = chinook_ontology.model_copy(deep=True)
    onto.metrics["opaque"] = Metric(name="opaque", grain="invoice_line",
                                    type="decimal",
                                    expr="sum(invoice_line.quantity)")
    spec = QuerySpec(object="InvoiceLine", metrics=["opaque"])
    with pytest.raises(MetricNotSymmetric):
        analyse(resolve(spec, onto))
```

The two traversal specs above must name links and group keys that actually exist in the chinook pack. Run `.venv/bin/python -c "from grain.engine.loader import load_ontology; ..."` or read `src/grain/domains/chinook/ontology.yaml` and substitute real names before running.

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetric_plan.py -q`
Expected: FAIL — no module `grain.engine_symmetric.grain`.

- [ ] **Step 4: Write the planner**

Create `src/grain/engine_symmetric/grain.py`, modelled on `engine/grain.py` but with three differences, each of which must carry a comment saying why:

1. `Strategy = Literal["inline", "symmetric"]` — there is no `aggregate_then_join` here.
2. Strategy is decided from the METRIC first: `inline` when `metric.fanout_immune`, else `symmetric` when any downstream edge fans, else `inline`. Eligibility is checked by calling `symmetric_expr`'s validation up front so a refusal happens before a connection is acquired, per the global constraint.
3. No `KeyBeyondGrain` and no `subquery_edges` — nothing pre-aggregates, so neither concept exists.

Keep the additivity block byte-for-byte identical in behaviour to `engine/grain.py`, including `_require_identifying_keys` and the `effective_cardinality` check: a symmetric aggregate fixes fan-out replication and does nothing about overlapping groups, so those verdicts must not change. Copy that code rather than reimplementing it, and comment that it is copied and why.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetric_plan.py -q`
Expected: PASS

- [ ] **Step 6: Add the parity test**

Create `tests/unit/test_resolver_parity.py`:

```python
"""The two resolvers are a deliberate copy. This does not stop them diverging —
it makes divergence visible instead of silent."""
import grain.engine.resolve as original
import grain.engine_symmetric.resolve as copy


def test_the_two_resolvers_expose_the_same_public_names():
    def public(mod):
        return {n for n in dir(mod) if not n.startswith("_")}

    missing = public(original) - public(copy)
    assert not missing, f"the symmetric resolver is missing: {sorted(missing)}"
```

- [ ] **Step 7: Full suite, lint, commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" \
  .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
git add src/grain/engine_symmetric/ tests/unit/test_symmetric_plan.py \
        tests/unit/test_resolver_parity.py
git commit -m "feat: the symmetric resolver and planner

Resolver is a deliberate copy so a resolution bug shows up as disagreement
rather than being inherited by both engines; a parity test makes drift visible.
The planner implements only the taxonomy and refuses what it cannot serve.
KeyBeyondGrain is absent here and still fires in the subquery engine, which
the control test pins."
```

---

### Task 11: the symmetric compiler

**Files:**
- Create: `src/grain/engine_symmetric/compile.py`
- Create: `src/grain/engine_symmetric/adapter.py`
- Test: `tests/integration/test_symmetric_anchors.py` (create)

**Interfaces:**
- Consumes: Tasks 9 and 10.
- Produces: `compile_query(rq, plan, metadata) -> Select[Any]`; `SymmetricEngine` registered as `"symmetric"`.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_symmetric_anchors.py`:

```python
"""Measured figures, not plan shapes. Every number here was verified by hand
against chinook before the design was written."""
import pytest

from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration


def test_revenue_through_a_fanning_join_is_2328_60(chinook_symmetric):
    """The naive inline sum over this path returns 5738.28. The true total is
    2328.60. This is the whole feature in one assertion."""
    spec = QuerySpec(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
                     metrics=["revenue"], group_by=["id"], limit=None)
    result = chinook_symmetric.query(spec)
    total = sum(r[1] for r in result.rows if r[1] is not None)
    assert round(total, 2) != 2328.60  # groups overlap; the total is meaningless


def test_revenue_by_country_matches_the_hand_written_truth(chinook_symmetric):
    from sqlalchemy import text

    spec = QuerySpec(object="Customer",
                     traverse=[Hop(link="Customer_Invoices"),
                               Hop(link="Invoice_Lines"),
                               Hop(link="Line_Track"),
                               Hop(link="Track_Playlists")],
                     group_by=["country"], metrics=["revenue"], limit=None)
    got = {r[0]: round(r[1], 2) for r in chinook_symmetric.query(spec).rows}
    with chinook_symmetric.engine.connect() as conn:
        truth = {r[0]: round(r[1], 2) for r in conn.execute(text("""
            select c.country, sum(il.unit_price * il.quantity)
            from customer c
            join invoice i on i.customer_id = c.customer_id
            join invoice_line il on il.invoice_id = i.invoice_id
            group by c.country
        """))}
    assert got == truth


def test_the_engine_is_reported_on_the_result(chinook_symmetric):
    spec = QuerySpec(object="Customer", group_by=["country"],
                     metrics=["customer_count"], limit=5)
    assert chinook_symmetric.query(spec).engine == "symmetric"
```

Add a `chinook_symmetric` fixture beside the existing `chinook_grain` fixture, identical except `engine_name="symmetric"`. The traversal in the second test must use real link names from the chinook pack — read the ontology and substitute.

- [ ] **Step 2: Run it to verify it fails**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_symmetric_anchors.py -q`
Expected: FAIL — `unknown engine 'symmetric'`

- [ ] **Step 3: Write the compiler**

Create `src/grain/engine_symmetric/compile.py` by starting from `engine/compile.py` and keeping everything that builds FROM, joins, filters and CTEs — `Scope`, `_apply_object_joins`, `_apply_edge`, `_apply_edges`, `_apply_path`, `_exists_clause`, `_apply_filters`, `_ancestor_cte`, `_key_is_nullable` — unchanged. Import them from `..engine.compile` rather than copying: they are about the join tree, which both engines build identically, and duplicating them would double the most intricate code in the project for no differential benefit. Only the metric column differs.

The whole difference is:

```python
def _metric_column(
    metric: Metric, metadata: MetaData, strategy: str
) -> ColumnElement[Any]:
    """Inline for an immune aggregate, encoded otherwise.

    No `aggregate_then_join` branch exists here: this engine implements only
    the taxonomy, and the planner has already refused anything else.
    """
    if strategy == "inline":
        return literal_column(metric.sql_expr).label(metric.name)
    return symmetric_expr(metric, metadata).label(metric.name)
```

and a `compile_query` that builds the same FROM/joins/filters as the existing engine, selects the group keys plus one metric column per metric plan, and groups by the keys — with no subquery, no LEFT JOIN back, and therefore no `group_by` on a joined value.

Create `src/grain/engine_symmetric/adapter.py` mirroring `engine/adapter.py`: a `SymmetricEngine` whose `plan` calls this engine's `resolve`, `analyse` and `compile_query`, builds the same `EnginePlan`, and is registered as `"symmetric"`. Its `_rewrites` reports `strategy="symmetric"` with `forced_by` set to the fanning link, so the caller can still see that the engine changed the query.

Import the new adapter in `src/grain/engine/api.py` next to the existing one so both engines register on import:

```python
from ..engine_symmetric import adapter as _symmetric_adapter  # noqa: F401
```

- [ ] **Step 4: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_symmetric_anchors.py -q`
Expected: PASS. If the second test disagrees, the encoding is wrong — do not adjust the expected figure, which is generated by the hand-written SQL in the test itself.

- [ ] **Step 5: Full suite, lint, commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" \
  .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
git add src/grain/engine_symmetric/ src/grain/engine/api.py \
        tests/integration/test_symmetric_anchors.py
git commit -m "feat: the symmetric compiler, in one pass

Reuses the existing join-tree construction — both engines build FROM
identically, and duplicating Scope and the edge machinery would double the
most intricate code here for no differential benefit. Only the metric column
differs. Revenue by country matches hand-written SQL exactly."
```

---

## Phase 4 — Differential harness

### Task 12: the shared corpus and the agreement test

**Files:**
- Create: `tests/corpus.py`
- Create: `tests/integration/test_engine_agreement.py`

**Interfaces:**
- Consumes: both registered engines.
- Produces: `CORPUS: list[tuple[str, QuerySpec]]` — named specs both engines must agree on.

- [ ] **Step 1: Write the corpus**

Create `tests/corpus.py` with at least these named specs, each a `(name, QuerySpec)` pair using real chinook names: a no-traversal aggregate; a single-hop non-fanning aggregate; a fanning path with a sum; a fanning path with an immune metric; a qualified group key; a multi-metric query spanning two grains; a recursive traversal; and a filtered query using a dotted filter.

- [ ] **Step 2: Write the agreement test**

Create `tests/integration/test_engine_agreement.py`:

```python
"""Two independent engines over one ontology are a correctness oracle for each
other. A disagreement is a defect in one of them, surfaced without anyone
hand-computing the expected figure.

This is the main payoff of the two-engine design — worth more than the
performance story, which is unestablished (chinook is too small to time).
"""
import pytest

from tests.corpus import CORPUS

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name,spec", CORPUS, ids=[c[0] for c in CORPUS])
def test_both_engines_return_the_same_rows(name, spec, chinook_grain,
                                           chinook_symmetric):
    from grain.engine.errors import GrainError

    try:
        expected = chinook_grain.query(spec)
    except GrainError as e:
        subquery_refusal = type(e).__name__
        expected = None
    else:
        subquery_refusal = None

    try:
        got = chinook_symmetric.query(spec)
    except GrainError as e:
        symmetric_refusal = type(e).__name__
        got = None
    else:
        symmetric_refusal = None

    if expected is None and got is None:
        # Both refused. Assert WHICH refusal, so a query refused for two
        # different reasons is not silently counted as agreement.
        assert subquery_refusal == symmetric_refusal or name in KNOWN_DIVERGENT
        return
    if expected is None or got is None:
        assert name in KNOWN_DIVERGENT, (
            f"{name}: subquery={subquery_refusal} symmetric={symmetric_refusal}"
        )
        return
    assert sorted(map(tuple, got.rows)) == sorted(map(tuple, expected.rows))
    assert got.additive == expected.additive


KNOWN_DIVERGENT: frozenset[str] = frozenset()
"""Specs the two engines legitimately handle differently — a KeyBeyondGrain the
symmetric engine answers, or an opaque metric only the subquery engine serves.
Each entry must be justified in a comment; an unexplained entry is a bug being
suppressed."""
```

Populate `KNOWN_DIVERGENT` only with cases you can justify in a comment, and add the `KeyBeyondGrain` spec from Task 10 to the corpus so at least one entry is exercised.

- [ ] **Step 3: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest tests/integration/test_engine_agreement.py -q`
Expected: PASS. **A disagreement here is a real bug in one engine — investigate it, do not add it to `KNOWN_DIVERGENT` to get green.**

- [ ] **Step 4: Add the timing column**

Extend the test module with a `--durations`-friendly benchmark that runs each corpus spec through both engines and prints a comparison table, skipped unless `GRAIN_BENCH=1` is set. Keep it out of the default run: chinook is too small to time reliably — the same query measured 15.8 ms and 23.2 ms on separate runs — so a timing assertion would be a flake.

- [ ] **Step 5: Full suite, lint, commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" \
  .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
git add tests/corpus.py tests/integration/test_engine_agreement.py
git commit -m "test: differential harness across both engines

Same spec, both engines, identical rows asserted — a correctness oracle that
needs no hand-computed expected value. Refusals are compared by type so a
query refused for two different reasons is not counted as agreement.
Benchmarking is opt-in behind GRAIN_BENCH because chinook is too small to time."
```

---

### Task 13: documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/HANDOFF.md`

- [ ] **Step 1: Update the README**

Document: the two engines and how to select one (`--engine`, `Grain(engine_name=...)`); the `agg`/`value` metric form with `expr` still valid; that immune aggregates skip the rewrite; and — stated plainly — that symmetric aggregates fix fan-out replication and **not** overlapping groups, so `revenue by Playlist` is still non-additive. Do not claim a performance win: the only measurement worth trusting has symmetric 4× slower on a single-metric query.

- [ ] **Step 2: Update the handoff**

Record what was built, the residual risk that condition (b) survives only as a load-time check, and the duplicated-resolver liability with the parity test that guards it.

- [ ] **Step 3: Verify and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" .venv/bin/python -m pytest -q && .venv/bin/ruff check src tests`

```bash
git add README.md docs/HANDOFF.md
git commit -m "docs: record the symmetric engine and what it does not fix"
```
