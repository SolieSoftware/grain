# Order Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let both engines answer `median` and `percentile(p)` over a fanning join, correctly — the symmetric engine via a new encoding, the subquery engine for free.

**Both engines, deliberately.** This is not duplication for its own sake — it is
the verification mechanism. The two arrive at the same number by routes that
share no code: the subquery engine pre-aggregates at the grain and calls
Postgres's own `percentile_disc`, while the symmetric engine sorts an encoded
array in one pass. Neither can inherit the other's mistake, so their agreement
in Task 5's differential harness is real evidence rather than a tautology. An
implementation that landed in only one engine would forfeit that, which is the
main reason to build both.

**Architecture:** Two aggregates join the taxonomy. The subquery engine needs nothing but the taxonomy entry, because it pre-aggregates at the grain and a plain `percentile_disc` is already correct there. The symmetric engine has no subquery to hide behind, so it packs the value into the high digits of a numeric and the primary key into the low ones, deduplicates the encoded pair, indexes the sorted array, and decodes.

**Tech Stack:** Python 3.12+ · SQLAlchemy 2.x (`select()` style only) · Pydantic v2 · psycopg 3 · pytest · ruff

**Spec:** `docs/plans/2026-09-05-order-statistics-design.md` — read it first. This plan implements it and argues from it.

## Global Constraints

- **`K = 1e19`** is the key offset. It exceeds any bigint key **by proof** (a bigint cannot exceed 9.22×10¹⁸), so unlike the symmetric sum's `|v| < 5e29` it needs no load-time check and cannot drift as data grows.
- **`s` is the scale of the REFLECTED column**, never the ontology's declared `type`: `Integer` → `0`, `Numeric` with a declared scale → that scale, anything else → refuse. `ValueType` has no float member, so a `double precision` column would be declared `decimal` and trusting the declaration would admit an inexact column.
- **`percentile_disc` semantics.** Index is `greatest(1, ceil(p * n))` — verified exact at p = 0, 0.25, 0.5, 0.75, 0.9, 1.0. `percentile_cont` is a stated non-goal.
- **Only a bare `table.column` value is eligible** in the symmetric engine. A scale can be read off a column, not off `a * b`.
- Every refusal is `MetricNotSymmetric` or `NoIntegerKeyForGrain`, naming the `subquery` engine, and is raised **before a connection is acquired**.
- Python 3.12+, line length 100, `ruff check src tests tools` must pass.
- Tests run with `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook"`. Expect **421 passing** before this plan starts. Check the skip count, not the colour.
- `pyproject.toml` promotes `SAWarning` to an error. Do not weaken it.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/grain/engine/ontology.py` | `AggFunc` gains `median`, `percentile`; `Metric.percentile` field + validator; `sql_expr` renders both |
| `src/grain/engine_symmetric/scale.py` | **new** — derive a column's decimal scale from reflection; the one place that decides eligibility by type |
| `src/grain/engine_symmetric/symmetric.py` | `order_statistic_expr`, dispatched from `symmetric_expr`; refusals in `require_eligible` |
| `src/grain/domains/chinook/ontology.yaml` | two real metrics so the corpus and anchors have something to exercise |
| `tests/unit/test_order_statistics.py` | **new** — the encoding, the refusals, the index formula |
| `tests/integration/test_order_statistic_anchors.py` | **new** — measured against the unfanned truth |
| `tests/corpus.py` | median/percentile specs, so the differential harness covers them |
| `tools/oracle.py` | order-statistic support, since it is the only check sharing no SQL with either engine |

`scale.py` is a separate module rather than a function in `symmetric.py` because it is the single point where the database's opinion overrides the ontology's, and that deserves its own file and its own tests.

---

## Task 1: `median` and `percentile` in the taxonomy

**Files:**
- Modify: `src/grain/engine/ontology.py` — `AggFunc` (line 12), `Metric` (from line 18)
- Test: `tests/unit/test_order_statistics.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AggFunc` including `"median"` and `"percentile"`; `Metric.percentile: float | None`; `Metric.sql_expr` rendering `percentile_disc(p) within group (order by value)`. Tasks 2–5 rely on these exact names.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_order_statistics.py`:

