# Stock Quantities and a Time Dimension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let grain express a quantity that sums across space but not across time — an inventory level, a balance, a headcount — and compute it by windowing to a period boundary rather than summing.

**Architecture:** Four moves. The `quantity` vocabulary is renamed and moves from `Property` to `Metric`, because headcount has no quantity column to hang it on. A property may declare itself a time axis with `time_grain`. A `stock` metric declares `over_time: {dimension, choice}` and the subquery engine compiles it as a window function inside a subquery. The symmetric engine refuses it, and the oracle gains stock support to replace the differential check that refusal costs.

**Tech Stack:** Python 3.12+ · SQLAlchemy 2.x (`select()` style only) · Pydantic v2 · psycopg 3 · pytest · ruff

**Spec:** `docs/plans/2026-09-06-stock-and-time-design.md` — read it first. This plan implements it and argues from it.

## Global Constraints

- **Vocabulary:** `quantity: flow | stock | value_per_unit`. `extensive` → `flow`; `rate` and `ratio` both → `value_per_unit`. No deprecation path, no alias mapping — a value that silently mapped from an old name to a new one is the quiet accommodation this codebase avoids.
- **`quantity` lives on `Metric`.** `Property.quantity` stays and is the source of *inference* for a `sum` over a bare column. **A metric's own declaration wins; the property is consulted only when the metric is silent.**
- **`over_time` is required when `quantity: stock` and refused otherwise** — the same conditional-field shape as `Metric.percentile`, validated the same way.
- **`choice: first | last`**, deliberately not MetricFlow's `min | max`.
- **The symmetric engine refuses `stock`** with `MetricNotSymmetric` naming the `subquery` engine. Its corpus entry goes in `DIVERGENT` with that reason.
- Every refusal is raised **before a connection is acquired**, except `GuardTripped`.
- Python 3.12+, line length 100, `ruff check src tests tools` must pass.
- Tests run with `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook"`. **461 passing** before this plan starts. Check the skip count, not the colour.
- `pyproject.toml` promotes `SAWarning` to an error. Do not weaken it.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/grain/engine/ontology.py` | `QuantityKind` renamed; `TimeGrain`; `Property.time_grain`; `Metric.quantity`, `Metric.over_time`, `OverTime` model + validator |
| `src/grain/engine/loader.py` | `_check_quantity_kinds` reads the metric first, property second; new `_check_time_dimensions` |
| `src/grain/engine/grain.py` | `MetricPlan.window` — the verdict that a stock needs windowing, and over which column |
| `src/grain/engine/compile.py` | `_window_to_boundary` — the subquery + window function |
| `src/grain/engine_symmetric/symmetric.py` | refuse `stock` in `require_eligible` |
| `src/grain/domains/chinook/inventory.sql` | **new** — the `daily_inventory` table and its seed rows |
| `tools/seed_inventory.py` | **new** — opt-in migration runner; nothing in `Grain.load` touches it |
| `src/grain/domains/chinook/ontology.yaml` | rename the four annotations; add `Inventory` and `inventory_level` |
| `tests/unit/test_quantity_kind.py` | rename throughout; metric-vs-property precedence |
| `tests/unit/test_stock.py` | **new** — declaration, validation, planning verdicts |
| `tests/integration/test_stock_anchors.py` | **new** — measured, against hand-computed end-of-period figures |
| `tools/oracle.py` | stock support, per spec §5 |

`inventory.sql` sits in the domain pack rather than in `tools/` because it *is* domain data; the runner sits in `tools/` because running it is an operation on someone's database and must never be a side effect of loading an ontology.

---

## Task 1: rename the vocabulary

**Files:**
- Modify: `src/grain/engine/ontology.py:19-21` (`QuantityKind`, `ACCUMULATES`)
- Modify: `src/grain/engine/loader.py` (`_check_quantity_kinds` message text)
- Modify: `src/grain/domains/chinook/ontology.yaml` (four annotations)
- Test: `tests/unit/test_quantity_kind.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `QuantityKind = Literal["flow", "stock", "value_per_unit"]`; `ACCUMULATES = frozenset({"flow"})`. Tasks 2–7 use these names.

- [ ] **Step 1: Update the tests first**

In `tests/unit/test_quantity_kind.py`, replace the vocabulary. The parametrised list becomes:

```python
@pytest.mark.parametrize("kind", ["flow", "stock", "value_per_unit"])
def test_the_three_declarable_kinds(kind):
    assert Property(column="track.unit_price", type="decimal",
                    quantity=kind).quantity == kind
```

Every `quantity="extensive"` becomes `quantity="flow"`; every `quantity="rate"` and `quantity="ratio"` becomes `quantity="value_per_unit"`. Delete `test_summing_a_ratio_is_refused` — it tested a name that no longer exists and `test_summing_a_rate_is_refused` (renamed below) covers the behaviour.

Rename `test_summing_a_rate_is_refused` to `test_summing_a_value_per_unit_is_refused` and update its assertion:

```python
def test_summing_a_value_per_unit_is_refused(lite_metadata):
    """The case this whole feature exists for."""
    with pytest.raises(OntologyError, match="does not accumulate"):
        validate(_onto(_sum("track.unit_price"), quantity="value_per_unit"),
                 lite_metadata)
```

Add one asserting the old names are gone, so the migration cannot be half-done:

```python
@pytest.mark.parametrize("old", ["extensive", "rate", "ratio"])
def test_the_old_vocabulary_is_refused(old):
    """No alias mapping. A value that silently mapped from an old name to a new
    one is the quiet accommodation this codebase avoids — the ontology author
    should see the error and choose."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Property(column="track.unit_price", type="decimal", quantity=old)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_quantity_kind.py -q`
Expected: FAIL — `flow`, `stock` and `value_per_unit` are not members of `QuantityKind`.

- [ ] **Step 3: Rename in the model**

In `src/grain/engine/ontology.py`:

```python
QuantityKind = Literal["flow", "stock", "value_per_unit"]

ACCUMULATES: frozenset[str] = frozenset({"flow"})
"""Kinds that may be summed.

A FLOW is measured over a period and accumulates -- money taken, units sold,
seconds elapsed. Adding two flows yields a flow, which is what makes a total
mean anything.

A STOCK is a level at an instant -- inventory on hand, an account balance, a
headcount. It sums across space (all warehouses) and NOT across time: adding
Monday's balance to Tuesday's yields a number no accountant recognises. See
`Metric.over_time`.

A VALUE_PER_UNIT is a rate or a proportion -- a unit price, an exchange rate, a
percentage. It accumulates over nothing.

The names are Lenz & Shoshani's (1997), not ours. An earlier version read
`extensive | rate | ratio`, invented before the literature was read: `extensive`
had no partner once `stock` arrived, and `rate` versus `ratio` was a distinction
nothing branched on. See `docs/QUANTITY-TYPES.md`.
"""
```

In `src/grain/engine/loader.py`, the message in `_check_quantity_kinds` currently reads *"so say which it is: extensive (money, counts, durations), rate (a price, a speed) or ratio (a percentage, a share)"*. Replace with:

```python
                f"{ctx}, but '{name}' does not declare a 'quantity'. Summing is "
                f"only meaningful for a quantity that accumulates, so say which "
                f"it is: flow (money, counts, durations), stock (a level at an "
                f"instant) or value_per_unit (a price, a rate, a percentage)."
```

- [ ] **Step 4: Rename the chinook annotations**

Four values in `src/grain/domains/chinook/ontology.yaml`:

```
track.unit_price         rate       -> value_per_unit
invoice.total            extensive  -> flow
invoice_line.unit_price  rate       -> value_per_unit
invoice_line.quantity    extensive  -> flow
```

Verify none were missed:

```bash
grep -n "extensive\|quantity: rate\|quantity: ratio" src/grain/domains/chinook/ontology.yaml || echo "clean"
```

Also update `tests/unit/conftest.py`, whose `TINY_YAML` declares `quantity: extensive` on `invoice.total` and `invoice_line.quantity`.

- [ ] **Step 5: Full suite and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`
Expected: all green. Every measured anchor must pass **unedited** — this is a rename, and a changed figure means something else moved.

```bash
git add -A
git commit -m "refactor: rename the quantity vocabulary to flow/stock/value_per_unit

Lenz & Shoshani's names, not ours. extensive had no partner once stock arrived,
and rate versus ratio was a distinction nothing in the codebase branched on —
the only use of either was interpolating the word into an error message.

No alias mapping, deliberately. A value that silently mapped from an old name to
a new one is the quiet accommodation this codebase avoids."
```

---

## Task 2: `quantity` on the metric, with inference from the property

**Files:**
- Modify: `src/grain/engine/ontology.py` (`Metric`)
- Modify: `src/grain/engine/loader.py` (`_check_quantity_kinds`)
- Test: `tests/unit/test_quantity_kind.py`

**Interfaces:**
- Consumes: `QuantityKind`, `ACCUMULATES` from Task 1.
- Produces: `Metric.quantity: QuantityKind | None`; `loader._effective_quantity(onto, metric) -> tuple[str | None, str]` returning the kind and a human-readable source (`"metric 'x'"` or `"property 'Obj.p'"`). Tasks 3–6 rely on both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_quantity_kind.py`:

```python
# -- the declaration site ----------------------------------------------------

def test_a_metric_may_declare_its_own_quantity(lite_metadata):
    """Headcount is the textbook stock and a column-level field cannot express
    it: count_distinct(employee_id) has no quantity column, because employee_id
    is an identifier."""
    m = Metric(name="headcount", grain="employee", type="integer",
               agg="count_distinct", value="employee.employee_id",
               quantity="stock", over_time={"dimension": "hired",
                                            "choice": "last"})
    assert m.quantity == "stock"


def test_a_metric_quantity_beats_the_property(lite_metadata):
    """Precedence is one-directional and stated: the metric is closer to the
    meaning of the number, so it wins. No error is raised on disagreement."""
    from grain.engine.loader import _effective_quantity

    onto = _onto(Metric(name="m", grain="track", type="decimal", agg="sum",
                        value="track.unit_price", quantity="flow"),
                 quantity="value_per_unit")
    kind, source = _effective_quantity(onto, onto.metrics["m"])
    assert kind == "flow"
    assert "metric" in source


def test_the_property_is_used_when_the_metric_is_silent(lite_metadata):
    """What keeps chinook's four existing annotations working."""
    from grain.engine.loader import _effective_quantity

    onto = _onto(_sum("track.unit_price"), quantity="value_per_unit")
    kind, source = _effective_quantity(onto, onto.metrics["m"])
    assert kind == "value_per_unit"
    assert "property" in source


def test_inference_still_refuses_the_original_case(lite_metadata):
    """The migration must not silently drop the guarantee Task 1 preserved: a
    sum over a bare column with no metric-level quantity is still refused when
    the property says value_per_unit."""
    with pytest.raises(OntologyError, match="does not accumulate"):
        validate(_onto(_sum("track.unit_price"), quantity="value_per_unit"),
                 lite_metadata)


def test_a_metric_level_declaration_works_without_any_property(lite_metadata):
    """`sum(track.album_id)` has no declared property, which used to be refused
    outright. A metric-level quantity now supplies what the column cannot."""
    m = Metric(name="m", grain="track", type="integer", agg="sum",
               value="track.album_id", quantity="flow")
    onto = _onto(m, quantity="flow")
    validate(onto, lite_metadata)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_quantity_kind.py -q -k "declaration or beats or silent or inference or without_any"`
Expected: FAIL — `Metric` has no `quantity` field and `loader` has no `_effective_quantity`.

- [ ] **Step 3: Add the field**

In `src/grain/engine/ontology.py`, on `Metric`, after `percentile`:

```python
    quantity: QuantityKind | None = None
    """What kind of number this metric produces.

    On the METRIC rather than only on a property, because stock-ness describes
    what the RESULT means and not what a column holds. Headcount is the textbook
    stock and `count_distinct(employee.employee_id)` reads no quantity column at
    all -- `employee_id` is an identifier.

    Omitted, it is INFERRED from the summed column's property where the metric
    is a `sum` over a bare column. A metric's own declaration always wins; see
    `loader._effective_quantity`.
    """
```

- [ ] **Step 4: Teach the loader to read both**

In `src/grain/engine/loader.py`, above `_check_quantity_kinds`:

```python
def _effective_quantity(onto: Ontology, metric: Metric) -> tuple[str | None, str]:
    """This metric's quantity kind, and where it came from.

    Precedence is one-directional: the METRIC wins. It is closer to the meaning
    of the number than any single column it reads, and a metric may legitimately
    produce a different kind from its inputs -- `revenue` is a value_per_unit
    times a flow and is itself a flow.

    A disagreement is NOT an error. That is a deliberate choice and a debatable
    one: the alternative is refusing at load the way conflicting `expr`/`agg`
    declarations are refused. It is allowed here because the metric is
    authoritative rather than merely later, so there is nothing for the author to
    resolve.

    The returned source string is for error messages, so a refusal can name
    where the offending declaration actually lives.
    """
    if metric.quantity is not None:
        return metric.quantity, f"metric '{metric.name}'"
    if metric.agg != "sum" or not metric.value:
        return None, f"metric '{metric.name}'"
    match = BARE_COLUMN.match(metric.value)
    if match is None:
        return None, f"metric '{metric.name}'"
    name, prop = _property_for_column(onto, match.group(1), match.group(2))
    if prop is None or prop.quantity is None:
        return None, f"metric '{metric.name}'"
    return prop.quantity, f"property '{name}'"
```

Then rewrite `_check_quantity_kinds` to use it. The rule is unchanged — only where the kind comes from changes:

```python
def _check_quantity_kinds(onto: Ontology) -> None:
    """A quantity that does not accumulate may not be summed.

    NARROW ON PURPOSE, unchanged from before: only a summed value that is a BARE
    column reference is inspected when the kind must be inferred. `sum(a * b)`
    is left alone, because a rate multiplied by a count genuinely IS a flow --
    `revenue` is `sum(unit_price * quantity)`, exactly that shape, and a cruder
    rule would refuse grain's flagship metric.

    A metric that declares its own `quantity` is checked whatever its value's
    shape, because the author has stated the kind rather than leaving it to be
    read off a column.
    """
    for metric in onto.metrics.values():
        if metric.agg != "sum":
            continue
        kind, source = _effective_quantity(onto, metric)
        if kind is None:
            # Only demand a declaration where one could have been inferred --
            # a bare column. Anything else was never covered by this rule.
            if not metric.value or BARE_COLUMN.match(metric.value) is None:
                continue
            table, column = BARE_COLUMN.match(metric.value).groups()
            ctx = f"metric '{metric.name}' sums '{table}.{column}'"
            name, prop = _property_for_column(onto, table, column)
            if prop is None:
                raise OntologyError(
                    f"{ctx}, which has no declared property, so there is "
                    f"nowhere to say whether that quantity accumulates. Declare "
                    f"a property for it with an explicit 'quantity', or set "
                    f"'quantity' on the metric."
                )
            raise OntologyError(
                f"{ctx}, but '{name}' does not declare a 'quantity'. Summing is "
                f"only meaningful for a quantity that accumulates, so say which "
                f"it is: flow (money, counts, durations), stock (a level at an "
                f"instant) or value_per_unit (a price, a rate, a percentage)."
            )
        if kind not in ACCUMULATES:
            raise OntologyError(
                f"metric '{metric.name}' sums a quantity that {source} declares "
                f"a {kind}. A {kind} does not accumulate -- summing it produces "
                f"a number with no referent, however correct the arithmetic. "
                f"Alternatives: use agg avg, min or max; or measure a flow "
                f"instead."
            )
```

**Note the behaviour change this introduces deliberately:** a `stock` metric is now refused by this check, because `stock` is not in `ACCUMULATES`. Task 4 is what makes a stock summable — by windowing it so the sum never crosses time. Until Task 4 lands, declaring a stock and summing it is refused, which is correct at that point in the sequence.

- [ ] **Step 5: Run and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

```bash
git add -A
git commit -m "feat: quantity may be declared on a metric, inferred from a property

Stock-ness describes what a metric's RESULT means, not what a column holds:
headcount is the textbook stock and count_distinct(employee_id) reads no
quantity column at all.

Precedence is one-directional and the metric wins, because it is closer to the
meaning of the number and may legitimately produce a different kind from its
inputs. A disagreement is deliberately not an error — debatable, and recorded as
such in the docstring."
```

---

## Task 3: the time dimension

**Files:**
- Modify: `src/grain/engine/ontology.py` (`TimeGrain`, `Property.time_grain`)
- Modify: `src/grain/engine/loader.py` (`_check_time_dimensions`, called from `validate`)
- Test: `tests/unit/test_stock.py` (create)

**Interfaces:**
- Consumes: nothing from Tasks 1–2.
- Produces: `TimeGrain = Literal["day", "week", "month", "quarter", "year"]`; `Property.time_grain: TimeGrain | None`; `loader._check_time_dimensions(onto, metadata)`. Task 4 relies on `time_grain` being present and verified.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_stock.py`:

```python
"""A time dimension, and a quantity that does not sum across it.

grain has `type: date` and `type: datetime` but nothing marking a property as
THE time axis. `time_grain` does that, and a `stock` metric names it in
`over_time`.
"""
import pytest
from pydantic import ValidationError

from grain.engine.errors import OntologyError
from grain.engine.loader import validate
from grain.engine.ontology import Metric, ObjectType, Ontology, Property


def _time_onto(metric: Metric | None = None, grain: str | None = "day",
               column: str = "invoice.invoice_date") -> Ontology:
    props = {
        "when": Property(column=column, type="datetime", time_grain=grain),
        "total": Property(column="invoice.total", type="decimal",
                          quantity="flow"),
    }
    return Ontology(
        name="t",
        objects={"Invoice": ObjectType(name="Invoice", primary="invoice",
                                       properties=props)},
        metrics={metric.name: metric} if metric else {},
    )


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_the_five_declarable_grains(grain):
    assert Property(column="invoice.invoice_date", type="datetime",
                    time_grain=grain).time_grain == grain


def test_an_unknown_grain_is_refused():
    with pytest.raises(ValidationError):
        Property(column="invoice.invoice_date", type="datetime",
                 time_grain="fortnight")


def test_time_grain_is_optional():
    """Most properties are not a time axis and should need nothing said."""
    assert Property(column="invoice.total", type="decimal").time_grain is None


def test_a_time_grain_on_a_temporal_column_is_accepted(lite_metadata):
    validate(_time_onto(), lite_metadata)