```python
"""Order statistics: median and percentile over a fanning join.

The subquery engine gets these for free — it pre-aggregates at the grain, so a
plain `percentile_disc` is already looking at distinct rows. The symmetric
engine has no subquery, so it encodes; see `test_the_encoding` below.
"""
import pytest
from pydantic import ValidationError

from grain.engine.ontology import Metric


def test_median_renders_as_percentile_disc():
    m = Metric(name="med", grain="track", type="integer", agg="median",
               value="track.milliseconds")
    assert m.sql_expr == (
        "percentile_disc(0.5) within group (order by track.milliseconds)")


def test_percentile_renders_with_its_own_p():
    m = Metric(name="p90", grain="track", type="integer", agg="percentile",
               percentile=0.9, value="track.milliseconds")
    assert m.sql_expr == (
        "percentile_disc(0.9) within group (order by track.milliseconds)")


def test_percentile_requires_a_p():
    """A percentile with no p has no meaning, and defaulting it would silently
    answer a question nobody asked."""
    with pytest.raises(ValidationError, match="percentile"):
        Metric(name="p", grain="track", type="integer", agg="percentile",
               value="track.milliseconds")


def test_p_is_refused_on_any_other_aggregate():
    """A field that is meaningful for exactly one aggregate must be rejected
    elsewhere, or it reads as configuration that does nothing."""
    with pytest.raises(ValidationError, match="percentile"):
        Metric(name="s", grain="track", type="integer", agg="sum",
               percentile=0.9, value="track.milliseconds")


@pytest.mark.parametrize("p", [-0.1, 1.1, 2.0])
def test_p_outside_zero_to_one_is_refused(p):
    with pytest.raises(ValidationError):
        Metric(name="p", grain="track", type="integer", agg="percentile",
               percentile=p, value="track.milliseconds")


@pytest.mark.parametrize("p", [0.0, 0.5, 1.0])
def test_the_endpoints_are_legal(p):
    """p=0 is the minimum and p=1 the maximum; both are meaningful."""
    assert Metric(name="p", grain="track", type="integer", agg="percentile",
                  percentile=p, value="track.milliseconds").percentile == p


@pytest.mark.parametrize("agg", ["median", "percentile"])
def test_neither_is_fanout_immune(agg):
    """A fanning join genuinely corrupts an order statistic — the median
    `track.milliseconds` reads 256026 over the fan against a true 255634 — so
    neither may join FANOUT_IMMUNE."""
    kw = {"percentile": 0.5} if agg == "percentile" else {}
    m = Metric(name="m", grain="track", type="integer", agg=agg,
               value="track.milliseconds", **kw)
    assert m.fanout_immune is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_order_statistics.py -q`
Expected: FAIL — `median` is not a member of `AggFunc`, and `Metric` has no `percentile` field.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine/ontology.py`, extend the literal:

```python
AggFunc = Literal[
    "sum", "count", "count_distinct", "min", "max", "avg", "median", "percentile"
]
```

Add the field to `Metric`, after `value`:

```python
    percentile: float | None = None
    """The p of `agg: percentile`, in [0, 1]. Meaningless on any other
    aggregate and refused there, because a field that quietly does nothing is
    worse than no field."""
```

Add a validator beside `_check_exactly_one_form`:

```python
    @model_validator(mode="after")
    def _check_percentile(self) -> "Metric":
        if self.agg == "percentile":
            if self.percentile is None:
                raise ValueError(
                    f"metric '{self.name}' uses agg 'percentile' but sets no "
                    f"'percentile' value. A percentile with no p has no meaning."
                )
            if not 0.0 <= self.percentile <= 1.0:
                raise ValueError(
                    f"metric '{self.name}' has percentile {self.percentile}, "
                    f"which is outside [0, 1]."
                )
        elif self.percentile is not None:
            raise ValueError(
                f"metric '{self.name}' sets 'percentile' but its agg is "
                f"'{self.agg}', where p has no meaning. Remove it, or use "
                f"agg: percentile."
            )
        return self
```

Extend `sql_expr`, before the final `return`:

```python
        if self.agg in ("median", "percentile"):
            # `percentile_disc` returns a value that actually occurs in the
            # data. `percentile_cont` would interpolate between the two middle
            # values of an even-sized set, returning a number that may appear
            # nowhere — a deliberate non-goal.
            p = 0.5 if self.agg == "median" else self.percentile
            return f"percentile_disc({p}) within group (order by {self.value})"