def test_a_time_grain_on_a_non_temporal_column_is_refused(lite_metadata):
    """The whole point is that ordering and comparison are meaningful, which a
    text column cannot promise. Checked against the REFLECTED type, not the
    declared one, for the same reason order statistics are."""
    with pytest.raises(OntologyError, match="not a date or timestamp"):
        validate(_time_onto(column="invoice.customer_id"), lite_metadata)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_stock.py -q`
Expected: FAIL — `Property` has no `time_grain`.

- [ ] **Step 3: Add the field**

In `src/grain/engine/ontology.py`, beside `QuantityKind`:

```python
TimeGrain = Literal["day", "week", "month", "quarter", "year"]
"""The granularity at which a time axis is recorded.

Declared, not inferred: a `date` column may hold daily snapshots or
month-end ones, and nothing in the type says which.

v1 records it and does not yet re-bucket by it -- grain has no `date_trunc`, so
grouping by month is not expressible. It is declared now so the granularity
work has somewhere to attach, and the loader verifies it against the column's
reflected type so it is checked even while its granularity meaning is unused.
"""
```

On `Property`, after `quantity`:

```python
    time_grain: TimeGrain | None = None
    """Set to make this property THE time axis of its object.

    A `stock` metric names it in `over_time` to say which dimension it must not
    be summed across.
    """
```

- [ ] **Step 4: Verify it against the database**

In `src/grain/engine/loader.py`, add and call from `validate` (beside the `_check_quantity_kinds` call):

```python
def _check_time_dimensions(onto: Ontology, metadata: MetaData) -> None:
    """A `time_grain` must sit on a column the database agrees is temporal.

    Checked against the REFLECTED type rather than the declared `type`, for the
    same reason order-statistic eligibility is: a declaration can be wrong, and
    the consequence here is a window over a column whose ordering means nothing.
    """
    from sqlalchemy import Date, DateTime

    for obj in onto.objects.values():
        for prop_name, prop in obj.properties.items():
            if prop.time_grain is None:
                continue
            ctx = f"object '{obj.name}' property '{prop_name}'"
            _require_column(metadata, prop.column, ctx)
            column = metadata.tables[prop.column.table].columns[
                prop.column.column]
            if not isinstance(column.type, (Date, DateTime)):
                raise OntologyError(
                    f"{ctx} declares time_grain '{prop.time_grain}' but "
                    f"'{prop.column.qualified}' is {column.type}, not a date or "
                    f"timestamp. A time axis has to order and compare "
                    f"meaningfully."
                )
```

- [ ] **Step 5: Run and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

```bash
git add -A
git commit -m "feat: a property may declare itself a time axis

time_grain marks the time dimension and records the granularity the data is
recorded at. Verified against the column's REFLECTED type, not its declared
one — a declaration can be wrong, and the consequence is a window over a column
whose ordering means nothing.

v1 records the granularity without re-bucketing by it; grain has no date_trunc,
so grouping by month is not expressible yet."
```

---

## Task 4: `over_time`, and the planning verdict

**Files:**
- Modify: `src/grain/engine/ontology.py` (`OverTime`, `Metric.over_time`, validator)
- Modify: `src/grain/engine/loader.py` (validate `over_time` against the ontology)
- Modify: `src/grain/engine/grain.py` (`MetricPlan.window`)
- Test: `tests/unit/test_stock.py`

**Interfaces:**
- Consumes: `TimeGrain`, `Property.time_grain` from Task 3; `Metric.quantity` from Task 2.
- Produces: `OverTime(dimension: str, choice: Literal["first","last"])`; `Metric.over_time: OverTime | None`; `MetricPlan.window: WindowSpec | None` where `WindowSpec` is a frozen dataclass `(column: ColumnRef, choice: str)`. Task 5 compiles from `MetricPlan.window`; Task 6 refuses on `Metric.over_time`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_stock.py`:

```python
# -- over_time ---------------------------------------------------------------

def _stock(choice="last", dimension="when") -> Metric:
    return Metric(name="level", grain="invoice", type="decimal", agg="sum",
                  value="invoice.total", quantity="stock",
                  over_time={"dimension": dimension, "choice": choice})


@pytest.mark.parametrize("choice", ["first", "last"])
def test_both_choices_are_legal(choice):
    assert _stock(choice=choice).over_time.choice == choice


def test_min_and_max_are_not_the_vocabulary():
    """MetricFlow spells these min|max. `window_choice: max` reads as the
    largest VALUE when it means the value at the latest DATE — two readings, one
    wrong, in a field whose whole job is to disambiguate."""
    with pytest.raises(ValidationError):
        _stock(choice="max")


def test_a_stock_requires_over_time():
    with pytest.raises(ValidationError, match="over_time"):
        Metric(name="level", grain="invoice", type="decimal", agg="sum",
               value="invoice.total", quantity="stock")


def test_over_time_is_refused_on_a_flow():
    """A field meaningful for one quantity kind must be rejected elsewhere, or
    it reads as configuration that silently does nothing."""
    with pytest.raises(ValidationError, match="over_time"):
        Metric(name="rev", grain="invoice", type="decimal", agg="sum",
               value="invoice.total", quantity="flow",
               over_time={"dimension": "when", "choice": "last"})


def test_the_named_dimension_must_exist(lite_metadata):
    with pytest.raises(OntologyError, match="no property"):
        validate(_time_onto(_stock(dimension="nope")), lite_metadata)


def test_the_named_dimension_must_declare_a_time_grain(lite_metadata):
    """Naming a non-temporal property would window over something with no
    meaningful order."""
    with pytest.raises(OntologyError, match="time_grain"):
        validate(_time_onto(_stock(dimension="total")), lite_metadata)


def test_a_well_formed_stock_loads(lite_metadata):
    validate(_time_onto(_stock()), lite_metadata)


def test_a_stock_is_no_longer_refused_for_not_accumulating(lite_metadata):
    """Task 2 left `stock` outside ACCUMULATES, so summing one was refused.
    `over_time` is what makes it summable — the window means the sum never
    crosses time."""
    validate(_time_onto(_stock()), lite_metadata)


# -- the planning verdict ----------------------------------------------------

def test_the_plan_carries_the_window(lite_metadata, chinook_lite):
    """Decided with the other verdicts rather than recomputed in compile, for
    the same reason subquery_edges is: a window applied over a different column
    from the one the analysis reasoned about would silently invalidate it."""
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock())
    plan = analyse(resolve(QuerySpec(object="Invoice", metrics=["level"]), onto))
    (mp,) = plan.metric_plans
    assert mp.window is not None
    assert mp.window.choice == "last"
    assert mp.window.column.qualified == "invoice.invoice_date"


def test_a_flow_carries_no_window(chinook_lite):
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    plan = analyse(resolve(
        QuerySpec(object="Invoice", metrics=["invoice_total"]), chinook_lite))
    (mp,) = plan.metric_plans
    assert mp.window is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_stock.py -q`
Expected: FAIL — no `OverTime`, no `Metric.over_time`, no `MetricPlan.window`.

- [ ] **Step 3: Add the model**

In `src/grain/engine/ontology.py`, above `Metric`:

```python
class OverTime(BaseModel):
    """How a `stock` collapses across time.

    `first | last` rather than MetricFlow's `min | max`: `window_choice: max`
    reads as "the largest value" when it means "the value at the latest date".
    """

    model_config = ConfigDict(extra="forbid")
    dimension: str
    choice: Literal["first", "last"] = "last"
```

On `Metric`, after `quantity`:

```python
    over_time: OverTime | None = None
    """Required when `quantity: stock`, refused otherwise.

    A stock sums across space and not across time, so it needs to know WHICH
    dimension is time before it can be aggregated at all.
    """
```

And a validator beside `_check_percentile`:

```python
    @model_validator(mode="after")
    def _check_over_time(self) -> "Metric":
        if self.quantity == "stock":
            if self.over_time is None:
                raise ValueError(
                    f"metric '{self.name}' is a stock but sets no 'over_time'. "
                    f"A stock does not sum across time, so it has to name which "
                    f"dimension time is."
                )
        elif self.over_time is not None:
            raise ValueError(
                f"metric '{self.name}' sets 'over_time' but its quantity is "
                f"'{self.quantity}', where collapsing across time has no "
                f"meaning. Remove it, or declare quantity: stock."
            )
        return self
```

- [ ] **Step 4: Validate it against the ontology**

In `_check_time_dimensions` (Task 3), append after the property loop:

```python
    for metric in onto.metrics.values():
        if metric.over_time is None:
            continue
        obj = onto.object_for_table(metric.grain)
        ctx = f"metric '{metric.name}' over_time"
        if obj is None:
            raise OntologyError(
                f"{ctx} names dimension '{metric.over_time.dimension}' but "
                f"grain '{metric.grain}' is not the primary table of any "
                f"declared object, so it has no properties to name."
            )
        prop = obj.properties.get(metric.over_time.dimension)
        if prop is None:
            raise OntologyError(
                f"{ctx} names '{metric.over_time.dimension}', which is no "
                f"property of {obj.name}. Declared: "
                f"{sorted(obj.properties)}."
            )
        if prop.time_grain is None:
            raise OntologyError(
                f"{ctx} names '{metric.over_time.dimension}', which declares no "
                f"time_grain. A stock collapses across TIME, so the dimension "
                f"it names has to be a time axis."
            )
```

Also allow a stock past `_check_quantity_kinds`. In that function, before the `ACCUMULATES` check:

```python
        if kind == "stock":
            # A stock IS summable -- across space. `over_time` guarantees the
            # sum never crosses time, because the window collapses to one
            # instant before the aggregate runs. Validated above.
            continue
```

- [ ] **Step 5: Carry the verdict on the plan**

In `src/grain/engine/grain.py`, above `MetricPlan`:

```python
@dataclass(frozen=True)
class WindowSpec:
    """Collapse to one instant before aggregating. See `OverTime`."""

    column: ColumnRef
    choice: str
```

Add to `MetricPlan`:

```python
    # A stock's window, resolved to the actual column. Decided here with the
    # other verdicts rather than recomputed in `compile`, for the same reason
    # `subquery_edges` is: a window applied over a different column from the one
    # this analysis reasoned about would silently invalidate the verdict.
    window: WindowSpec | None = None
```

And in `analyse`, where the `MetricPlan` is constructed, compute it:

```python
        window = None
        if metric.over_time is not None:
            obj = rq.ontology.object_for_table(metric.grain)
            prop = obj.properties[metric.over_time.dimension]
            window = WindowSpec(column=prop.column,
                                choice=metric.over_time.choice)
```

then pass `window=window` to every `MetricPlan(...)` construction in the function — there are two, the immune fast path and the main one.

Import `ColumnRef` from `.ontology` at the top of `grain.py`.

- [ ] **Step 6: Run and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

```bash
git add -A
git commit -m "feat: over_time declares how a stock collapses across time

first|last, not MetricFlow's min|max — 'window_choice: max' reads as the largest
VALUE when it means the value at the latest DATE.

Required for a stock and refused elsewhere, the same conditional-field shape as
percentile. The named dimension must be a property of the metric's own object
and must declare a time_grain, both checked at load.

The window is resolved to a real column in analyse and carried on MetricPlan,
not recomputed in compile — a window over a different column from the one the
analysis reasoned about would silently invalidate the verdict."
```

---

## Task 5: compile the window

**Files:**
- Modify: `src/grain/engine/compile.py`
- Test: `tests/unit/test_stock.py`

**Interfaces:**
- Consumes: `MetricPlan.window` from Task 4.
- Produces: `_window_to_boundary(stmt, scope, metadata, rq, mp) -> Select[Any]`. Task 7's anchors assert its output numerically.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_stock.py`:

```python
def test_the_emitted_sql_windows_before_aggregating(lite_metadata):
    """The shape: a window function inside a subquery, then a filter to the
    picked instant, then the aggregate. Summing across time is impossible by
    construction rather than by a check."""
    from grain.engine.compile import compile_query, sql_text
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock())
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    sql = sql_text(compile_query(rq, analyse(rq), lite_metadata)).lower()
    assert "over (" in sql, "needs a window function"
    assert "max(" in sql, "last means the maximum instant"
    assert "sum(" in sql


def test_choice_first_uses_min(lite_metadata):
    from grain.engine.compile import compile_query, sql_text
    from grain.engine.grain import analyse
    from grain.engine.resolve import resolve
    from grain.engine.spec import QuerySpec

    onto = _time_onto(_stock(choice="first"))
    rq = resolve(QuerySpec(object="Invoice", metrics=["level"]), onto)
    sql = sql_text(compile_query(rq, analyse(rq), lite_metadata)).lower()
    assert "min(" in sql
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_stock.py -q -k "windows_before or choice_first"`
Expected: FAIL — no window appears; the metric compiles as a plain `sum`.

- [ ] **Step 3: Implement**

In `src/grain/engine/compile.py`, add above `compile_query`:

```python
def _window_to_boundary(
    stmt: Select[Any],
    scope: Scope,
    metadata: MetaData,
    rq: ResolvedQuery,
    mp: MetricPlan,
) -> Select[Any]:
    """Restrict a stock to one instant per group, then let the aggregate run.

    A stock sums across space and not across time. Rather than checking that
    nobody sums across time, the window makes it impossible: within each group
    only the rows at the first/last instant survive, so there is one instant to
    sum over.

        select key, sum(v) from (
          select *, max(t) over (partition by key) as pick from ...
        ) s where t = pick group by key

    The partition is the query's own group keys, so the three cases in the
    design all fall out of one construction: no group_by partitions over the
    whole population (the global instant), a non-time key partitions per group
    (each group's own instant), and grouping by the time dimension itself gives
    each group one instant already, making the window a no-op.

    A subquery is unavoidable -- a window function cannot be referenced from the
    WHERE of the select that computes it. That is why the symmetric engine
    refuses a stock outright rather than growing a special case.
    """
    assert mp.window is not None
    time_col = _column(metadata, mp.window.column.table, mp.window.column.column)
    pick = func.max if mp.window.choice == "last" else func.min
    partition = [scope.column(rp) for rp in rq.group_by]

    inner = stmt.add_columns(
        time_col.label("_grain_t"),
        pick(time_col).over(partition_by=partition).label("_grain_pick"),
    ).subquery(name=f"{mp.metric.name}_at_boundary")
    return select(inner).where(inner.c._grain_t == inner.c._grain_pick)
```

Add `func` to the SQLAlchemy imports if absent.

Then in `compile_query`, after `stmt = _apply_filters(...)` and before columns are selected, apply it for any windowed metric:

```python
    windowed = [mp for mp in plan.metric_plans if mp.window is not None]
    if windowed:
        if len(windowed) > 1:
            # Two stocks would need two partitions of the same rows, and the
            # second window's filter would apply to rows the first already
            # dropped. Refused rather than silently answering one of them.
            raise GrainError(
                f"a query may window at most one stock metric; asked for "
                f"{sorted(mp.metric.name for mp in windowed)}.",
                ["ask for them in separate queries"],
            )
        stmt = _window_to_boundary(stmt, scope, metadata, rq, windowed[0])
```

- [ ] **Step 4: Run and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

If the emitted SQL fails to compile because `scope.column` cannot resolve a group key against the wrapped subquery, the columns must be re-labelled through `inner.c[...]` — the group keys are already labelled by `rp.name` in `compile_query`, so read them back by that name rather than through `Scope`.

```bash
git add -A
git commit -m "feat: window a stock to one instant before aggregating

Rather than checking that nobody sums a stock across time, the window makes it
impossible: within each group only the rows at the first/last instant survive.

Partitioned by the query's own group keys, so all three cases fall out of one
construction — no group_by gives the global instant, a non-time key gives each
group its own, and grouping by the time dimension itself makes the window a
no-op because each group already holds one instant.

Two windowed stocks in one query are refused: the second window's filter would
apply to rows the first had already dropped."
```

---

## Task 6: the symmetric engine refuses stock

**Files:**
- Modify: `src/grain/engine_symmetric/symmetric.py` (`require_eligible`)
- Test: `tests/unit/test_stock.py`

**Interfaces:**
- Consumes: `Metric.over_time` from Task 4.
- Produces: no new names.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_stock.py`:

```python
def test_the_symmetric_engine_refuses_a_stock(lite_metadata):
    """Stated in the design rather than discovered. The window needs a window
    function inside a subquery, and that engine is one pass over the join.

    The cost is real: the differential harness cannot cross-check stock, so the
    oracle is the only independent judge for it."""
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import require_eligible

    with pytest.raises(MetricNotSymmetric, match="subquery"):
        require_eligible(_stock(), lite_metadata)


def test_the_refusal_explains_why(lite_metadata):
    from grain.engine.errors import MetricNotSymmetric
    from grain.engine_symmetric.symmetric import require_eligible

    with pytest.raises(MetricNotSymmetric) as exc:
        require_eligible(_stock(), lite_metadata)
    assert "window" in str(exc.value).lower()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_stock.py -q -k symmetric`
Expected: FAIL — `require_eligible` accepts it; `sum` over a bare column is eligible as far as it knows.

- [ ] **Step 3: Implement**

In `src/grain/engine_symmetric/symmetric.py`, in `require_eligible`, immediately after the `is_structured` check:

```python
    if metric.over_time is not None:
        raise MetricNotSymmetric(
            metric.name,
            "collapsing a stock to one instant needs a window function inside a "
            "subquery, and this engine is one pass over the join",
        )
```

- [ ] **Step 4: Run and commit**

Run: `GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools`

```bash
git add -A
git commit -m "feat: the symmetric engine refuses a stock, explaining why

The window needs a window function inside a subquery and that engine is one
pass over the join. The second such asymmetry after opaque expr metrics, and
stated in the design rather than discovered.

The cost is that the differential harness cannot cross-check stock. After the
pre-aggregate defect that harness found, an unverifiable path is a real cost —
Task 7 gives the oracle stock support as the mitigation."
```

---

## Task 7: real data, measured anchors, and the oracle

**Files:**
- Create: `src/grain/domains/chinook/inventory.sql`
- Create: `tools/seed_inventory.py`
- Modify: `src/grain/domains/chinook/ontology.yaml`
- Create: `tests/integration/test_stock_anchors.py`
- Modify: `tools/oracle.py`, `tests/corpus.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: `daily_inventory` table; `Inventory` object and `inventory_level` metric in the chinook pack.

- [ ] **Step 1: Write the table and seed**

Create `src/grain/domains/chinook/inventory.sql`. Deliberately small enough to compute by hand, and it seeds a **tie on the maximum date** because a windowing bug that only shows when two rows share the boundary instant would otherwise hide (design §9.3):

```sql
-- A stock: units on hand for a few tracks, snapshotted daily.
-- chinook ships no level or balance column, so this is added to give `stock`
-- real data to be measured against. Small on purpose: every figure the tests
-- assert is computable by hand from these rows.
CREATE TABLE IF NOT EXISTS daily_inventory (
    track_id     integer NOT NULL REFERENCES track (track_id),
    as_of_date   date    NOT NULL,
    units_on_hand integer NOT NULL,
    PRIMARY KEY (track_id, as_of_date)
);

TRUNCATE daily_inventory;

INSERT INTO daily_inventory (track_id, as_of_date, units_on_hand) VALUES
  -- track 1: rises then falls. Latest (03) = 4, earliest (01) = 10.
  (1, '2026-01-01', 10), (1, '2026-01-02', 25), (1, '2026-01-03', 4),
  -- track 2: latest (03) = 7, earliest (01) = 3.
  (2, '2026-01-01', 3),  (2, '2026-01-02', 5),  (2, '2026-01-03', 7),
  -- track 3: TIE on the latest date with tracks 1 and 2, so the window must
  -- keep BOTH rows at that instant rather than picking one.
  (3, '2026-01-03', 100),
  -- track 4: only ever recorded earlier, so it is absent at the latest instant
  -- and must NOT contribute to a `last` window.
  (4, '2026-01-01', 999);

-- Hand-computed: sum of units at the LAST instant (2026-01-03) = 4 + 7 + 100
--                = 111.  Sum at the FIRST instant (2026-01-01) = 10 + 3 + 999
--                = 1012.  Naive sum across all dates = 1153.
```