```

`fanout_immune` needs no change: `FANOUT_IMMUNE` is `{min, max, count_distinct}` and neither new member is in it.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_order_statistics.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Full suite and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`
Expected: 432 passed, lint clean.

```bash
git add src/grain/engine/ontology.py tests/unit/test_order_statistics.py
git commit -m "feat: median and percentile in the aggregate taxonomy

percentile_disc semantics — a value that actually occurs in the data, rather
than percentile_cont interpolating a number that may appear nowhere.

p is required for agg: percentile and refused on every other aggregate. A field
meaningful for exactly one aggregate has to be rejected elsewhere, or it reads
as configuration that silently does nothing."
```

---

## Task 2: derive a column's scale from reflection

**Files:**
- Create: `src/grain/engine_symmetric/scale.py`
- Test: `tests/unit/test_order_statistics.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `column_scale(column: Column[Any]) -> int | None` — the number of decimal places the column can hold exactly, or `None` when that cannot be established. Task 3 relies on this exact name and return type.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_order_statistics.py`:

```python
# -- scale, read from the database rather than the ontology -------------------

def test_an_integer_column_has_scale_zero(lite_metadata):
    from grain.engine_symmetric.scale import column_scale

    col = lite_metadata.tables["track"].columns["milliseconds"]
    assert column_scale(col) == 0


def test_a_numeric_column_reports_its_declared_scale(chinook_metadata):
    """Reflection carries it: NUMERIC(10,2) gives scale=2. Multiplying by 10^2
    clears the fraction exactly, with nothing to round.

    This is the difference from Looker, which FLOOR-scales to a guessed
    precision of 6 and therefore must truncate."""
    from grain.engine_symmetric.scale import column_scale

    col = chinook_metadata.tables["invoice_line"].columns["unit_price"]
    assert column_scale(col) == 2


@pytest.mark.parametrize("table,column", [
    ("track", "name"),          # VARCHAR
    ("invoice", "invoice_date"),  # TIMESTAMP
])
def test_a_non_numeric_column_has_no_scale(table, column, chinook_metadata):
    from grain.engine_symmetric.scale import column_scale

    assert column_scale(chinook_metadata.tables[table].columns[column]) is None


def test_a_float_column_has_no_scale():
    """Binary floating point has no exact decimal scale, so it cannot be
    encoded without rounding. Caught here rather than by naming float in a
    list, because ValueType has no float member — a double precision column
    would be DECLARED decimal, and trusting the declaration would admit it."""
    from sqlalchemy import Column, Float, MetaData, Table

    from grain.engine_symmetric.scale import column_scale

    md = MetaData()
    t = Table("f", md, Column("x", Float))
    assert column_scale(t.columns["x"]) is None


def test_an_unconstrained_numeric_has_no_scale():
    """`numeric` with no precision holds arbitrary scale, so no fixed power of
    ten clears the fraction."""
    from sqlalchemy import Column, MetaData, Numeric, Table

    from grain.engine_symmetric.scale import column_scale

    md = MetaData()
    t = Table("n", md, Column("x", Numeric))
    assert column_scale(t.columns["x"]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/unit/test_order_statistics.py -q -k scale`
Expected: FAIL — `ModuleNotFoundError: No module named 'grain.engine_symmetric.scale'`

- [ ] **Step 3: Write minimal implementation**

Create `src/grain/engine_symmetric/scale.py`:

```python
"""How many decimal places a column can hold exactly.

The one place where the DATABASE overrides the ontology. `ValueType` has no
float member, so a `double precision` column can be — and would be — declared
`decimal` by an ontology author. Trusting that declaration would let an inexact
column into an encoding that depends on exactness, so eligibility is decided
from the reflected column instead. The database is authoritative here exactly as
it already is for cardinality, uniqueness and nullability.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Integer, Numeric


def column_scale(column: Column[Any]) -> int | None:
    """The column's exact decimal scale, or None if it has none.

    `None` means "cannot be encoded exactly" and is the caller's cue to refuse.
    It covers three cases that need no separate handling: floating point, which
    has no exact decimal scale at all; `numeric` declared without precision,
    which holds arbitrary scale so no fixed power of ten clears the fraction;
    and every non-numeric type.
    """
    if isinstance(column.type, Integer):
        return 0
    if isinstance(column.type, Numeric):
        # `Float` subclasses `Numeric` in SQLAlchemy, and reports scale None —
        # so this branch rejects it without naming it. `asdecimal` is not
        # consulted: it describes how values are returned to Python, not what
        # the column can hold.
        scale = getattr(column.type, "scale", None)
        return scale if scale is not None else None
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/unit/test_order_statistics.py -q -k scale`
Expected: PASS (6 tests)