- [ ] **Step 2: Write the runner**

Create `tools/seed_inventory.py`:

```python
"""Create and seed `daily_inventory` in the chinook database.

OPT-IN AND SEPARATE FROM LOADING, deliberately. Until now a domain pack only
DESCRIBED a database it did not own; shipping this SQL makes the pack a producer
of schema. Running it is an operation on someone's database and must never be a
side effect of `Grain.load`.

    GRAIN_DATABASE_URL=... uv run python tools/seed_inventory.py
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from grain.domains.chinook import CHINOOK_DIR


def main() -> int:
    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        print("GRAIN_DATABASE_URL is not set")
        return 2
    sql = (CHINOOK_DIR / "inventory.sql").read_text(encoding="utf-8")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(sql))
    with engine.connect() as conn:
        n = conn.execute(text("select count(*) from daily_inventory")).scalar()
    print(f"daily_inventory seeded: {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run it:

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run python tools/seed_inventory.py
```

Expected: `daily_inventory seeded: 8 rows`

- [ ] **Step 3: Declare it in the ontology**

In `src/grain/domains/chinook/ontology.yaml`, under `objects:`:

```yaml
  Inventory:
    primary: daily_inventory
    description: >
      Units on hand per track, snapshotted daily. A STOCK: it sums across
      tracks and not across dates.
    properties:
      as_of: {column: daily_inventory.as_of_date, type: date, time_grain: day}
      units: {column: daily_inventory.units_on_hand, type: integer, quantity: stock}
```

and under `metrics:`:

```yaml
  inventory_level:
    grain: daily_inventory
    agg: sum
    value: "daily_inventory.units_on_hand"
    quantity: stock
    over_time: {dimension: as_of, choice: last}
    type: integer
    description: >
      Units on hand at the most recent snapshot. Never summed across dates —
      the window collapses to one instant first.
    ai_context:
      synonyms: [inventory, stock on hand, units available, current inventory]
      instructions: >
        A level, not a total. Asking for it "over January" gives the level at
        the end of January, not the sum of January's daily levels.
```

- [ ] **Step 4: Write the measured anchors**

Create `tests/integration/test_stock_anchors.py`:

```python
"""A stock, measured against hand-computed figures.

Every number here is derivable by eye from `inventory.sql`. Requires that seed:
    GRAIN_DATABASE_URL=... uv run python tools/seed_inventory.py
"""
import pytest
from sqlalchemy import text

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.spec import QuerySpec

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def g(db_engine):
    with db_engine.connect() as conn:
        seeded = conn.execute(text(
            "select count(*) from information_schema.tables"
            " where table_name = 'daily_inventory'")).scalar()
    if not seeded:
        pytest.skip("run tools/seed_inventory.py first")
    return Grain.load(CHINOOK_DIR, db_engine, engine_name="subquery")


def test_the_naive_sum_differs_from_the_level(db_engine):
    """The control. Summing across dates gives 1153; the level is 111."""
    with db_engine.connect() as conn:
        naive = conn.execute(text(
            "select sum(units_on_hand) from daily_inventory")).scalar()
        level = conn.execute(text("""
            select sum(units_on_hand) from daily_inventory
            where as_of_date = (select max(as_of_date) from daily_inventory)
        """)).scalar()
    assert naive == 1153
    assert level == 111


def test_the_global_level_is_the_latest_instant(g):
    """No group_by: the window partitions over the whole population, so this is
    the level at 2026-01-03 = 4 + 7 + 100."""
    result = g.query(QuerySpec(object="Inventory", metrics=["inventory_level"],
                               limit=None))
    assert int(result.rows[0][0]) == 111


def test_a_track_absent_at_the_boundary_does_not_contribute(g):
    """Track 4 was last recorded on 01-01. A `last` window must drop it, which
    is why the global level is 111 and not 1110."""
    result = g.query(QuerySpec(object="Inventory", metrics=["inventory_level"],
                               limit=None))
    assert int(result.rows[0][0]) != 1110


def test_grouping_by_the_time_dimension_makes_the_window_a_no_op(g):
    """Each group holds one instant already, so every date's own total appears:
    01-01 = 10+3+999 = 1012, 01-02 = 25+5 = 30, 01-03 = 4+7+100 = 111."""
    rows = {str(r[0]): int(r[1]) for r in g.query(QuerySpec(
        object="Inventory", group_by=["as_of"], metrics=["inventory_level"],
        limit=None)).rows}
    assert rows["2026-01-01"] == 1012
    assert rows["2026-01-02"] == 30
    assert rows["2026-01-03"] == 111


def test_choice_first_windows_to_the_earliest_instant(db_engine, g):
    """Same construction, other end: 01-01 = 1012."""
    from grain.engine.ontology import Metric, OverTime

    g.ontology.metrics["opening_level"] = Metric(
        name="opening_level", grain="daily_inventory", type="integer",
        agg="sum", value="daily_inventory.units_on_hand", quantity="stock",
        over_time=OverTime(dimension="as_of", choice="first"))
    try:
        result = g.query(QuerySpec(object="Inventory",
                                   metrics=["opening_level"], limit=None))
    finally:
        del g.ontology.metrics["opening_level"]
    assert int(result.rows[0][0]) == 1012


def test_two_windowed_stocks_in_one_query_are_refused(g):
    """The second window's filter would apply to rows the first had dropped."""
    from grain.engine.errors import GrainError
    from grain.engine.ontology import Metric, OverTime

    g.ontology.metrics["opening_level"] = Metric(
        name="opening_level", grain="daily_inventory", type="integer",
        agg="sum", value="daily_inventory.units_on_hand", quantity="stock",
        over_time=OverTime(dimension="as_of", choice="first"))
    try:
        with pytest.raises(GrainError, match="at most one stock"):
            g.query(QuerySpec(object="Inventory",
                              metrics=["inventory_level", "opening_level"],
                              limit=None))
    finally:
        del g.ontology.metrics["opening_level"]


def test_the_symmetric_engine_refuses_it_end_to_end(db_engine, g):
    from grain.engine.errors import MetricNotSymmetric

    sym = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")
    with pytest.raises(MetricNotSymmetric, match="subquery"):
        sym.query(QuerySpec(object="Inventory", metrics=["inventory_level"],
                            limit=None))
```