If `test_a_float_column_has_no_scale` fails because SQLAlchemy's `Float` reports a scale, add an explicit `isinstance(column.type, Float)` guard returning `None` **before** the `Numeric` branch, and note in the docstring that Float subclasses Numeric so order matters.

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine_symmetric/scale.py tests/unit/test_order_statistics.py
git commit -m "feat: derive a column's exact decimal scale from reflection

The single point where the database overrides the ontology. ValueType has no
float member, so a double precision column would be declared decimal — trusting
the declaration would admit an inexact column into an encoding that depends on
exactness. None means 'cannot be encoded exactly' and covers float,
unconstrained numeric and every non-numeric type in one rule."
```

---

## Task 3: the encoded order statistic

**Files:**
- Modify: `src/grain/engine_symmetric/symmetric.py` — `require_eligible` (line 110), `symmetric_expr` (line 151)
- Test: `tests/unit/test_order_statistics.py` (append)

**Interfaces:**
- Consumes: `Metric.percentile` and `sql_expr` from Task 1; `column_scale` from Task 2.
- Produces: `ORDER_STATISTICS: frozenset[str]`, `order_statistic_expr(metric, metadata) -> ColumnElement[Any]`, and `symmetric_expr` dispatching to it. Task 4 relies on the emitted SQL shape.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_order_statistics.py`:

```python
# -- the encoding -------------------------------------------------------------

def _median(value="track.milliseconds", type_="integer"):
    return Metric(name="m", grain="track", type=type_, agg="median", value=value)


def test_the_encoding_packs_value_and_key(lite_metadata):
    from grain.engine_symmetric.symmetric import symmetric_expr

    sql = str(symmetric_expr(_median(), lite_metadata)).lower()
    assert "array_agg" in sql
    assert "distinct" in sql
    assert "1e19" in sql                      # the key offset
    assert "track.track_id" in sql            # the key itself
    assert "track.milliseconds" in sql        # the value


def test_the_index_is_the_percentile_disc_definition(lite_metadata):
    """`greatest(1, ceil(p * n))` — the first value whose cumulative fraction
    reaches p. Verified exact at p = 0, .25, .5, .75, .9, 1.0 against
    percentile_disc on the unfanned table."""
    from grain.engine_symmetric.symmetric import symmetric_expr

    sql = str(symmetric_expr(_median(), lite_metadata)).lower()
    assert "greatest" in sql and "ceil" in sql
    assert "count(distinct" in sql.replace(" ", "").replace("count(distinct", "count(distinct")


def test_a_decimal_value_is_scaled_by_its_own_scale(chinook_metadata):
    """unit_price is NUMERIC(10,2), so 10^2 clears the fraction exactly."""
    from grain.engine_symmetric.symmetric import symmetric_expr

    m = Metric(name="m", grain="invoice_line", type="decimal", agg="median",
               value="invoice_line.unit_price")
    sql = str(symmetric_expr(m, chinook_metadata)).lower()
    assert "100" in sql   # 10^2


def test_a_value_that_is_not_a_bare_column_is_refused(lite_metadata):
    """A scale can be read off a column, not off an expression."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import symmetric_expr

    with pytest.raises(MetricNotSymmetric, match="bare column"):
        symmetric_expr(_median(value="track.milliseconds * 2"), lite_metadata)


def test_a_column_with_no_exact_scale_is_refused(chinook_metadata):
    """A text column cannot be packed into an orderable number."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import symmetric_expr

    m = Metric(name="m", grain="track", type="string", agg="median",
               value="track.name")
    with pytest.raises(MetricNotSymmetric, match="exact"):
        symmetric_expr(m, chinook_metadata)


def test_every_refusal_names_the_other_engine(lite_metadata):
    """grain's errors each name a legal alternative, and the subquery engine
    computes all of these correctly by pre-aggregating at the grain."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import symmetric_expr

    with pytest.raises(MetricNotSymmetric) as exc:
        symmetric_expr(_median(value="track.milliseconds * 2"), lite_metadata)
    assert "subquery" in str(exc.value)


def test_eligibility_is_checkable_before_a_connection(lite_metadata):
    """The planner calls this, and every failure except GuardTripped is raised
    before a connection is acquired."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import require_eligible

    require_eligible(_median(), lite_metadata)
    with pytest.raises(MetricNotSymmetric):
        require_eligible(_median(value="track.milliseconds * 2"), lite_metadata)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/unit/test_order_statistics.py -q -k "encoding or index or scaled or bare or exact or names_the_other or before_a_connection"`
Expected: FAIL — `symmetric_expr` reaches its final `raise MetricNotSymmetric(... 'has no symmetric form')` for `median`.

- [ ] **Step 3: Write minimal implementation**

In `src/grain/engine_symmetric/symmetric.py`, add the imports and constants near `NEEDS_ENCODING`:

```python
import re

from .scale import column_scale

ORDER_STATISTICS: frozenset[str] = frozenset({"median", "percentile"})
"""Aggregates that need the array encoding rather than the sum encoding.

A sum decomposes — SUM(DISTINCT k*K + v) - SUM(DISTINCT k*K) recovers the total
because addition is associative and the key contribution cancels exactly. An
order statistic does not: it needs the multiset of distinct values IN ORDER, so
it is built by sorting an encoded array and indexing it.
"""

BARE_COLUMN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$")
```

Add the expression builder above `symmetric_expr`:

```python
def order_statistic_expr(
    metric: Metric, metadata: MetaData
) -> ColumnElement[Any]:
    """A median or percentile that survives a fanning join, in one pass.

        encoded = (v * 10^s) * K + pk
        result  = floor(sorted_distinct[greatest(1, ceil(p*n))] / K) / 10^s

    Ordering: `10^s` and `K` are positive constants, so the encoded number sorts
    in the same order as `v`, with the key in the low digits breaking ties
    without disturbing that order.

    Deduplication: DISTINCT applies to the encoded pair, which is equivalent to
    deduplicating on the key because `v` is functionally determined by `pk`.
    This is WHY the key is in the encoding — `array_agg(DISTINCT v)` would
    collapse two different rows that happen to share a value, and those are two
    data points.

    K needs no runtime check, unlike the symmetric sum's `|v| < 5e29`. The
    requirement is `K > max(pk)`, and a bigint cannot exceed 9.22e18, so the
    key's own TYPE guarantees it. A proof rather than a measurement.
    """
    match = BARE_COLUMN.match(metric.value or "")
    if match is None:
        raise MetricNotSymmetric(
            metric.name,
            "an order statistic needs the scale of the column it measures, and "
            "a scale can be read off a bare column but not off an expression",
        )
    table_name, column_name = match.group(1), match.group(2)
    column = metadata.tables[table_name].columns[column_name]
    scale = column_scale(column)
    if scale is None:
        raise MetricNotSymmetric(
            metric.name,
            f"'{table_name}.{column_name}' has no exact decimal scale, so it "
            f"cannot be encoded without rounding",
        )

    key = grain_key(metric, metadata)
    factor = literal_column(str(10 ** scale))
    encoded = cast(column, Numeric) * factor * K + cast(key, Numeric)
    p = 0.5 if metric.agg == "median" else metric.percentile
    index = func.greatest(1, func.ceil(literal_column(str(p)) * func.count(distinct(key))))
    picked = func.array_agg(aggregate_order_by(distinct(encoded), encoded))[index]
    return func.floor(picked / K) / factor
```

Add `aggregate_order_by` to the SQLAlchemy imports:

```python
from sqlalchemy.dialects.postgresql import aggregate_order_by
```

Dispatch to it in `symmetric_expr`, immediately after the immune branch:

```python
    if metric.agg in ORDER_STATISTICS:
        return order_statistic_expr(metric, metadata)
```

And in `require_eligible`, after the `is_structured` check:

```python
    if metric.agg in ORDER_STATISTICS:
        # Building the expression IS the eligibility check — it raises for a
        # non-bare value or a column with no exact scale. Doing it here keeps
        # the refusal before a connection is acquired.
        order_statistic_expr(metric, metadata)
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/unit/test_order_statistics.py -q`
Expected: PASS.

If `aggregate_order_by` produces `array_agg(DISTINCT x ORDER BY y)` where Postgres requires the ORDER BY expression to match the DISTINCT expression, the two must be the identical Python object — build `encoded` once and pass the same variable to both, which the code above already does.

- [ ] **Step 5: Full suite, lint, commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

```bash
git add src/grain/engine_symmetric/symmetric.py tests/unit/test_order_statistics.py
git commit -m "feat: encoded order statistics in the symmetric engine

A sum decomposes and an order statistic does not, so this uses a second
encoding: pack the value into the high digits and the key into the low ones,
let DISTINCT dedupe the pair, sort, index, decode.

K needs no runtime check, unlike the symmetric sum's |v| < 5e29. The requirement
is K > max(pk) and a bigint cannot exceed 9.22e18, so the key's own type
guarantees it — a proof rather than a measurement.

Refuses a non-bare value and a column with no exact decimal scale, both naming
the subquery engine, which computes these correctly by pre-aggregating."
```

---

## Task 4: measured anchors against the unfanned truth

**Files:**
- Modify: `src/grain/domains/chinook/ontology.yaml` — add two metrics
- Create: `tests/integration/test_order_statistic_anchors.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `median_duration` and `p90_duration` metrics in the chinook pack, used by Task 5's corpus entries.

- [ ] **Step 1: Add the metrics**

In `src/grain/domains/chinook/ontology.yaml`, in the `metrics:` block:

```yaml
  median_duration:
    grain: track
    agg: median
    value: "track.milliseconds"
    type: integer
    description: >
      The middle track duration, as a duration some track actually has —
      percentile_disc, not an interpolated midpoint.
    ai_context:
      synonyms: [median duration, typical length, middle track length]
      instructions: >
        A CATALOGUE statistic: it describes tracks, not sales. For the typical
        duration of tracks that SOLD, traverse Track_InvoiceLines first.

  p90_duration:
    grain: track
    agg: percentile
    percentile: 0.9
    value: "track.milliseconds"
    type: integer
    description: The duration below which 90% of tracks fall.
    ai_context:
      synonyms: [90th percentile duration, p90 length, long tracks]
```

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_order_statistic_anchors.py`:

```python
"""Order statistics, measured against the unfanned truth.

Every figure is compared with `percentile_disc` over the un-joined table, so a
wrong encoding cannot hide behind the engine agreeing with itself. The naive
figure must DIFFER from the truth or the test proves nothing — over
`track -> playlist_track` the median duration reads 256026 against a true
255634.
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec

pytestmark = pytest.mark.integration

FANNED = [Hop(link="Playlist_Tracks")]


@pytest.fixture(scope="module")
def engines(db_engine):
    return {n: Grain.load(CHINOOK_DIR, db_engine, engine_name=n)
            for n in ("subquery", "symmetric")}


def test_the_join_really_corrupts_the_median(db_engine):
    """The control. Without this the rest proves nothing."""
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.5) within group (order by milliseconds)"
            " from track")).scalar()
        naive = conn.execute(text(
            "select percentile_disc(0.5) within group (order by t.milliseconds)"
            " from track t join playlist_track pt on pt.track_id = t.track_id"
        )).scalar()
    assert truth == 255634
    assert naive != truth


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_median_over_a_fanning_join_is_exact(which, engines, db_engine):
    result = engines[which].query(QuerySpec(
        object="Playlist", traverse=FANNED, metrics=["median_duration"],
        limit=None))
    (got,) = [r[0] for r in result.rows]
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.5) within group (order by milliseconds)"
            " from track")).scalar()
    assert int(got) == int(truth)


@pytest.mark.parametrize("which", ["subquery", "symmetric"])
def test_p90_over_a_fanning_join_is_exact(which, engines, db_engine):
    result = engines[which].query(QuerySpec(
        object="Playlist", traverse=FANNED, metrics=["p90_duration"],
        limit=None))
    (got,) = [r[0] for r in result.rows]
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.9) within group (order by milliseconds)"
            " from track")).scalar()
    assert int(got) == int(truth)


def test_a_decimal_valued_order_statistic_is_exact(db_engine):
    """The case the integer-only design would have refused. unit_price is
    NUMERIC(10,2), and the scale comes from reflection, so nothing rounds."""
    from grain.engine.ontology import Metric

    g = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")
    g.ontology.metrics["median_price"] = Metric(
        name="median_price", grain="invoice_line", type="decimal",
        agg="median", value="invoice_line.unit_price")
    try:
        result = g.query(QuerySpec(
            object="InvoiceLine", metrics=["median_price"], limit=None))
    finally:
        del g.ontology.metrics["median_price"]
    with db_engine.connect() as conn:
        truth = conn.execute(text(
            "select percentile_disc(0.5) within group (order by unit_price)"
            " from invoice_line")).scalar()
    assert float(result.rows[0][0]) == float(truth)


def test_grouped_medians_match_group_by_group(engines, db_engine):
    """A single total can be right by luck; per-group figures cannot."""
    got = {r[0]: int(r[1]) for r in engines["symmetric"].query(QuerySpec(
        object="Album", traverse=[Hop(link="Album_Tracks")],
        group_by=["title"], metrics=["median_duration"], limit=None)).rows}
    with db_engine.connect() as conn:
        truth = {r[0]: int(r[1]) for r in conn.execute(text("""
            select a.title,
                   percentile_disc(0.5) within group (order by t.milliseconds)
            from album a join track t on t.album_id = a.album_id
            group by a.title"""))}
    assert got == truth


def test_the_symmetric_plan_emits_no_subquery(engines):
    sql = engines["symmetric"].explain(QuerySpec(
        object="Playlist", traverse=FANNED, metrics=["median_duration"],
        limit=None))["compiled_sql"]
    assert sql.lower().count("select") == 1
    assert "array_agg" in sql.lower()
```

- [ ] **Step 3: Run it**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/integration/test_order_statistic_anchors.py -q`
Expected: PASS. If a figure disagrees, the encoding is wrong — **do not adjust the expected value**, which is generated by the hand-written SQL in the test itself.

- [ ] **Step 4: Full suite, lint, commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools
git add src/grain/domains/chinook/ontology.yaml tests/integration/test_order_statistic_anchors.py
git commit -m "test: measured anchors for order statistics

Compared against percentile_disc on the unfanned table, over a join that shifts
the naive median from 255634 to 256026. Both engines, and a decimal-valued case
that the integer-only design would have refused."
```

---

## Task 5: differential coverage and the oracle

**Files:**
- Modify: `tests/corpus.py` — add order-statistic specs
- Modify: `tools/oracle.py` — order-statistic support
- Modify: `docs/plans/2026-09-05-order-statistics-design.md` — mark implemented

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: no new names. The differential harness and sweep cover order statistics.

- [ ] **Step 1: Add corpus entries**

In `tests/corpus.py`, append to `CORPUS`:

```python
    (
        "median-over-a-many-to-many",
        QuerySpec(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
                  group_by=["id"], metrics=["median_duration"], limit=None),
    ),
    (
        "p90-over-a-one-to-many",
        QuerySpec(object="Album", traverse=[Hop(link="Album_Tracks")],
                  group_by=["title"], metrics=["p90_duration"], limit=None),
    ),
```

- [ ] **Step 2: Run the differential harness**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest tests/integration/test_engine_agreement.py -q`
Expected: PASS. The two engines compute these by completely different routes — the subquery engine pre-aggregates and calls `percentile_disc`, the symmetric engine sorts an encoded array — so agreement is real evidence rather than a tautology.

**A disagreement here is a genuine bug in one of them. Investigate it; do not add the spec to `DIVERGENT` to get green.**

- [ ] **Step 3: Teach the oracle order statistics**

In `tools/oracle.py`, extend `METRICS` and the aggregation branch in `answer`:

```python
    "median_duration": ("track", "median", lambda r: r["milliseconds"]),
    "p90_duration": ("track", "percentile", lambda r: r["milliseconds"]),