- [ ] **Step 5: Teach the oracle, and record the divergence**

In `tools/oracle.py`, add to `OBJECT_TABLE` and `PK`:

```python
    "Inventory": "daily_inventory",
```
**The oracle dedups on ONE primary-key column** — `groups[key][row[PK[grain]]]`
— and `daily_inventory`'s key is composite `(track_id, as_of_date)`. Registering
`track_id` alone would collapse every date into one row per track and quietly
destroy the thing being tested. So `PK` must accept a tuple, and the dedup key
must be built from all of its columns:

```python
# PK gains a composite entry
PK = {
    ...,
    "daily_inventory": ("track_id", "as_of_date"),
}
```

and in `answer`, where the grain row is recorded:

```python
        keyspec = PK[grain]
        columns = keyspec if isinstance(keyspec, tuple) else (keyspec,)
        row_id = tuple(tup[grain][c] for c in columns)
        groups[key][row_id] = tup[grain]
```

Every existing entry stays a plain string and takes the one-element path, so no
other metric changes behaviour.

and to `METRICS`:

```python
    "inventory_level": ("daily_inventory", "stock_last",
                        lambda r: r["units_on_hand"]),
```

with a branch in `answer`, before the `count_distinct` branch:

```python
        if agg in ("stock_last", "stock_first"):
            # Collapse to one instant, THEN sum -- the specification, stated
            # directly. Shares no SQL with either engine.
            pick = max if agg == "stock_last" else min
            boundary = pick(r["as_of_date"] for r in rows.values())
            out[key] = sum(value_of(r) for r in rows.values()
                           if r["as_of_date"] == boundary)
            continue
```

In `tests/corpus.py`, add the stock spec to **`DIVERGENT`**, not `CORPUS`, with the reason:

```python
    (
        "stock-windowed-to-the-latest-instant",
        QuerySpec(object="Inventory", metrics=["inventory_level"], limit=None),
        # The symmetric engine refuses this: the window needs a window function
        # inside a subquery and that engine is one pass. So the two cannot be
        # compared, and tools/oracle.py is the only independent judge for it.
        "symmetric refuses stock; the oracle checks it instead",
    ),
```

- [ ] **Step 6: Verify against the oracle and commit**

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" PYTHONPATH=tools uv run python tools/sweep.py
uv run ruff check src tests tools
```

Expected: all green; the sweep reports no `WRONG` rows. **A disagreement between the engine and the oracle is a real bug — investigate it, do not add the spec to `DIVERGENT` to get green.**

```bash
git add -A
git commit -m "feat: real stock data, measured anchors, and oracle support

chinook ships no level or balance column, so daily_inventory is added — eight
rows over three dates, small enough that every asserted figure is computable by
hand. Seeded deliberately with a TIE on the boundary instant, and with one track
absent at that instant, because a window that picked a single row or included
stale rows would otherwise pass.

Hand-computed: level at the last instant 111, at the first 1012, naive sum
across dates 1153.

The seed is opt-in and run by a tool. Until now a domain pack only DESCRIBED a
database it did not own; shipping SQL makes it a producer of schema, and that
must never be a side effect of Grain.load.

The corpus entry goes in DIVERGENT with its reason, since the symmetric engine
refuses stock — the oracle gains stock support and is the only independent judge
for that path."
```

---

## Task 8: documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/FINDINGS.md`, `docs/QUANTITY-TYPES.md`, `docs/plans/2026-09-06-stock-and-time-design.md`

- [ ] **Step 1: README**

In *"What the ontology must declare"*, document `quantity` on a metric and `time_grain` on a property. In the two-engines table add a row: `| Stock (level) metrics | windowed to a boundary | refused |`. In *"What is not done"*, replace the semi-additive row with: granularity re-bucketing is still absent, so *"inventory in Q1"* is inexpressible; and note the seed step for anyone running the tests.

- [ ] **Step 2: CLAUDE.md**

Under *"Two engines"*, add that the symmetric engine now refuses stock as well as opaque `expr`, and that its corpus entry is in `DIVERGENT` because of it — so the oracle is the only check on that path. Update the test command section to mention `tools/seed_inventory.py`.

- [ ] **Step 3: QUANTITY-TYPES.md**

Update §4's mapping table: `stock` is no longer missing, and grain's row in §5 becomes `flow/stock/value_per_unit + windowing; no composition, no granularity`. Leave §5a — the typed-composition gap — untouched, since it is still open and is the next phase.

- [ ] **Step 4: FINDINGS.md**

Add what this taught. At minimum: **making a rule unnecessary beats enforcing it** — rather than checking that nobody sums a stock across time, the window collapses to one instant so the sum cannot cross time at all; the guarantee holds by construction rather than by a check, which is the same move `EXISTS` made for the pre-aggregate.

- [ ] **Step 5: Mark the design implemented and commit**

Change its status line to `**Status:** implemented 2026-09-06.`

```bash
GRAIN_DATABASE_URL="postgresql+psycopg://$(whoami)@localhost:5432/chinook" uv run pytest -q && uv run ruff check src tests tools
git add -A
git commit -m "docs: record stock quantities and the time dimension"
```

---

## Deferred, recorded so each is a choice rather than an omission

- **Granularity re-bucketing** (design §6). No `date_trunc`, so *"inventory in Q1"* is inexpressible. `time_grain` is declared and verified but its granularity meaning is unused — the closest thing in this codebase to a field nothing reads, accepted only because the loader does check it against the column's type.
- **`window_groupings`.** MetricFlow can take each user's latest MRR then sum across users. Needs a partition distinct from the query's group keys.
- **Stock in the symmetric engine.** Would need a formulation that is not one pass.
- **Two windowed stocks in one query.** Refused in Task 5 rather than solved; two partitions of the same rows where the second filter sees only what the first left.
- **A metric/property quantity disagreement is not an error** (design §2). The metric wins silently. Debatable, and the alternative — refusing at load, as conflicting `expr`/`agg` declarations are — is the more conservative reading of this codebase's own rules.