```

and in `answer`, before the `count_distinct` branch:

```python
        if agg in ("median", "percentile"):
            # percentile_disc: the first value whose cumulative fraction
            # reaches p, over the DISTINCT grain rows. Computed here from raw
            # Python rows, sharing no SQL with either engine.
            values = sorted(value_of(r) for r in rows.values())
            p = 0.5 if agg == "median" else 0.9
            index = max(1, math.ceil(p * len(values)))
            out[key] = values[index - 1]
            continue
```

with `import math` at the top.

- [ ] **Step 4: Run the sweep**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" PYTHONPATH=tools uv run python tools/sweep.py`
Expected: the enumerated combinations grow, with both engines matching the oracle and no `WRONG` rows.

- [ ] **Step 5: Mark the design implemented and commit**

In `docs/plans/2026-09-05-order-statistics-design.md`, change the status line to `**Status:** implemented 2026-09-05.`

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools
git add tests/corpus.py tools/oracle.py docs/plans/2026-09-05-order-statistics-design.md
git commit -m "test: order statistics in the differential harness and the oracle

The engines compute these by completely different routes — one pre-aggregates
and calls percentile_disc, the other sorts an encoded array — so agreement is
evidence rather than tautology. The oracle computes them from raw Python rows,
sharing no SQL with either."
```

---

## Task 6: documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Update the README**

In the *Two engines* comparison table, change the metric-forms row so it no longer implies the symmetric engine cannot do order statistics, and add a row:

```markdown
| Median / percentile over a fan | via `expr`, pre-aggregated | **native** — `agg: median`, `agg: percentile` |
```

In *What neither engine can do*, delete the median row — both engines now answer it — and note in its place that `percentile_cont` remains a non-goal.

- [ ] **Step 2: Update CLAUDE.md**

Under *Two engines*, add: the symmetric engine now carries **two** encodings — the sum encoding for `sum`/`avg`, and the array encoding for `median`/`percentile`. State that the array encoding's `K = 1e19` is a **proof** (a bigint cannot exceed 9.22e18) where the sum encoding's `|v| < 5e29` is only a load-time check, so the two must not be confused when reasoning about drift.

- [ ] **Step 3: Verify and commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools
git add README.md CLAUDE.md
git commit -m "docs: record native order statistics in both engines"
```

---

## Task 7: record what was learned

**Files:**
- Modify: `docs/FINDINGS.md`

The findings document is written AS the work happens, not reconstructed at the
end — a difficulty is easiest to describe honestly while it is still costing you
something, and by the time it is fixed the reasoning that made it hard has
usually evaporated.

- [ ] **Step 1: Append what this feature taught**

For each entry: what was believed, what turned out to be true, and what it cost.
At minimum, this work produced four:

1. **A cited limitation was narrower than its wording.** "A median has no
   distinct-sum rewrite" is true and rules out THAT rewrite, not every encoding.
   Reading it as "medians cannot be made fan-out-safe" would have closed the
   question wrongly.
2. **The objection to decimals dissolved on inspection.** Scaling was rejected
   as reintroducing Looker's lossiness; Looker guesses a scale of 6, while the
   scale can be READ from reflection, at which point nothing rounds. The
   integer-only restriction was unnecessary and would have been a real limit.
3. **A bound from a type is worth more than a bound from data.** `K = 1e19`
   holds by proof because a bigint cannot exceed 9.22e18. The sum encoding's
   `|v| < 5e29` bounds a value nothing constrains, so it can only be checked at
   load and can drift afterwards. Same-looking constants, different guarantees.
4. **Eligibility must be read from the database, not the declaration.**
   `ValueType` has no float member, so a `double precision` column would be
   declared `decimal`; trusting that would admit an inexact column into an
   encoding that depends on exactness.

- [ ] **Step 2: Commit**

```bash
git add docs/FINDINGS.md
git commit -m "docs: what order statistics taught"
```

---

## Deferred, recorded so it is a choice rather than an omission

- **`percentile_cont`.** Reachable — take both middle elements and average — but a second specification, and speculative without a caller.
- **`median(a * b)`.** Refused in the symmetric engine because a scale cannot be read off an expression. The subquery engine answers it, and the error says so.
- **`tools/bench.py` order-statistic shape.** §7 of the spec predicts `array_agg(DISTINCT ...)` will be worse than the subquery engine because it materialises the distinct set per group rather than streaming. That prediction is currently unmeasured, and should not be quoted as fact until it is.
