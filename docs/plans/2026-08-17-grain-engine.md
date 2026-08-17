# grain Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only ontology engine that turns a typed `QuerySpec` into grain-correct SQL over the Chinook database, refusing or rewriting any query that would silently double-count.

**Architecture:** Four layers. Generated SQLAlchemy ORM models describe the physical tables; a declarative `ontology.yaml` describes object types, links and metrics; a domain-neutral engine resolves a spec against the ontology, analyses grain, compiles a `Select`, and guards execution; thin adapters (library, CLI, MCP) sit on top. Nothing under `engine/` may import a domain module or an adapter.

**Tech Stack:** Python 3.12 · uv · SQLAlchemy 2.x · psycopg 3 · Pydantic 2 · PyYAML · pytest · sqlacodegen · MCP Python SDK

**Spec:** `/home/sol/projects/sol-obsidian-notes/obsidian-vault/Projects/Ontology Engine/grain/Design.md`

**Scope:** Milestones 2–7 of the spec's §13. Milestone 1 (Chinook loaded and verified) is **already complete**. Milestones 8–10 (second-domain test, golden set, ablation) are a separate plan — R6 is still deferred, so the second domain cannot be specified yet.

## Global Constraints

- **`engine/` never imports from `domains/` or from any adapter.** Domain packs are located by path at load time. This is the architecture test; a violation is a defect, not a style issue.
- **No SQL string is ever authored by a model.** The agent's only input is a validated `QuerySpec`.
- **Cardinality is declared, never inferred from data.**
- **Every failure is raised before a database connection is acquired**, except `GuardTripped`.
- **Every error names a legal alternative**, and that alternative must itself resolve.
- **Every result carries `compiled_sql`, `rewrites[]` and `additive`.**
- Python **3.12+**. SQLAlchemy **2.x** (`select()` style only — no legacy `Query`). Pydantic **v2** (`model_validate`, `Field`, not v1 APIs).
- Line length 100. All public functions carry type hints.
- Database URL comes from env var **`GRAIN_DATABASE_URL`**. Never hardcode credentials.

### Spec gap resolved in this plan

The spec structured join conditions (R1) but left metric `expr` as a SQL fragment, e.g. `"sum(invoice_line.unit_price * invoice_line.quantity)"`. The aggregate-then-join rewrite must relocate that expression into a subquery grouped at the metric's grain.

**Resolution:** `expr` remains an authored SQL fragment compiled via `sqlalchemy.text()`, but the loader validates that **every `table.column` token it references belongs to the metric's own grain table**. That makes relocation provably safe without parsing arithmetic, and it is checkable with a token scan. A metric referencing a foreign table fails at load.

---

## File Structure

```
src/grain/
  __init__.py
  engine/
    __init__.py
    errors.py       typed exception hierarchy; every error carries alternatives
    ontology.py     Pydantic types: ColumnRef, JoinPair, ObjectType, LinkType, Metric, Ontology
    loader.py       YAML -> Ontology, validated against SQLAlchemy MetaData
    spec.py         QuerySpec, Filter, Hop, OrderBy
    resolve.py      spec names -> ontology elements; builds ResolvedQuery (root + edge path)
    grain.py        cardinality propagation; fan-out, chasm and additivity analysis
    compile.py      ResolvedQuery + GrainPlan -> sqlalchemy Select
    guard.py        engine factory: read-only session, statement_timeout, row cap
    execute.py      run a Select, assemble Result
    describe.py     Ontology -> agent-readable description
    api.py          Grain facade: load / describe / explain / query
    cli.py          CLI adapter
    server.py       MCP adapter
  domains/
    __init__.py
    chinook/
      __init__.py
      models.py     sqlacodegen output (generated, not hand-edited)
      ontology.yaml objects / links / metrics
tests/
  conftest.py       db engine fixture, loaded ontology fixture
  unit/             no database; QuerySpec -> expected SQL
  integration/      against loaded chinook; anchored on measured values
```

---

### Task 1: Scaffold, generated models, database fixture

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `src/grain/__init__.py`, `src/grain/engine/__init__.py`, `src/grain/domains/__init__.py`, `src/grain/domains/chinook/__init__.py`
- Create (generated): `src/grain/domains/chinook/models.py`
- Test: `tests/conftest.py`, `tests/integration/test_database.py`

**Interfaces:**
- Consumes: nothing
- Produces: `db_engine` pytest fixture returning a SQLAlchemy `Engine`; `src.grain.domains.chinook.models` module exposing declarative classes `Album, Artist, Customer, Employee, Genre, Invoice, InvoiceLine, MediaType, Playlist, PlaylistTrack, Track` and `Base.metadata`

- [ ] **Step 1: Create the project scaffold**

```toml
# pyproject.toml
[project]
name = "grain"
version = "0.1.0"
description = "A declarative ontology layer over relational data."
requires-python = ">=3.12"
dependencies = [
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.1",
    "pydantic>=2.7",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "sqlacodegen>=3.0"]
mcp = ["mcp>=1.0"]

[project.scripts]
grain = "grain.engine.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/grain"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: requires a loaded chinook database"]
```

```
# .env.example
GRAIN_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/chinook
```

```
# .gitignore
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
dist/
```

Create the four `__init__.py` files as empty files.

- [ ] **Step 2: Install and generate the ORM models**

```bash
cd ~/projects/grain
uv venv && uv pip install -e ".[dev,mcp]"
export GRAIN_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/chinook'
uv run sqlacodegen "$GRAIN_DATABASE_URL" --outfile src/grain/domains/chinook/models.py
```

Expected: `models.py` containing eleven declarative classes. Do not hand-edit it — regeneration must stay safe.

- [ ] **Step 3: Write the failing fixture and smoke test**

```python
# tests/conftest.py
import os
import pytest
from sqlalchemy import create_engine

@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        pytest.skip("GRAIN_DATABASE_URL not set")
    return url

@pytest.fixture(scope="session")
def db_engine(db_url):
    engine = create_engine(db_url, future=True)
    yield engine
    engine.dispose()
```

```python
# tests/integration/test_database.py
import pytest
from sqlalchemy import func, select
from grain.domains.chinook import models

pytestmark = pytest.mark.integration

EXPECTED_ROWS = {
    "track": 3503, "invoice_line": 2240, "playlist_track": 8715, "invoice": 412,
    "album": 347, "artist": 275, "customer": 59, "genre": 25,
    "playlist": 18, "employee": 8, "media_type": 5,
}

def test_all_tables_present_with_expected_rows(db_engine):
    with db_engine.connect() as conn:
        for table_name, expected in EXPECTED_ROWS.items():
            table = models.Base.metadata.tables[table_name]
            actual = conn.execute(select(func.count()).select_from(table)).scalar_one()
            assert actual == expected, f"{table_name}: expected {expected}, got {actual}"
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/integration/test_database.py -v`
Expected: PASS. A failure here means the database differs from the spec's §3 and the plan's anchor values are invalid — stop and reconcile before continuing.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example .gitignore src tests
git commit -m "feat: scaffold grain package with generated chinook models"
```

---

### Task 2: Typed errors

**Files:**
- Create: `src/grain/engine/errors.py`
- Test: `tests/unit/test_errors.py`

**Interfaces:**
- Consumes: nothing
- Produces: `GrainError`, `OntologyError`, `UnknownName(kind, name, suggestions)`, `NoPath(from_object, to_object, available)`, `AmbiguousPath(from_object, to_object, candidates)`, `FanOutRefused(metric, grain, offending_edge, alternatives)`, `GuardTripped(limit_name, limit_value)`. Every subclass exposes `.alternatives -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_errors.py
import pytest
from grain.engine.errors import (
    GrainError, UnknownName, NoPath, AmbiguousPath, FanOutRefused, GuardTripped,
)

def test_unknown_name_lists_suggestions_in_message():
    err = UnknownName("metric", "revenu", ["revenue", "invoice_total"])
    assert "revenu" in str(err)
    assert "revenue" in str(err)
    assert err.alternatives == ["revenue", "invoice_total"]

def test_no_path_names_what_does_connect():
    err = NoPath("Customer", "Track", ["Customer_Invoices", "Customer_SupportRep"])
    assert "Customer" in str(err) and "Track" in str(err)
    assert err.alternatives == ["Customer_Invoices", "Customer_SupportRep"]

def test_fan_out_refused_names_grain_edge_and_fix():
    err = FanOutRefused("invoice_total", "invoice", "Invoice_Lines", ["revenue"])
    msg = str(err)
    assert "invoice_total" in msg and "invoice" in msg and "Invoice_Lines" in msg
    assert err.alternatives == ["revenue"]

def test_every_error_is_a_grain_error():
    for err in (
        UnknownName("metric", "x", []),
        NoPath("A", "B", []),
        AmbiguousPath("A", "B", ["L1", "L2"]),
        FanOutRefused("m", "g", "e", []),
        GuardTripped("row_cap", 10_000),
    ):
        assert isinstance(err, GrainError)
        assert isinstance(err.alternatives, list)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.errors'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/errors.py
"""Typed failures. Every error names a legal next move — an error that only
says 'no' costs the caller a turn and teaches it nothing."""
from __future__ import annotations


class GrainError(Exception):
    """Base for every grain failure."""

    def __init__(self, message: str, alternatives: list[str] | None = None) -> None:
        super().__init__(message)
        self.alternatives: list[str] = alternatives or []


class OntologyError(GrainError):
    """Raised at load time: the ontology does not match the database."""


class UnknownName(GrainError):
    def __init__(self, kind: str, name: str, suggestions: list[str]) -> None:
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"Unknown {kind} '{name}'.{hint}", suggestions)
        self.kind, self.name = kind, name


class NoPath(GrainError):
    def __init__(self, from_object: str, to_object: str, available: list[str]) -> None:
        hint = f" Links from {from_object}: {', '.join(available)}." if available else ""
        super().__init__(f"No declared link connects {from_object} to {to_object}.{hint}", available)
        self.from_object, self.to_object = from_object, to_object


class AmbiguousPath(GrainError):
    def __init__(self, from_object: str, to_object: str, candidates: list[str]) -> None:
        super().__init__(
            f"More than one link connects {from_object} to {to_object}: "
            f"{', '.join(candidates)}. Name the one you mean.",
            candidates,
        )
        self.from_object, self.to_object = from_object, to_object


class FanOutRefused(GrainError):
    def __init__(
        self, metric: str, grain: str, offending_edge: str, alternatives: list[str]
    ) -> None:
        hint = f" Alternatives: {', '.join(alternatives)}." if alternatives else ""
        super().__init__(
            f"Metric '{metric}' has grain '{grain}', but the query path fans out "
            f"through '{offending_edge}'. Summing it here would multiply each "
            f"'{grain}' row by its child count.{hint}",
            alternatives,
        )
        self.metric, self.grain, self.offending_edge = metric, grain, offending_edge


class GuardTripped(GrainError):
    def __init__(self, limit_name: str, limit_value: int) -> None:
        super().__init__(
            f"Guard '{limit_name}' exceeded (limit {limit_value}). "
            f"Narrow the request with filters or a smaller limit.",
            [],
        )
        self.limit_name, self.limit_value = limit_name, limit_value
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_errors.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/errors.py tests/unit/test_errors.py
git commit -m "feat: typed error hierarchy that names legal alternatives"
```

---

### Task 3: Ontology types

**Files:**
- Create: `src/grain/engine/ontology.py`
- Test: `tests/unit/test_ontology_types.py`

**Interfaces:**
- Consumes: `grain.engine.errors`
- Produces: `ColumnRef(table, column)` with `.parse(str)` and `.qualified`; `JoinPair(from_, to)`; `TableJoin(to, kind, on)`; `Property(column, type, nullable, via)`; `AiContext(synonyms, instructions)`; `ObjectType(name, primary, title_property, description, joins, properties, ai_context)`; `LinkType(name, from_, to, kind, cardinality, on, via, on_from, on_to, max_depth, inverse_of, description)` with `.fans_out -> bool`; `Metric(name, grain, expr, type, description, ai_context)`; `Ontology(name, objects, links, metrics)` with `.links_from(object_name)`. `Cardinality` and `LinkKind` literal aliases.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ontology_types.py
import pytest
from pydantic import ValidationError
from grain.engine.ontology import ColumnRef, LinkType, Metric, Ontology, ObjectType, Property

def test_column_ref_parses_dotted_name():
    ref = ColumnRef.parse("track.album_id")
    assert ref.table == "track" and ref.column == "album_id"
    assert ref.qualified == "track.album_id"

def test_column_ref_rejects_undotted_name():
    with pytest.raises(ValueError):
        ColumnRef.parse("album_id")

def test_fans_out_is_true_only_for_multiplying_cardinalities():
    def link(card):
        return LinkType(
            name="L", **{"from": "A"}, to="B", kind="direct", cardinality=card,
            on=[{"from": "a.b_id", "to": "b.id"}],
        )
    assert link("one_to_many").fans_out is True
    assert link("many_to_many").fans_out is True
    assert link("many_to_one").fans_out is False
    assert link("one_to_one").fans_out is False

def test_through_link_requires_via():
    with pytest.raises(ValidationError):
        LinkType(
            name="L", **{"from": "Playlist"}, to="Track", kind="through",
            cardinality="many_to_many",
            on_from=[{"from": "playlist.playlist_id", "to": "playlist_track.playlist_id"}],
            on_to=[{"from": "playlist_track.track_id", "to": "track.track_id"}],
        )

def test_direct_link_requires_on():
    with pytest.raises(ValidationError):
        LinkType(name="L", **{"from": "A"}, to="B", kind="direct", cardinality="many_to_one")

def test_links_from_returns_only_outbound_links():
    onto = Ontology(
        name="t",
        objects={"A": ObjectType(name="A", primary="a",
                                 properties={"id": Property(column="a.id", type="integer")})},
        links={
            "A_B": LinkType(name="A_B", **{"from": "A"}, to="B", kind="direct",
                            cardinality="many_to_one", on=[{"from": "a.b_id", "to": "b.id"}]),
            "B_A": LinkType(name="B_A", **{"from": "B"}, to="A", kind="direct",
                            cardinality="one_to_many", on=[{"from": "b.id", "to": "a.b_id"}]),
        },
        metrics={},
    )
    assert [l.name for l in onto.links_from("A")] == ["A_B"]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_ontology_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.ontology'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/ontology.py
"""The logical layer, as data. These types are the whole vocabulary the engine
reasons over; nothing here knows about any particular database."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
LinkKind = Literal["direct", "through", "recursive"]
ValueType = Literal["string", "integer", "decimal", "boolean", "date", "datetime"]

FANNING: frozenset[str] = frozenset({"one_to_many", "many_to_many"})


class ColumnRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    table: str
    column: str

    @classmethod
    def parse(cls, value: str | "ColumnRef" | dict[str, Any]) -> "ColumnRef":
        if isinstance(value, ColumnRef):
            return value
        if isinstance(value, dict):
            return cls(**value)
        table, sep, column = value.partition(".")
        if not sep or not table or not column:
            raise ValueError(f"Column reference must be 'table.column', got '{value}'")
        return cls(table=table, column=column)

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


def _as_column_ref(value: Any) -> ColumnRef:
    return ColumnRef.parse(value)


class JoinPair(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: ColumnRef = Field(alias="from")
    to: ColumnRef

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> ColumnRef:
        return _as_column_ref(v)


class TableJoin(BaseModel):
    to: str
    kind: Literal["left", "inner"] = "left"
    on: list[JoinPair]


class AiContext(BaseModel):
    synonyms: list[str] = Field(default_factory=list)
    instructions: str | None = None


class Property(BaseModel):
    column: ColumnRef
    type: ValueType
    nullable: bool = False
    via: str | None = None
    description: str | None = None

    @field_validator("column", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> ColumnRef:
        return _as_column_ref(v)


class ObjectType(BaseModel):
    name: str
    primary: str
    title_property: str | None = None
    description: str | None = None
    joins: dict[str, TableJoin] = Field(default_factory=dict)
    properties: dict[str, Property] = Field(default_factory=dict)
    ai_context: AiContext | None = None

    @property
    def tables(self) -> list[str]:
        return [self.primary] + [j.to for j in self.joins.values()]


class LinkType(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    from_: str = Field(alias="from")
    to: str
    kind: LinkKind
    cardinality: Cardinality
    on: list[JoinPair] = Field(default_factory=list)
    via: str | None = None
    on_from: list[JoinPair] = Field(default_factory=list)
    on_to: list[JoinPair] = Field(default_factory=list)
    max_depth: int = 10
    inverse_of: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "LinkType":
        if self.kind in ("direct", "recursive") and not self.on:
            raise ValueError(f"Link '{self.name}' of kind '{self.kind}' requires 'on'")
        if self.kind == "through":
            if not self.via:
                raise ValueError(f"Link '{self.name}' of kind 'through' requires 'via'")
            if not self.on_from or not self.on_to:
                raise ValueError(
                    f"Link '{self.name}' of kind 'through' requires 'on_from' and 'on_to'"
                )
        return self

    @property
    def fans_out(self) -> bool:
        """True when traversing this edge can replicate the rows we started with."""
        return self.cardinality in FANNING


class Metric(BaseModel):
    name: str
    grain: str
    expr: str
    type: ValueType
    description: str | None = None
    ai_context: AiContext | None = None


class Ontology(BaseModel):
    name: str
    description: str | None = None
    objects: dict[str, ObjectType] = Field(default_factory=dict)
    links: dict[str, LinkType] = Field(default_factory=dict)
    metrics: dict[str, Metric] = Field(default_factory=dict)

    def links_from(self, object_name: str) -> list[LinkType]:
        return [l for l in self.links.values() if l.from_ == object_name]

    def object_for_table(self, table: str) -> ObjectType | None:
        for obj in self.objects.values():
            if obj.primary == table:
                return obj
        return None
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_ontology_types.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/ontology.py tests/unit/test_ontology_types.py
git commit -m "feat: pydantic ontology types with declared cardinality"
```

---

### Task 4: Loader with startup validation

**Files:**
- Create: `src/grain/engine/loader.py`
- Test: `tests/unit/test_loader.py`

**Interfaces:**
- Consumes: `ontology.Ontology`, `errors.OntologyError`
- Produces: `load_ontology(path: Path, metadata: MetaData) -> Ontology`, `METRIC_COLUMN_TOKEN` regex, `validate(onto, metadata) -> None`

Validation rules, all raising `OntologyError` naming the offender:
1. Every table named by an object's `primary`, a `TableJoin.to`, a `JoinPair` column, a `Property.column` or a `Metric.grain` exists in `metadata`.
2. Every column named exists on its table.
3. Every `Property.via` names a declared join on that object.
4. Every link's `from`/`to` names a declared object.
5. **Every `table.column` token in a metric's `expr` belongs to that metric's `grain` table.**

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_loader.py
import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table
from grain.engine.errors import OntologyError
from grain.engine.loader import load_ontology_from_string

@pytest.fixture
def metadata():
    md = MetaData()
    Table("invoice", md, Column("invoice_id", Integer), Column("total", Numeric),
          Column("customer_id", Integer))
    Table("invoice_line", md, Column("invoice_line_id", Integer),
          Column("invoice_id", Integer), Column("unit_price", Numeric),
          Column("quantity", Integer))
    Table("customer", md, Column("customer_id", Integer), Column("country", String))
    return md

GOOD = """
name: tiny
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}
links:
  Customer_Invoices:
    from: Customer
    to: Invoice
    kind: direct
    on: [{from: customer.customer_id, to: invoice.customer_id}]
    cardinality: one_to_many
metrics:
  revenue:
    grain: invoice_line
    expr: "sum(invoice_line.unit_price * invoice_line.quantity)"
    type: decimal
"""

def test_good_ontology_loads(metadata):
    onto = load_ontology_from_string(GOOD, metadata)
    assert onto.name == "tiny"
    assert onto.metrics["revenue"].grain == "invoice_line"
    assert onto.links["Customer_Invoices"].fans_out is True

def test_unknown_column_fails_at_load_naming_the_column(metadata):
    bad = GOOD.replace("customer.country", "customer.contry")
    with pytest.raises(OntologyError, match="contry"):
        load_ontology_from_string(bad, metadata)

def test_unknown_table_fails_at_load_naming_the_table(metadata):
    bad = GOOD.replace("grain: invoice_line", "grain: invoice_lines")
    with pytest.raises(OntologyError, match="invoice_lines"):
        load_ontology_from_string(bad, metadata)

def test_metric_referencing_a_foreign_table_is_rejected(metadata):
    bad = GOOD.replace(
        'expr: "sum(invoice_line.unit_price * invoice_line.quantity)"',
        'expr: "sum(invoice.total)"',
    )
    with pytest.raises(OntologyError, match="invoice.total"):
        load_ontology_from_string(bad, metadata)

def test_link_naming_an_undeclared_object_is_rejected(metadata):
    bad = GOOD.replace("to: Invoice", "to: Invoicee")
    with pytest.raises(OntologyError, match="Invoicee"):
        load_ontology_from_string(bad, metadata)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_loader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.loader'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/loader.py
"""YAML in, validated Ontology out. Everything checkable is checked here, at
startup — a typo must fail at boot, not halfway through a query."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy import MetaData

from .errors import OntologyError
from .ontology import ColumnRef, Metric, Ontology

METRIC_COLUMN_TOKEN = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")


def load_ontology_from_string(text: str, metadata: MetaData) -> Ontology:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise OntologyError("Ontology file must be a YAML mapping")
    for key in ("objects", "links", "metrics"):
        for name, body in (raw.get(key) or {}).items():
            body.setdefault("name", name)
    onto = Ontology.model_validate(raw)
    validate(onto, metadata)
    return onto


def load_ontology(path: Path, metadata: MetaData) -> Ontology:
    return load_ontology_from_string(path.read_text(encoding="utf-8"), metadata)


def _require_table(metadata: MetaData, table: str, context: str) -> None:
    if table not in metadata.tables:
        raise OntologyError(f"{context}: table '{table}' does not exist in the database")


def _require_column(metadata: MetaData, ref: ColumnRef, context: str) -> None:
    _require_table(metadata, ref.table, context)
    if ref.column not in metadata.tables[ref.table].columns:
        raise OntologyError(
            f"{context}: column '{ref.qualified}' does not exist in the database"
        )


def _validate_metric_expr(metric: Metric, metadata: MetaData) -> None:
    """Every table.column token must belong to the metric's own grain table.

    This is what makes relocating the expression into a subquery grouped at its
    grain provably safe, without parsing the arithmetic.
    """
    _require_table(metadata, metric.grain, f"metric '{metric.name}'")
    for table, column in METRIC_COLUMN_TOKEN.findall(metric.expr):
        if table != metric.grain:
            raise OntologyError(
                f"metric '{metric.name}' has grain '{metric.grain}' but its "
                f"expression references '{table}.{column}'. A metric may only "
                f"reference columns of its own grain table."
            )
        _require_column(metadata, ColumnRef(table=table, column=column),
                        f"metric '{metric.name}'")


def validate(onto: Ontology, metadata: MetaData) -> None:
    for obj in onto.objects.values():
        ctx = f"object '{obj.name}'"
        _require_table(metadata, obj.primary, ctx)
        for join_name, join in obj.joins.items():
            _require_table(metadata, join.to, f"{ctx} join '{join_name}'")
            for pair in join.on:
                _require_column(metadata, pair.from_, f"{ctx} join '{join_name}'")
                _require_column(metadata, pair.to, f"{ctx} join '{join_name}'")
        for prop_name, prop in obj.properties.items():
            _require_column(metadata, prop.column, f"{ctx} property '{prop_name}'")
            if prop.via is not None and prop.via not in obj.joins:
                raise OntologyError(
                    f"{ctx} property '{prop_name}': via '{prop.via}' is not a "
                    f"declared join. Declared joins: {sorted(obj.joins)}"
                )

    for link in onto.links.values():
        ctx = f"link '{link.name}'"
        for side in (link.from_, link.to):
            if side not in onto.objects:
                raise OntologyError(
                    f"{ctx}: '{side}' is not a declared object. "
                    f"Declared objects: {sorted(onto.objects)}"
                )
        if link.via is not None:
            _require_table(metadata, link.via, ctx)
        for pair in [*link.on, *link.on_from, *link.on_to]:
            _require_column(metadata, pair.from_, ctx)
            _require_column(metadata, pair.to, ctx)
        if link.inverse_of is not None and link.inverse_of not in onto.links:
            raise OntologyError(f"{ctx}: inverse_of '{link.inverse_of}' is not a declared link")

    for metric in onto.metrics.values():
        _validate_metric_expr(metric, metadata)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_loader.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/loader.py tests/unit/test_loader.py
git commit -m "feat: ontology loader validating against database metadata at startup"
```

---

### Task 5: The Chinook domain pack

**Files:**
- Create: `src/grain/domains/chinook/ontology.yaml`
- Test: `tests/integration/test_chinook_ontology.py`

**Interfaces:**
- Consumes: `loader.load_ontology`
- Produces: an `ontology.yaml` declaring objects `Artist, Album, Track, Genre, MediaType, Playlist, Customer, Invoice, InvoiceLine, Employee`; the seven links from the spec plus `Album_Tracks`; metrics `revenue, invoice_total, units_sold, track_count, customer_count`. Also produces the `chinook_ontology` pytest fixture.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_chinook_ontology.py
import pytest

pytestmark = pytest.mark.integration

# Fixtures chinook_metadata / chinook_ontology come from tests/conftest.py (below).

def test_all_ten_objects_declared(chinook_ontology):
    onto = chinook_ontology
    assert set(onto.objects) == {
        "Artist", "Album", "Track", "Genre", "MediaType",
        "Playlist", "Customer", "Invoice", "InvoiceLine", "Employee",
    }

def test_all_three_link_kinds_present(chinook_ontology):
    onto = chinook_ontology
    kinds = {l.kind for l in onto.links.values()}
    assert kinds == {"direct", "through", "recursive"}

def test_two_distinct_routes_from_customer(chinook_ontology):
    onto = chinook_ontology
    names = {l.name for l in onto.links_from("Customer")}
    assert {"Customer_Invoices", "Customer_SupportRep"} <= names

def test_metrics_declare_three_distinct_grains(chinook_ontology):
    onto = chinook_ontology
    grains = {m.grain for m in onto.metrics.values()}
    assert {"invoice_line", "invoice", "track", "customer"} == grains

def test_track_object_spans_three_tables(chinook_ontology):
    onto = chinook_ontology
    assert set(onto.objects["Track"].tables) == {"track", "genre", "media_type"}

def test_playlist_track_join_table_is_hidden(chinook_ontology):
    onto = chinook_ontology
    """playlist_track must not appear as an object — it is a physical artifact."""
    assert all(o.primary != "playlist_track" for o in onto.objects.values())
    assert onto.links["Playlist_Tracks"].via == "playlist_track"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/integration/test_chinook_ontology.py -v`
Expected: FAIL — `ImportError: cannot import name 'CHINOOK_DIR'`

- [ ] **Step 3: Write the domain pack**

```python
# src/grain/domains/chinook/__init__.py
from pathlib import Path

CHINOOK_DIR = Path(__file__).parent
```

```yaml
# src/grain/domains/chinook/ontology.yaml
name: chinook
description: A digital music store — artists, tracks, playlists, customers, invoices.

objects:
  Artist:
    primary: artist
    title_property: name
    properties:
      name: {column: artist.name, type: string}

  Album:
    primary: album
    title_property: title
    properties:
      title: {column: album.title, type: string}

  Track:
    primary: track
    title_property: name
    description: A purchasable audio track.
    joins:
      genre:
        to: genre
        kind: left
        on: [{from: track.genre_id, to: genre.genre_id}]
      media_type:
        to: media_type
        kind: left
        on: [{from: track.media_type_id, to: media_type.media_type_id}]
    properties:
      name: {column: track.name, type: string}
      composer: {column: track.composer, type: string, nullable: true}
      duration_ms: {column: track.milliseconds, type: integer}
      unit_price: {column: track.unit_price, type: decimal}
      genre: {column: genre.name, type: string, via: genre, nullable: true}
      media_type: {column: media_type.name, type: string, via: media_type, nullable: true}

  Genre:
    primary: genre
    title_property: name
    properties:
      name: {column: genre.name, type: string}

  MediaType:
    primary: media_type
    title_property: name
    properties:
      name: {column: media_type.name, type: string}

  Playlist:
    primary: playlist
    title_property: name
    properties:
      name: {column: playlist.name, type: string}

  Customer:
    primary: customer
    title_property: last_name
    properties:
      first_name: {column: customer.first_name, type: string}
      last_name: {column: customer.last_name, type: string}
      country: {column: customer.country, type: string, nullable: true}
      city: {column: customer.city, type: string, nullable: true}
      email: {column: customer.email, type: string}

  Invoice:
    primary: invoice
    properties:
      invoice_date: {column: invoice.invoice_date, type: datetime}
      billing_country: {column: invoice.billing_country, type: string, nullable: true}
      total: {column: invoice.total, type: decimal}

  InvoiceLine:
    primary: invoice_line
    properties:
      unit_price: {column: invoice_line.unit_price, type: decimal}
      quantity: {column: invoice_line.quantity, type: integer}

  Employee:
    primary: employee
    title_property: last_name
    properties:
      first_name: {column: employee.first_name, type: string}
      last_name: {column: employee.last_name, type: string}
      title: {column: employee.title, type: string, nullable: true}

links:
  Artist_Albums:
    from: Artist
    to: Album
    kind: direct
    on: [{from: artist.artist_id, to: album.artist_id}]
    cardinality: one_to_many

  Album_Tracks:
    from: Album
    to: Track
    kind: direct
    on: [{from: album.album_id, to: track.album_id}]
    cardinality: one_to_many

  Track_Album:
    from: Track
    to: Album
    kind: direct
    on: [{from: track.album_id, to: album.album_id}]
    cardinality: many_to_one
    inverse_of: Album_Tracks

  Playlist_Tracks:
    from: Playlist
    to: Track
    kind: through
    via: playlist_track
    on_from: [{from: playlist.playlist_id, to: playlist_track.playlist_id}]
    on_to: [{from: playlist_track.track_id, to: track.track_id}]
    cardinality: many_to_many

  Track_InvoiceLines:
    from: Track
    to: InvoiceLine
    kind: direct
    on: [{from: track.track_id, to: invoice_line.track_id}]
    cardinality: one_to_many

  Customer_Invoices:
    from: Customer
    to: Invoice
    kind: direct
    on: [{from: customer.customer_id, to: invoice.customer_id}]
    cardinality: one_to_many

  Invoice_Lines:
    from: Invoice
    to: InvoiceLine
    kind: direct
    on: [{from: invoice.invoice_id, to: invoice_line.invoice_id}]
    cardinality: one_to_many

  Customer_SupportRep:
    from: Customer
    to: Employee
    kind: direct
    on: [{from: customer.support_rep_id, to: employee.employee_id}]
    cardinality: many_to_one
    description: The sales rep assigned to this customer.

  Employee_Manager:
    from: Employee
    to: Employee
    kind: recursive
    on: [{from: employee.reports_to, to: employee.employee_id}]
    cardinality: many_to_one
    max_depth: 10

metrics:
  revenue:
    grain: invoice_line
    expr: "sum(invoice_line.unit_price * invoice_line.quantity)"
    type: decimal
    description: Money actually taken, at line grain. The correct revenue figure.
    ai_context:
      synonyms: [revenue, sales, turnover, money taken, total sales]
      instructions: >
        Default choice for any revenue question. Prefer this over invoice_total
        unless the question is explicitly about invoice headers.

  invoice_total:
    grain: invoice
    expr: "sum(invoice.total)"
    type: decimal
    description: >
      Invoice header totals. Equals revenue only when evaluated at invoice grain.
    ai_context:
      synonyms: [invoice total, billed amount, header total]
      instructions: >
        Only for questions about invoices as documents. Not the default revenue
        metric — see revenue.

  units_sold:
    grain: invoice_line
    expr: "sum(invoice_line.quantity)"
    type: integer

  track_count:
    grain: track
    expr: "count(distinct track.track_id)"
    type: integer

  customer_count:
    grain: customer
    expr: "count(distinct customer.customer_id)"
    type: integer
```

Add the shared fixtures to `tests/conftest.py`:

```python
# append to tests/conftest.py
from pathlib import Path
from sqlalchemy import MetaData
from grain.engine.loader import load_ontology
from grain.domains.chinook import CHINOOK_DIR

@pytest.fixture(scope="session")
def chinook_metadata(db_engine):
    md = MetaData()
    md.reflect(bind=db_engine)
    return md

@pytest.fixture(scope="session")
def chinook_ontology(chinook_metadata):
    return load_ontology(CHINOOK_DIR / "ontology.yaml", chinook_metadata)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/integration/test_chinook_ontology.py -v`
Expected: PASS (6 tests). Any load failure names the exact offending table or column — fix the YAML, not the loader.

- [ ] **Step 5: Commit**

```bash
git add src/grain/domains/chinook tests/conftest.py tests/integration/test_chinook_ontology.py
git commit -m "feat: chinook domain pack with three link kinds and five metrics"
```

---

### Task 6: QuerySpec

**Files:**
- Create: `src/grain/engine/spec.py`
- Test: `tests/unit/test_spec.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Filter(property, op, value)`, `Hop(link, max_depth)`, `OrderBy(key, desc)`, `QuerySpec(object, filters, traverse, group_by, metrics, order_by, limit)`, `FilterOp` literal alias

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_spec.py
import pytest
from pydantic import ValidationError
from grain.engine.spec import Filter, Hop, QuerySpec

def test_minimal_spec_defaults():
    spec = QuerySpec(object="Customer")
    assert spec.filters == [] and spec.metrics == [] and spec.limit == 100

def test_limit_is_capped():
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", limit=10_001)

def test_limit_must_be_positive():
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", limit=0)

def test_unknown_operator_is_rejected():
    with pytest.raises(ValidationError):
        Filter(property="country", op="regex", value="^U")

def test_is_null_needs_no_value():
    assert Filter(property="composer", op="is_null").value is None

def test_spec_rejects_unknown_fields():
    """A hallucinated field must fail loudly, not be silently ignored."""
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", sql="select 1")

def test_hop_carries_optional_depth():
    assert Hop(link="Employee_Manager", max_depth=3).max_depth == 3
    assert Hop(link="Customer_Invoices").max_depth is None
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.spec'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/spec.py
"""The agent's only input. Every string here must name something the ontology
declares, so an invalid request is unrepresentable rather than merely rejected."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FilterOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "like", "is_null"]

STRICT = ConfigDict(extra="forbid")


class Filter(BaseModel):
    model_config = STRICT
    property: str
    op: FilterOp
    value: Any = None


class Hop(BaseModel):
    model_config = STRICT
    link: str
    max_depth: int | None = Field(default=None, ge=1, le=50)


class OrderBy(BaseModel):
    model_config = STRICT
    key: str
    desc: bool = False


class QuerySpec(BaseModel):
    model_config = STRICT
    object: str
    filters: list[Filter] = Field(default_factory=list)
    traverse: list[Hop] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=10_000)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_spec.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/spec.py tests/unit/test_spec.py
git commit -m "feat: QuerySpec with closed vocabulary and forbidden extra fields"
```

---

### Task 7: Resolver

**Files:**
- Create: `src/grain/engine/resolve.py`
- Test: `tests/unit/test_resolve.py`

**Interfaces:**
- Consumes: `ontology.Ontology`, `spec.QuerySpec`, `errors.*`
- Produces: `Edge(link, from_object, to_object)`; `ResolvedProperty(object, name, prop)`; `ResolvedFilter(property, op, value, hops_before)`; `ResolvedQuery(ontology, root, path, filters, group_by, metrics, order_by, limit)` with `.tables_in_scope -> list[str]` and `.fanning_edges -> list[Edge]`; `resolve(spec, onto) -> ResolvedQuery`; `suggest(name, candidates) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_resolve.py
import pytest
from grain.engine.errors import NoPath, UnknownName
from grain.engine.resolve import resolve, suggest
from grain.engine.spec import Filter, Hop, QuerySpec

def test_resolves_root_and_direct_path(chinook_lite):
    rq = resolve(QuerySpec(object="Customer", traverse=[Hop(link="Customer_Invoices")]),
                 chinook_lite)
    assert rq.root.name == "Customer"
    assert [e.link.name for e in rq.path] == ["Customer_Invoices"]
    assert "invoice" in rq.tables_in_scope

def test_unknown_object_suggests_nearest(chinook_lite):
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Custommer"), chinook_lite)
    assert "Customer" in exc.value.alternatives

def test_unknown_metric_suggests_nearest(chinook_lite):
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Customer", metrics=["revenu"]), chinook_lite)
    assert "revenue" in exc.value.alternatives

def test_suggestion_is_itself_a_valid_name(chinook_lite):
    """An error that suggests an invalid name wastes the retry it was meant to save."""
    with pytest.raises(UnknownName) as exc:
        resolve(QuerySpec(object="Customer", metrics=["revenu"]), chinook_lite)
    for candidate in exc.value.alternatives:
        assert candidate in chinook_lite.metrics

def test_hop_that_does_not_start_at_current_object_raises_no_path(chinook_lite):
    with pytest.raises(NoPath):
        resolve(QuerySpec(object="Customer", traverse=[Hop(link="Invoice_Lines")]),
                chinook_lite)

def test_fanning_edges_reports_only_multiplying_hops(chinook_lite):
    rq = resolve(
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")]),
        chinook_lite,
    )
    assert [e.link.name for e in rq.fanning_edges] == ["Customer_Invoices", "Invoice_Lines"]

def test_many_to_one_hop_does_not_fan(chinook_lite):
    rq = resolve(QuerySpec(object="Customer", traverse=[Hop(link="Customer_SupportRep")]),
                 chinook_lite)
    assert rq.fanning_edges == []

def test_suggest_orders_by_closeness():
    assert suggest("revenu", ["revenue", "units_sold", "track_count"])[0] == "revenue"
```

Add the unit-test ontology fixture — this one needs **no database**, which is the point:

```python
# tests/unit/conftest.py
import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Numeric, String, Table
from grain.engine.loader import load_ontology_from_string

TINY_YAML = """
name: chinook_lite
objects:
  Customer:
    primary: customer
    properties:
      country: {column: customer.country, type: string}
  Invoice:
    primary: invoice
    properties:
      total: {column: invoice.total, type: decimal}
  InvoiceLine:
    primary: invoice_line
    properties:
      quantity: {column: invoice_line.quantity, type: integer}
  Employee:
    primary: employee
    properties:
      last_name: {column: employee.last_name, type: string}
links:
  Customer_Invoices:
    from: Customer
    to: Invoice
    kind: direct
    on: [{from: customer.customer_id, to: invoice.customer_id}]
    cardinality: one_to_many
  Invoice_Lines:
    from: Invoice
    to: InvoiceLine
    kind: direct
    on: [{from: invoice.invoice_id, to: invoice_line.invoice_id}]
    cardinality: one_to_many
  Customer_SupportRep:
    from: Customer
    to: Employee
    kind: direct
    on: [{from: customer.support_rep_id, to: employee.employee_id}]
    cardinality: many_to_one
  Employee_Manager:
    from: Employee
    to: Employee
    kind: recursive
    on: [{from: employee.reports_to, to: employee.employee_id}]
    cardinality: many_to_one
    max_depth: 10
metrics:
  revenue:
    grain: invoice_line
    expr: "sum(invoice_line.unit_price * invoice_line.quantity)"
    type: decimal
  invoice_total:
    grain: invoice
    expr: "sum(invoice.total)"
    type: decimal
  customer_count:
    grain: customer
    expr: "count(distinct customer.customer_id)"
    type: integer
"""

@pytest.fixture(scope="session")
def lite_metadata():
    md = MetaData()
    Table("customer", md, Column("customer_id", Integer), Column("country", String),
          Column("support_rep_id", Integer))
    Table("invoice", md, Column("invoice_id", Integer), Column("customer_id", Integer),
          Column("total", Numeric), Column("invoice_date", DateTime))
    Table("invoice_line", md, Column("invoice_line_id", Integer),
          Column("invoice_id", Integer), Column("unit_price", Numeric),
          Column("quantity", Integer))
    Table("employee", md, Column("employee_id", Integer), Column("reports_to", Integer),
          Column("last_name", String))
    return md

@pytest.fixture(scope="session")
def chinook_lite(lite_metadata):
    return load_ontology_from_string(TINY_YAML, lite_metadata)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.resolve'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/resolve.py
"""Turn names into ontology elements and a walked path. Nothing here touches a
database; every failure is decidable from the ontology alone."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .errors import NoPath, UnknownName
from .ontology import LinkType, Metric, ObjectType, Ontology, Property
from .spec import FilterOp, OrderBy, QuerySpec


def suggest(name: str, candidates: list[str], limit: int = 3) -> list[str]:
    close = difflib.get_close_matches(name, candidates, n=limit, cutoff=0.5)
    return close or sorted(candidates)[:limit]


@dataclass(frozen=True)
class Edge:
    link: LinkType
    from_object: ObjectType
    to_object: ObjectType


@dataclass(frozen=True)
class ResolvedProperty:
    object: ObjectType
    name: str
    prop: Property


@dataclass(frozen=True)
class ResolvedFilter:
    property: ResolvedProperty
    op: FilterOp
    value: Any


@dataclass
class ResolvedQuery:
    ontology: Ontology
    root: ObjectType
    path: list[Edge] = field(default_factory=list)
    filters: list[ResolvedFilter] = field(default_factory=list)
    group_by: list[ResolvedProperty] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int = 100

    @property
    def tables_in_scope(self) -> list[str]:
        tables = list(self.root.tables)
        for edge in self.path:
            for table in edge.to_object.tables:
                if table not in tables:
                    tables.append(table)
            if edge.link.via and edge.link.via not in tables:
                tables.append(edge.link.via)
        return tables

    @property
    def fanning_edges(self) -> list[Edge]:
        return [e for e in self.path if e.link.fans_out]


def _object(onto: Ontology, name: str) -> ObjectType:
    if name not in onto.objects:
        raise UnknownName("object type", name, suggest(name, list(onto.objects)))
    return onto.objects[name]


def _property(obj: ObjectType, name: str) -> ResolvedProperty:
    if name not in obj.properties:
        raise UnknownName(
            f"property of {obj.name}", name, suggest(name, list(obj.properties))
        )
    return ResolvedProperty(object=obj, name=name, prop=obj.properties[name])


def resolve(spec: QuerySpec, onto: Ontology) -> ResolvedQuery:
    root = _object(onto, spec.object)

    path: list[Edge] = []
    current = root
    for hop in spec.traverse:
        if hop.link not in onto.links:
            raise UnknownName("link", hop.link, suggest(hop.link, list(onto.links)))
        link = onto.links[hop.link]
        if link.from_ != current.name:
            raise NoPath(
                current.name,
                link.to,
                [l.name for l in onto.links_from(current.name)],
            )
        target = _object(onto, link.to)
        if hop.max_depth is not None:
            link = link.model_copy(update={"max_depth": hop.max_depth})
        path.append(Edge(link=link, from_object=current, to_object=target))
        current = target

    filters = [ResolvedFilter(_property(root, f.property), f.op, f.value) for f in spec.filters]
    group_by = [_property(root, key) for key in spec.group_by]

    metrics: list[Metric] = []
    for name in spec.metrics:
        if name not in onto.metrics:
            raise UnknownName("metric", name, suggest(name, list(onto.metrics)))
        metrics.append(onto.metrics[name])

    return ResolvedQuery(
        ontology=onto, root=root, path=path, filters=filters,
        group_by=group_by, metrics=metrics, order_by=spec.order_by, limit=spec.limit,
    )
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_resolve.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/resolve.py tests/unit/test_resolve.py tests/unit/conftest.py
git commit -m "feat: resolver building an edge path with validated suggestions"
```

---

### Task 8: Grain analysis

**Files:**
- Create: `src/grain/engine/grain.py`
- Test: `tests/unit/test_grain.py`

**Interfaces:**
- Consumes: `resolve.ResolvedQuery`, `resolve.Edge`, `errors.FanOutRefused`
- Produces: `Strategy = Literal["inline","aggregate_then_join"]`; `MetricPlan(metric, strategy, forced_by)`; `GrainPlan(metric_plans, additive, non_additive_reason)`; `analyse(rq) -> GrainPlan`; `path_to_table(rq, table) -> list[Edge] | None`

**The rules, in full.** For each requested metric `m` at grain `g`:
1. Find the prefix of `rq.path` that first brings table `g` into scope (`path_to_table`). `g` may also be one of the root object's tables, giving an empty prefix.
2. If `g` is nowhere in scope → `FanOutRefused(m.name, g, "<not on path>", alternatives)` where alternatives are the metrics whose grain *is* in scope.
3. If no edge in that prefix fans out → `strategy="inline"`.
4. Otherwise → `strategy="aggregate_then_join"`, `forced_by` naming the **first** fanning edge.

Additivity, computed once for the whole query: `additive=False` if **any** edge in the path to any requested metric's grain has cardinality `many_to_many`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_grain.py
import pytest
from grain.engine.errors import FanOutRefused
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

def plan_for(onto, **spec_kwargs):
    return analyse(resolve(QuerySpec(**spec_kwargs), onto))

def test_metric_at_root_grain_is_inline(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"])
    assert plan.metric_plans[0].strategy == "inline"
    assert plan.metric_plans[0].forced_by is None

def test_metric_behind_a_fanning_edge_is_rewritten(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    mp = plan.metric_plans[0]
    assert mp.strategy == "aggregate_then_join"
    assert mp.forced_by == "Customer_Invoices"

def test_metric_behind_only_many_to_one_stays_inline(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"],
                    traverse=[Hop(link="Customer_SupportRep")])
    assert plan.metric_plans[0].strategy == "inline"

def test_coarser_metric_on_a_fanned_path_is_rewritten_not_returned_raw(chinook_lite):
    """invoice_total lives at invoice grain; the path reaches invoice_line."""
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["invoice_total"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert plan.metric_plans[0].strategy == "aggregate_then_join"

def test_metric_whose_grain_is_off_path_is_refused(chinook_lite):
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Customer", metrics=["revenue"])
    assert exc.value.metric == "revenue"
    assert exc.value.grain == "invoice_line"

def test_refusal_suggests_a_metric_that_is_in_scope(chinook_lite):
    with pytest.raises(FanOutRefused) as exc:
        plan_for(chinook_lite, object="Customer", metrics=["revenue"])
    assert "customer_count" in exc.value.alternatives
    for name in exc.value.alternatives:
        assert name in chinook_lite.metrics

def test_metrics_at_two_grains_each_get_their_own_plan(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue", "customer_count"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    strategies = {mp.metric.name: mp.strategy for mp in plan.metric_plans}
    assert strategies == {"revenue": "aggregate_then_join", "customer_count": "inline"}

def test_analyse_is_total_over_every_metric(chinook_lite):
    """No metric may reach the end of analysis without a verdict."""
    plan = plan_for(chinook_lite, object="Customer", metrics=["customer_count"])
    assert len(plan.metric_plans) == 1
    assert all(mp.strategy in ("inline", "aggregate_then_join") for mp in plan.metric_plans)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_grain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.grain'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/grain.py
"""The one thing the database cannot tell us: what one row means, and therefore
when an aggregation over it is safe. Decidable from declared cardinality alone —
no data is read here, which is why the verdict cannot drift as rows are added."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .errors import FanOutRefused
from .ontology import Metric
from .resolve import Edge, ResolvedQuery

Strategy = Literal["inline", "aggregate_then_join"]


@dataclass(frozen=True)
class MetricPlan:
    metric: Metric
    strategy: Strategy
    forced_by: str | None = None


@dataclass
class GrainPlan:
    metric_plans: list[MetricPlan] = field(default_factory=list)
    additive: bool = True
    non_additive_reason: str | None = None


def path_to_table(rq: ResolvedQuery, table: str) -> list[Edge] | None:
    """The shortest prefix of the walked path that brings `table` into scope.

    Returns [] when the table belongs to the root object, and None when the
    table is not reachable along the declared traversal at all.
    """
    if table in rq.root.tables:
        return []
    prefix: list[Edge] = []
    for edge in rq.path:
        prefix.append(edge)
        if table in edge.to_object.tables or table == edge.link.via:
            return list(prefix)
    return None


def _in_scope_metric_names(rq: ResolvedQuery) -> list[str]:
    return sorted(
        name for name, metric in rq.ontology.metrics.items()
        if path_to_table(rq, metric.grain) is not None
    )


def analyse(rq: ResolvedQuery) -> GrainPlan:
    plan = GrainPlan()

    for metric in rq.metrics:
        prefix = path_to_table(rq, metric.grain)
        if prefix is None:
            raise FanOutRefused(
                metric.name,
                metric.grain,
                "<not on path>",
                _in_scope_metric_names(rq),
            )

        fanning = [e for e in prefix if e.link.fans_out]
        if not fanning:
            plan.metric_plans.append(MetricPlan(metric=metric, strategy="inline"))
        else:
            plan.metric_plans.append(
                MetricPlan(
                    metric=metric,
                    strategy="aggregate_then_join",
                    forced_by=fanning[0].link.name,
                )
            )

    return plan
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_grain.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/grain.py tests/unit/test_grain.py
git commit -m "feat: grain analysis deciding inline, rewrite or refusal from cardinality alone"
```

---

### Task 9: Non-additivity — and why chasm traps turn out to be unreachable

**Files:**
- Modify: `src/grain/engine/grain.py`
- Test: `tests/unit/test_grain_additivity.py`

**Interfaces:**
- Consumes: Task 8
- Produces: `analyse` now computes `GrainPlan.additive` and `GrainPlan.non_additive_reason`

**A finding that changes the spec's §7 step 5.** The spec plans chasm detection: two or more fanning edges branching from the same relation, aggregated across both. Working the compiler through, **that state cannot be reached in v1**, for two independent reasons:

1. **`QuerySpec.traverse` is a linear list of hops**, so the walked path is a chain. After the first hop you are at the target object; there is no way to express a second branch from the same parent.
2. **A metric whose grain is off the walked path is already refused** (Task 8), so metrics cannot smuggle a second branch into scope either.

And even if a future version allows branching traversal, **aggregate-then-join structurally prevents the chasm anyway**: each metric is computed in its own subquery at its own grain and joined back on the group-by keys, so the cartesian product between two branches never forms.

So chasm detection is **not implemented**. What survives from §7 step 5 is the part that is real and reachable: **non-additivity through a `many_to_many`**, which is a different failure and needs a flag rather than a refusal. The isolation property is pinned by a regression test so it cannot silently decay.

> **Feed back to the spec:** §7 step 5 should be rewritten to say that chasm safety is a *consequence* of the aggregate-then-join rule plus a linear traverse, not a separate detection pass. Flag this to the spec owner.

- [ ] **Step 1: Extend the unit fixture**

Append to `TINY_YAML` in `tests/unit/conftest.py` — under `objects:` add:

```yaml
  Track:
    primary: track
    properties:
      name: {column: track.name, type: string}
  Playlist:
    primary: playlist
    properties:
      name: {column: playlist.name, type: string}
```

under `links:` add:

```yaml
  Track_InvoiceLines:
    from: Track
    to: InvoiceLine
    kind: direct
    on: [{from: track.track_id, to: invoice_line.track_id}]
    cardinality: one_to_many
  Playlist_Tracks:
    from: Playlist
    to: Track
    kind: through
    via: playlist_track
    on_from: [{from: playlist.playlist_id, to: playlist_track.playlist_id}]
    on_to: [{from: playlist_track.track_id, to: track.track_id}]
    cardinality: many_to_many
```

and under `metrics:` add:

```yaml
  track_count:
    grain: track
    expr: "count(distinct track.track_id)"
    type: integer
```

In `lite_metadata`, add these tables and one column:

```python
    Table("track", md, Column("track_id", Integer), Column("name", String),
          Column("album_id", Integer))
    Table("playlist", md, Column("playlist_id", Integer), Column("name", String))
    Table("playlist_track", md, Column("playlist_id", Integer), Column("track_id", Integer))
```

and add `Column("track_id", Integer)` to the existing `invoice_line` table.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_grain_additivity.py
import pytest
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

def plan_for(onto, **kw):
    return analyse(resolve(QuerySpec(**kw), onto))

def test_metric_reached_through_many_to_many_is_non_additive(chinook_lite):
    """Revenue by playlist: every group correct, and the groups overlap."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    assert plan.additive is False
    assert "many_to_many" in plan.non_additive_reason
    assert "Playlist_Tracks" in plan.non_additive_reason

def test_non_additive_query_is_still_answered_not_refused(chinook_lite):
    """Refusing would be the worse answer — the per-group numbers are wanted."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    assert plan.metric_plans[0].strategy == "aggregate_then_join"

def test_additive_stays_true_without_a_many_to_many(chinook_lite):
    plan = plan_for(chinook_lite, object="Customer", group_by=["country"],
                    metrics=["revenue"],
                    traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert plan.additive is True
    assert plan.non_additive_reason is None

def test_metrics_at_different_grains_are_isolated_from_each_other(chinook_lite):
    """The property that makes chasm detection unnecessary: each metric is
    planned independently at its own grain, so two fanning branches can never
    multiply against one another."""
    plan = plan_for(chinook_lite, object="Playlist", group_by=["name"],
                    metrics=["revenue", "track_count"],
                    traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
    grains = {mp.metric.name: mp.metric.grain for mp in plan.metric_plans}
    assert grains == {"revenue": "invoice_line", "track_count": "track"}
    assert len(plan.metric_plans) == 2

def test_a_branching_path_cannot_be_expressed(chinook_lite):
    """Documents why chasm detection is absent: traverse is a chain, so after a
    hop you are at the target and cannot branch back to the parent."""
    from grain.engine.errors import NoPath
    with pytest.raises(NoPath):
        plan_for(chinook_lite, object="Playlist",
                 traverse=[Hop(link="Playlist_Tracks"), Hop(link="Playlist_Tracks")])
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_grain_additivity.py -v`
Expected: FAIL — `test_metric_reached_through_many_to_many_is_non_additive` asserts `additive is False` but `analyse` still returns the default `True`.

- [ ] **Step 4: Implement**

In `analyse`, inside the metric loop and after the strategy has been decided, add:

```python
        for edge in prefix:
            if edge.link.cardinality == "many_to_many" and plan.additive:
                plan.additive = False
                plan.non_additive_reason = (
                    f"'{metric.name}' is grouped across '{edge.link.name}', which is "
                    f"many_to_many. Each group is correct, but the groups overlap — "
                    f"this column will not sum to the total."
                )
```

Nothing else changes. There is no chasm pass to write.

- [ ] **Step 5: Run the whole unit suite**

Run: `uv run pytest tests/unit/ -v`
Expected: PASS (all unit tests)

- [ ] **Step 6: Commit**

```bash
git add src/grain/engine/grain.py tests/unit/
git commit -m "feat: non-additivity flag for many_to_many traversals"
```

---

### Task 10: Compiler — objects, properties, filters, direct links

**Files:**
- Create: `src/grain/engine/compile.py`
- Test: `tests/unit/test_compile_basic.py`

**Interfaces:**
- Consumes: `resolve.ResolvedQuery`, `grain.GrainPlan`, SQLAlchemy `MetaData`
- Produces: `compile_query(rq, plan, metadata) -> Select`; `sql_text(select_stmt) -> str` (compiles with literal binds, for logging and tests)

**Rule from the spec's §7 step 2:** a filter reached only through `many_to_one` edges becomes a `WHERE` on a join. A filter crossing **any** fanning edge becomes an `EXISTS` subquery — never a join — so *"customers who bought a Brazilian track"* returns each customer once.

In this task all filters are on the root object, so they are plain `WHERE` clauses. Task 11 adds `EXISTS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_compile_basic.py
from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec

def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))

def test_selects_root_properties(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"])
    assert "FROM customer" in sql
    assert "customer.country" in sql

def test_equality_filter_becomes_a_where_clause(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="eq", value="Brazil")])
    assert "WHERE" in sql and "Brazil" in sql

def test_in_filter_renders_an_in_clause(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="in", value=["Brazil", "France"])])
    assert " IN " in sql.upper()

def test_is_null_filter_renders_is_null(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="country", op="is_null")])
    assert "IS NULL" in sql.upper()

def test_many_to_one_hop_becomes_a_join(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                traverse=[Hop(link="Customer_SupportRep")])
    assert "JOIN employee" in sql

def test_limit_is_applied(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"], limit=25)
    assert "LIMIT 25" in sql
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_compile_basic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.compile'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/compile.py
"""ResolvedQuery + GrainPlan -> SQLAlchemy Select. This module decides nothing;
every verdict was reached in grain.py. It only emits what was decided."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, Select, and_, select
from sqlalchemy.sql import ColumnElement

from .grain import GrainPlan
from .resolve import Edge, ResolvedFilter, ResolvedProperty, ResolvedQuery


def sql_text(stmt: Select) -> str:
    """Render SQL with literal binds — for logging, provenance and tests."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _column(metadata: MetaData, table: str, column: str) -> Column[Any]:
    return metadata.tables[table].columns[column]


def _property_column(metadata: MetaData, rp: ResolvedProperty) -> Column[Any]:
    return _column(metadata, rp.prop.column.table, rp.prop.column.column)


def _filter_clause(metadata: MetaData, rf: ResolvedFilter) -> ColumnElement[bool]:
    col = _property_column(metadata, rf.property)
    op = rf.op
    if op == "eq":
        return col == rf.value
    if op == "ne":
        return col != rf.value
    if op == "gt":
        return col > rf.value
    if op == "gte":
        return col >= rf.value
    if op == "lt":
        return col < rf.value
    if op == "lte":
        return col <= rf.value
    if op == "in":
        return col.in_(rf.value)
    if op == "like":
        return col.like(rf.value)
    if op == "is_null":
        return col.is_(None)
    raise AssertionError(f"unhandled filter op '{op}'")  # spec.FilterOp is closed


def _edge_onclause(metadata: MetaData, edge: Edge) -> ColumnElement[bool]:
    pairs = edge.link.on
    return and_(
        *[
            _column(metadata, p.from_.table, p.from_.column)
            == _column(metadata, p.to.table, p.to.column)
            for p in pairs
        ]
    )


def _apply_object_joins(stmt: Select, metadata: MetaData, obj) -> Select:
    """Join the extra tables ONE object spans. Always outer — an inner join to
    `genre` would silently drop every track without one.

    Call this for an object only once that object is itself in scope, or the
    extra tables reference a FROM element that does not exist yet.
    """
    for join in obj.joins.values():
        onclause = and_(
            *[
                _column(metadata, p.from_.table, p.from_.column)
                == _column(metadata, p.to.table, p.to.column)
                for p in join.on
            ]
        )
        stmt = stmt.join(metadata.tables[join.to], onclause, isouter=(join.kind == "left"))
    return stmt


def compile_query(rq: ResolvedQuery, plan: GrainPlan, metadata: MetaData) -> Select:
    group_cols = [
        _property_column(metadata, rp).label(rp.name) for rp in rq.group_by
    ]
    stmt = select(*group_cols).select_from(metadata.tables[rq.root.primary])
    stmt = _apply_object_joins(stmt, metadata, rq.root)

    for edge in rq.path:
        if edge.link.kind == "direct":
            stmt = stmt.join(
                metadata.tables[edge.to_object.primary], _edge_onclause(metadata, edge)
            )
        stmt = _apply_object_joins(stmt, metadata, edge.to_object)

    clauses = [_filter_clause(metadata, rf) for rf in rq.filters]
    if clauses:
        stmt = stmt.where(and_(*clauses))

    if group_cols:
        stmt = stmt.group_by(*[_property_column(metadata, rp) for rp in rq.group_by])

    return stmt.limit(rq.limit)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_compile_basic.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/compile.py tests/unit/test_compile_basic.py
git commit -m "feat: compile object selection, filters and direct-link joins"
```

---

### Task 11: `through` links and EXISTS filter semantics

**Files:**
- Modify: `src/grain/engine/compile.py`, `src/grain/engine/resolve.py`
- Test: `tests/unit/test_compile_through.py`

**Interfaces:**
- Consumes: Task 10
- Produces: `compile_query` handles `kind="through"` by joining `via` then the target; filters that cross a fanning edge compile to `EXISTS`. Adds `ResolvedFilter.hops` (list of link names traversed to reach the filtered property) and extends `Filter.property` to accept a dotted `Link.property` form.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_compile_through.py
from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Filter, Hop, QuerySpec

def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))

def test_through_link_joins_via_table_then_target(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Playlist", group_by=["name"],
                traverse=[Hop(link="Playlist_Tracks")])
    assert "JOIN playlist_track" in sql
    assert "JOIN track" in sql

def test_join_table_never_appears_as_a_selected_entity(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Playlist", group_by=["name"],
                traverse=[Hop(link="Playlist_Tracks")])
    assert "playlist_track." not in sql.split("WHERE")[0].split("SELECT")[1]

def test_filter_across_a_fanning_edge_uses_exists_not_join(chinook_lite, lite_metadata):
    """Customers who bought something — each customer once, not once per line."""
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                filters=[Filter(property="Customer_Invoices.total", op="gt", value=5)])
    assert "EXISTS" in sql.upper()
    assert "JOIN invoice" not in sql
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_compile_through.py -v`
Expected: FAIL — the dotted filter property raises `UnknownName`, and `through` links emit no join.

- [ ] **Step 3: Implement**

In `resolve.py`, replace the filter-building line in `resolve()` with a helper that accepts `Link.property`:

```python
def _resolve_filter(spec_filter, root: ObjectType, onto: Ontology) -> ResolvedFilter:
    name = spec_filter.property
    if "." not in name:
        return ResolvedFilter(_property(root, name), spec_filter.op, spec_filter.value, [])
    link_name, _, prop_name = name.partition(".")
    if link_name not in onto.links:
        raise UnknownName("link", link_name, suggest(link_name, list(onto.links)))
    link = onto.links[link_name]
    if link.from_ != root.name:
        raise NoPath(root.name, link.to, [l.name for l in onto.links_from(root.name)])
    target = _object(onto, link.to)
    return ResolvedFilter(_property(target, prop_name), spec_filter.op,
                          spec_filter.value, [link])
```

Add `hops: list[LinkType] = field(default_factory=list)` to `ResolvedFilter` (change it from `frozen=True` to a plain `@dataclass`), and call `_resolve_filter` for each spec filter.

In `compile.py`, extract the path loop into a **shared helper** — Task 13's metric
subquery must apply exactly the same joins, and a second direct-only copy of this
loop would silently drop `through` and `recursive` edges from rewritten metrics:

```python
def _apply_path(stmt: Select, metadata: MetaData, rq: ResolvedQuery) -> Select:
    """Apply every edge on the walked path, plus each target's object joins.

    Task 13's aggregate-then-join subquery calls this too — one definition, so a
    metric behind a `through` link cannot end up with a subquery missing joins.
    """
    for edge in rq.path:
        stmt = _apply_edge(stmt, metadata, edge)
        stmt = _apply_object_joins(stmt, metadata, edge.to_object)
    return stmt
```

with `_apply_edge` handling all three kinds. Handle `through` links inside it:

```python
        elif edge.link.kind == "through":
            via = metadata.tables[edge.link.via]
            stmt = stmt.join(
                via,
                and_(*[
                    _column(metadata, p.from_.table, p.from_.column)
                    == _column(metadata, p.to.table, p.to.column)
                    for p in edge.link.on_from
                ]),
            )
            stmt = stmt.join(
                metadata.tables[edge.to_object.primary],
                and_(*[
                    _column(metadata, p.from_.table, p.from_.column)
                    == _column(metadata, p.to.table, p.to.column)
                    for p in edge.link.on_to
                ]),
            )
```

and replace the filter block with one that routes fanning filters to `EXISTS`:

```python
    plain, existential = [], []
    for rf in rq.filters:
        (existential if any(l.fans_out for l in rf.hops) else plain).append(rf)

    if plain:
        stmt = stmt.where(and_(*[_filter_clause(metadata, rf) for rf in plain]))

    for rf in existential:
        link = rf.hops[0]
        target = metadata.tables[rf.property.prop.column.table]
        sub = (
            select(1)
            .select_from(target)
            .where(
                and_(
                    *[
                        _column(metadata, p.from_.table, p.from_.column)
                        == _column(metadata, p.to.table, p.to.column)
                        for p in link.on
                    ],
                    _filter_clause(metadata, rf),
                )
            )
        )
        stmt = stmt.where(sub.exists())
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/ -v`
Expected: PASS (all unit tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/compile.py src/grain/engine/resolve.py tests/unit/test_compile_through.py
git commit -m "feat: through-link joins and EXISTS semantics for fanning filters"
```

---

### Task 12: Recursive links

**Files:**
- Modify: `src/grain/engine/compile.py`
- Test: `tests/unit/test_compile_recursive.py`, `tests/integration/test_recursive.py`

**Interfaces:**
- Consumes: Task 11
- Produces: `compile_query` emits a `WITH RECURSIVE` CTE for `kind="recursive"`, bounded by `max_depth` and guarded against cycles by tracking the visited-id path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_compile_recursive.py
from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))

def test_recursive_link_emits_a_recursive_cte(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager")])
    assert "RECURSIVE" in sql.upper()

def test_depth_bound_is_present(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Employee", group_by=["last_name"],
                traverse=[Hop(link="Employee_Manager", max_depth=3)])
    assert "3" in sql
```

```python
# tests/integration/test_recursive.py
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

def test_employee_hierarchy_is_three_levels(db_engine):
    """Anchor from the spec's §3: 1 -> 2 -> 5."""
    sql = text("""
        with recursive h as (
          select employee_id, reports_to, 1 as lvl from employee where reports_to is null
          union all
          select e.employee_id, e.reports_to, h.lvl + 1
          from employee e join h on e.reports_to = h.employee_id
        )
        select lvl, count(*) from h group by lvl order by lvl
    """)
    with db_engine.connect() as conn:
        assert [tuple(r) for r in conn.execute(sql)] == [(1, 1), (2, 2), (3, 5)]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/unit/test_compile_recursive.py -v`
Expected: FAIL — no `RECURSIVE` in the emitted SQL.

- [ ] **Step 3: Implement**

Add to `compile.py`, and call it from the path loop for `kind == "recursive"`:

```python
def _recursive_cte(metadata: MetaData, edge: Edge):
    """A self-referential link becomes a depth-bounded recursive CTE.

    `depth` bounds runaway recursion; `path` carries the visited ids so a cycle
    in the data terminates instead of looping forever.
    """
    table = metadata.tables[edge.to_object.primary]
    pair = edge.link.on[0]
    child_col = table.columns[pair.from_.column]   # e.g. employee.reports_to
    parent_col = table.columns[pair.to.column]     # e.g. employee.employee_id

    base = (
        select(
            table,
            literal(1).label("depth"),
            array((parent_col,)).label("path"),
        )
        .where(child_col.is_(None))
        .cte(name=f"{edge.link.name.lower()}_cte", recursive=True)
    )
    step = select(
        table,
        (base.c.depth + 1).label("depth"),
        (base.c.path + array((parent_col,))).label("path"),
    ).join(base, child_col == base.c[pair.to.column]).where(
        and_(base.c.depth < edge.link.max_depth, ~parent_col.eq(func.any(base.c.path)))
    )
    return base.union_all(step)
```

Add the imports `from sqlalchemy import func, literal` and `from sqlalchemy.dialects.postgresql import array`.

In the path loop:

```python
        elif edge.link.kind == "recursive":
            cte = _recursive_cte(metadata, edge)
            # C5: join against the CTE's own columns, not the base table's —
            # _edge_onclause would compare the base table to itself.
            pair = edge.link.on[0]
            stmt = stmt.join(
                cte,
                _column(metadata, pair.to.table, pair.to.column) == cte.c[pair.to.column],
            )
```

- [ ] **Step 4: Run and confirm they pass**

Run: `uv run pytest tests/unit/test_compile_recursive.py tests/integration/test_recursive.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/compile.py tests/unit/test_compile_recursive.py tests/integration/test_recursive.py
git commit -m "feat: depth-bounded recursive CTEs with cycle guard"
```

---

### Task 13: Metrics — inline, aggregate-then-join, refusal

**Files:**
- Modify: `src/grain/engine/compile.py`
- Test: `tests/unit/test_compile_metrics.py`, `tests/integration/test_measured_anchors.py`

**Interfaces:**
- Consumes: Tasks 10–12, `grain.MetricPlan`
- Produces: `compile_query` emits inline aggregates for `strategy="inline"`; for `aggregate_then_join` it computes the metric in a subquery grouped at the metric's own grain and `LEFT JOIN`s it back onto a materialised key set. The metric expression is rendered with `text(metric.expr)` — safe because the loader proved it references only grain-table columns.

**This is the task the project exists for.** The integration test is non-negotiable: `2328.60` must appear twice and `20848.62` must be unreachable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_compile_metrics.py
from grain.engine.compile import compile_query, sql_text
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec

def build(onto, metadata, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    return sql_text(compile_query(rq, analyse(rq), metadata))

def test_inline_metric_aggregates_without_a_subquery(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["customer_count"])
    assert "count(distinct customer.customer_id)" in sql.lower()
    assert sql.lower().count("select") == 1

def test_fanned_metric_is_computed_in_its_own_subquery(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["revenue"],
                traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sql.lower().count("select") >= 2
    assert "left outer join" in sql.lower() or "left join" in sql.lower()

def test_two_grains_produce_two_subqueries(chinook_lite, lite_metadata):
    sql = build(chinook_lite, lite_metadata, object="Customer", group_by=["country"],
                metrics=["revenue", "invoice_total"],
                traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sql.lower().count("group by") >= 3
```

```python
# tests/integration/test_measured_anchors.py
"""Every value here was measured against the loaded database on 2026-08-17.
These are regressions against reality, not expectations invented in the abstract."""
import pytest
from decimal import Decimal
from grain.engine.compile import compile_query
from grain.engine.grain import analyse
from grain.engine.resolve import resolve
from grain.engine.spec import Hop, QuerySpec
from grain.engine.errors import FanOutRefused

pytestmark = pytest.mark.integration

def run(db_engine, chinook_metadata, onto, **kw):
    rq = resolve(QuerySpec(**kw), onto)
    stmt = compile_query(rq, analyse(rq), chinook_metadata)
    with db_engine.connect() as conn:
        return conn.execute(stmt).all()

def test_revenue_at_line_grain_is_2328_60(db_engine, chinook_metadata, chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="InvoiceLine",
               metrics=["revenue"])
    assert rows[0][-1] == Decimal("2328.60")

def test_invoice_total_at_invoice_grain_is_also_2328_60(db_engine, chinook_metadata,
                                                        chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Invoice",
               metrics=["invoice_total"])
    assert rows[0][-1] == Decimal("2328.60")

def test_revenue_by_country_totals_2328_60(db_engine, chinook_metadata, chinook_ontology):
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["revenue"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sum(r[-1] for r in rows) == Decimal("2328.60")

def test_the_inflated_figure_is_unreachable(db_engine, chinook_metadata, chinook_ontology):
    """20848.62 must not be producible through any spec the engine accepts."""
    rows = run(db_engine, chinook_metadata, chinook_ontology, object="Customer",
               group_by=["country"], metrics=["invoice_total"], limit=100,
               traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")])
    assert sum(r[-1] for r in rows) == Decimal("2328.60")
    assert sum(r[-1] for r in rows) != Decimal("20848.62")

def test_metric_whose_grain_is_off_path_refuses(chinook_ontology):
    with pytest.raises(FanOutRefused):
        analyse(resolve(QuerySpec(object="Genre", metrics=["revenue"]), chinook_ontology))
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/unit/test_compile_metrics.py -v`
Expected: FAIL — no aggregate appears in the emitted SQL.

- [ ] **Step 3: Implement**

Replace `compile_query` with a version that branches on strategy:

```python
def _metric_expr(metric) -> ColumnElement[Any]:
    """Safe because the loader proved every token belongs to the grain table."""
    return text(metric.expr)


def _inline_metrics(stmt: Select, plans: list[MetricPlan]) -> Select:
    for mp in plans:
        stmt = stmt.add_columns(_metric_expr(mp.metric).label(mp.metric.name))
    return stmt


def _aggregate_then_join(
    stmt: Select, metadata: MetaData, rq: ResolvedQuery, mp: MetricPlan
) -> Select:
    """Compute the metric at its own grain, then LEFT JOIN it back on the keys.

    The outer key set is materialised from the root object so that groups with
    no matching facts survive as zero rather than vanishing.
    """
    keys = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    sub = select(*keys, _metric_expr(mp.metric).label(mp.metric.name))
    sub = sub.select_from(metadata.tables[rq.root.primary])
    sub = _apply_object_joins(sub, metadata, rq.root)
    sub = _apply_path(sub, metadata, rq)   # C2: same joins as the outer query —
                                           # never a direct-only copy of the loop
    sub = sub.group_by(*[_property_column(metadata, rp) for rp in rq.group_by]).subquery()

    onclause = and_(
        *[_property_column(metadata, rp) == sub.c[rp.name] for rp in rq.group_by]
    )
    return stmt.join(sub, onclause, isouter=True).add_columns(
        func.coalesce(sub.c[mp.metric.name], 0).label(mp.metric.name)
    )
```

Then in `compile_query`, after applying joins and filters:

```python
    inline = [mp for mp in plan.metric_plans if mp.strategy == "inline"]
    rewritten = [mp for mp in plan.metric_plans if mp.strategy == "aggregate_then_join"]

    stmt = _inline_metrics(stmt, inline)
    for mp in rewritten:
        stmt = _aggregate_then_join(stmt, metadata, rq, mp)
```

**C3 — SQLAlchemy 2 rejects an empty `select()`.** Verified against 2.0.52:
`select().select_from(t).add_columns(...)` raises `NotImplementedError`. So
`compile_query` must never call `select()` with zero columns. Restructure the
opening of `compile_query` to assemble the full column list first:

```python
    group_cols = [_property_column(metadata, rp).label(rp.name) for rp in rq.group_by]
    inline_cols = [
        _metric_expr(mp.metric).label(mp.metric.name)
        for mp in plan.metric_plans if mp.strategy == "inline"
    ]
    if not group_cols and not inline_cols:
        # Only rewritten metrics and no keys to join them back on: there is no
        # correct query to emit, so refuse rather than guess.
        first = next(mp for mp in plan.metric_plans if mp.strategy == "aggregate_then_join")
        raise FanOutRefused(
            first.metric.name, first.metric.grain, first.forced_by or "<path>",
            [f"add a group_by key, or choose a metric at {rq.root.primary} grain"],
        )
    stmt = select(*group_cols, *inline_cols).select_from(metadata.tables[rq.root.primary])
```

`_inline_metrics` is then no longer needed as a separate step — the inline columns
are already in the `select()`. Delete it and keep only the `_aggregate_then_join`
loop. Import `FanOutRefused` from `.errors`.

Import `text` and `func` from `sqlalchemy`, and `MetricPlan` from `.grain`.

- [ ] **Step 4: Run and confirm they pass**

Run: `uv run pytest tests/unit/test_compile_metrics.py tests/integration/test_measured_anchors.py -v`
Expected: PASS (8 tests). If `test_the_inflated_figure_is_unreachable` fails, **stop** — the core claim of the project is broken and no later task matters.

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/compile.py tests/unit/test_compile_metrics.py tests/integration/test_measured_anchors.py
git commit -m "feat: grain-correct metric compilation with aggregate-then-join rewrite"
```

---

### Task 14: Guard, executor and the Grain facade

**Files:**
- Create: `src/grain/engine/guard.py`, `src/grain/engine/execute.py`, `src/grain/engine/api.py`
- Test: `tests/unit/test_guard.py`, `tests/integration/test_api.py`

**Interfaces:**
- Consumes: everything above
- Produces: `GuardConfig(statement_timeout_ms, row_cap)`; `guarded_connection(engine, config)` context manager; `Rewrite(metric, strategy, forced_by)`; `Result(rows, columns, compiled_sql, rewrites, additive, non_additive_reason, ontology_elements_used)`; `Grain.load(domain_dir, engine, guard=None)`, `Grain.explain(spec) -> dict`, `Grain.query(spec) -> Result`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_guard.py
from grain.engine.guard import GuardConfig

def test_defaults_are_conservative():
    cfg = GuardConfig()
    assert cfg.statement_timeout_ms == 10_000
    assert cfg.row_cap == 10_000
```

```python
# tests/integration/test_api.py
import pytest
from decimal import Decimal
from grain.engine.api import Grain
from grain.engine.spec import Hop, QuerySpec
from grain.domains.chinook import CHINOOK_DIR

pytestmark = pytest.mark.integration

@pytest.fixture(scope="session")
def g(db_engine):
    return Grain.load(CHINOOK_DIR, db_engine)

def test_query_returns_rows_and_compiled_sql(g):
    result = g.query(QuerySpec(object="Customer", group_by=["country"],
                               metrics=["customer_count"], limit=100))
    assert result.rows
    assert "SELECT" in result.compiled_sql.upper()

def test_rewrite_is_surfaced_when_the_engine_changes_the_query(g):
    result = g.query(QuerySpec(
        object="Customer", group_by=["country"], metrics=["revenue"], limit=100,
        traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")]))
    assert len(result.rewrites) == 1
    assert result.rewrites[0].metric == "revenue"
    assert result.rewrites[0].strategy == "aggregate_then_join"
    assert result.rewrites[0].forced_by == "Customer_Invoices"

def test_no_rewrite_is_reported_when_none_happened(g):
    result = g.query(QuerySpec(object="Customer", group_by=["country"],
                               metrics=["customer_count"], limit=100))
    assert result.rewrites == []

def test_additive_flag_is_false_across_a_many_to_many(g):
    result = g.query(QuerySpec(
        object="Playlist", group_by=["name"], metrics=["revenue"], limit=100,
        traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")]))
    assert result.additive is False
    assert "many_to_many" in result.non_additive_reason

def test_explain_returns_sql_without_executing(g):
    out = g.explain(QuerySpec(object="Customer", group_by=["country"],
                              metrics=["customer_count"], limit=100))
    assert "SELECT" in out["compiled_sql"].upper()
    assert "rows" not in out

def test_row_cap_is_enforced(g):
    result = g.query(QuerySpec(object="Track", group_by=["name"], limit=5))
    assert len(result.rows) <= 5
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/unit/test_guard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.guard'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/guard.py
"""Defence in depth. The typed spec already makes writes inexpressible; the
read-only role means a compiler bug still cannot mutate anything."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import Engine, Connection, text


@dataclass(frozen=True)
class GuardConfig:
    statement_timeout_ms: int = 10_000
    row_cap: int = 10_000


@contextmanager
def guarded_connection(engine: Engine, config: GuardConfig) -> Iterator[Connection]:
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(config.statement_timeout_ms)}"))
        yield conn
        conn.rollback()  # read-only by construction; never leave a transaction open
```

```python
# src/grain/engine/execute.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Select

from .compile import sql_text
from .errors import GuardTripped
from .guard import GuardConfig, guarded_connection


@dataclass(frozen=True)
class Rewrite:
    metric: str
    strategy: str
    forced_by: str   # the link NAME that forced the rewrite
    reason: str      # human-readable: "<link> is one_to_many"


@dataclass
class Result:
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    compiled_sql: str = ""
    rewrites: list[Rewrite] = field(default_factory=list)
    additive: bool = True
    non_additive_reason: str | None = None
    ontology_elements_used: list[str] = field(default_factory=list)


def execute(engine: Engine, stmt: Select, config: GuardConfig) -> tuple[list, list[str]]:
    with guarded_connection(engine, config) as conn:
        cursor = conn.execute(stmt)
        rows = cursor.fetchmany(config.row_cap + 1)
        if len(rows) > config.row_cap:
            raise GuardTripped("row_cap", config.row_cap)
        return [tuple(r) for r in rows], list(cursor.keys())
```

```python
# src/grain/engine/api.py
"""The library IS the product. Adapters (CLI, MCP, your own chat harness) are
thin wrappers over this class and add no logic of their own."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, MetaData

from .compile import compile_query, sql_text
from .execute import Result, Rewrite, execute
from .grain import analyse
from .guard import GuardConfig
from .loader import load_ontology
from .resolve import resolve
from .spec import QuerySpec


class Grain:
    def __init__(self, ontology, metadata: MetaData, engine: Engine,
                 guard: GuardConfig | None = None) -> None:
        self.ontology = ontology
        self.metadata = metadata
        self.engine = engine
        self.guard = guard or GuardConfig()

    @classmethod
    def load(cls, domain_dir: Path, engine: Engine,
             guard: GuardConfig | None = None) -> "Grain":
        metadata = MetaData()
        metadata.reflect(bind=engine)
        ontology = load_ontology(Path(domain_dir) / "ontology.yaml", metadata)
        return cls(ontology, metadata, engine, guard)

    def _plan(self, spec: QuerySpec):
        rq = resolve(spec, self.ontology)
        plan = analyse(rq)
        return rq, plan, compile_query(rq, plan, self.metadata)

    def explain(self, spec: QuerySpec) -> dict[str, Any]:
        rq, plan, stmt = self._plan(spec)
        return {
            "compiled_sql": sql_text(stmt),
            "rewrites": [
                {"metric": mp.metric.name, "strategy": mp.strategy,
                 "forced_by": mp.forced_by}
                for mp in plan.metric_plans if mp.forced_by
            ],
            "additive": plan.additive,
            "non_additive_reason": plan.non_additive_reason,
        }

    def query(self, spec: QuerySpec) -> Result:
        rq, plan, stmt = self._plan(spec)
        rows, columns = execute(self.engine, stmt, self.guard)
        return Result(
            rows=rows,
            columns=columns,
            compiled_sql=sql_text(stmt),
            rewrites=[
                Rewrite(
                    metric=mp.metric.name,
                    strategy=mp.strategy,
                    forced_by=mp.forced_by,
                    reason=(
                        f"{mp.forced_by} is "
                        f"{self.ontology.links[mp.forced_by].cardinality}"
                    ),
                )
                for mp in plan.metric_plans if mp.forced_by
            ],
            additive=plan.additive,
            non_additive_reason=plan.non_additive_reason,
            ontology_elements_used=(
                [rq.root.name]
                + [e.link.name for e in rq.path]
                + [m.name for m in rq.metrics]
            ),
        )
```

- [ ] **Step 4: Run and confirm they pass**

Run: `uv run pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/guard.py src/grain/engine/execute.py src/grain/engine/api.py tests/
git commit -m "feat: guard, executor and the Grain facade returning rewrites and additivity"
```

---

### Task 15: `describe_ontology`

**Files:**
- Create: `src/grain/engine/describe.py`
- Modify: `src/grain/engine/api.py` (add `Grain.describe`)
- Test: `tests/unit/test_describe.py`

**Interfaces:**
- Consumes: `ontology.Ontology`
- Produces: `describe(onto, object_name=None) -> dict` and `NON_ADDITIVITY_RULE` constant. `Grain.describe(object=None) -> dict`.

**S1's resolution, implemented.** `describe_ontology` states the **rule** once rather than enumerating every `(metric × dimension)` pair — constant cost, total coverage, unchanged at 200 tables.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_describe.py
from grain.engine.describe import NON_ADDITIVITY_RULE, describe

def test_lists_objects_links_and_metrics(chinook_lite):
    out = describe(chinook_lite)
    assert "Customer" in out["objects"]
    assert "Customer_Invoices" in out["links"]
    assert "revenue" in out["metrics"]

def test_metrics_report_their_grain(chinook_lite):
    assert describe(chinook_lite)["metrics"]["revenue"]["grain"] == "invoice_line"

def test_links_report_cardinality(chinook_lite):
    links = describe(chinook_lite)["links"]
    assert links["Customer_Invoices"]["cardinality"] == "one_to_many"
    assert links["Playlist_Tracks"]["cardinality"] == "many_to_many"

def test_states_the_non_additivity_rule_once(chinook_lite):
    out = describe(chinook_lite)
    assert "many_to_many" in out["rules"]["non_additivity"]
    assert out["rules"]["non_additivity"] == NON_ADDITIVITY_RULE

def test_does_not_enumerate_metric_dimension_pairs(chinook_lite):
    """S1: the rule scales; an enumeration would grow multiplicatively."""
    out = describe(chinook_lite)
    for metric in out["metrics"].values():
        assert "non_additive_dimensions" not in metric

def test_single_object_view_is_narrower(chinook_lite):
    out = describe(chinook_lite, "Customer")
    assert set(out["objects"]) == {"Customer"}
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/test_describe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'grain.engine.describe'`

- [ ] **Step 3: Implement**

```python
# src/grain/engine/describe.py
"""How the agent learns the domain. This replaces dumping a schema: it is
smaller than the DDL and stated in the language the question is asked in."""
from __future__ import annotations

from typing import Any

from .ontology import Ontology

NON_ADDITIVITY_RULE = (
    "A metric grouped by a dimension reached through a many_to_many link is "
    "non-additive — the groups overlap, and the column will not sum to the total. "
    "Each group is still correct on its own."
)

GRAIN_RULE = (
    "Every metric is aggregated at its declared grain. If the query path fans out "
    "relative to that grain, the engine rewrites the query rather than double-counting, "
    "and reports the rewrite. If it cannot, it refuses and names the alternative."
)


def _ai(ctx) -> dict[str, Any]:
    if ctx is None:
        return {}
    return {k: v for k, v in {"synonyms": ctx.synonyms,
                              "instructions": ctx.instructions}.items() if v}


def describe(onto: Ontology, object_name: str | None = None) -> dict[str, Any]:
    names = [object_name] if object_name else list(onto.objects)
    return {
        "domain": onto.name,
        "description": onto.description,
        "rules": {"grain": GRAIN_RULE, "non_additivity": NON_ADDITIVITY_RULE},
        "objects": {
            name: {
                "description": onto.objects[name].description,
                "properties": {
                    p: {"type": prop.type, "nullable": prop.nullable}
                    for p, prop in onto.objects[name].properties.items()
                },
                **({"ai_context": _ai(onto.objects[name].ai_context)}
                   if onto.objects[name].ai_context else {}),
            }
            for name in names
        },
        "links": {
            name: {
                "from": link.from_, "to": link.to, "kind": link.kind,
                "cardinality": link.cardinality, "description": link.description,
            }
            for name, link in onto.links.items()
            if object_name is None or object_name in (link.from_, link.to)
        },
        "metrics": {
            name: {
                "grain": metric.grain, "type": metric.type,
                "description": metric.description,
                **({"ai_context": _ai(metric.ai_context)} if metric.ai_context else {}),
            }
            for name, metric in onto.metrics.items()
        },
    }
```

Add to `api.py`:

```python
    def describe(self, object: str | None = None) -> dict[str, Any]:
        from .describe import describe as _describe
        return _describe(self.ontology, object)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `uv run pytest tests/unit/test_describe.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/grain/engine/describe.py src/grain/engine/api.py tests/unit/test_describe.py
git commit -m "feat: describe_ontology stating the grain and non-additivity rules once"
```

---

### Task 16: CLI and MCP adapters

**Files:**
- Create: `src/grain/engine/cli.py`, `src/grain/engine/server.py`
- Test: `tests/integration/test_cli.py`, `tests/unit/test_boundary.py`

**Interfaces:**
- Consumes: `api.Grain`
- Produces: `grain describe|explain|query` CLI; an MCP server exposing `describe_ontology`, `explain`, `query`

**The boundary test is the point of this task.** `engine/` must not import an adapter or a domain — that is the architecture test made continuous rather than checked once at milestone 8.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_boundary.py
"""The architecture test, enforced continuously. Milestone 8 proves the boundary
holds once; this keeps it true between milestones."""
import ast
import pathlib
import pytest

ENGINE = pathlib.Path(__file__).parents[2] / "src" / "grain" / "engine"
ADAPTERS = {"cli", "server"}

def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found

@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")))
def test_engine_never_imports_a_domain(path):
    if path.stem in ADAPTERS:
        return  # adapters may name a default domain; the engine core may not
    assert not any("domains" in imp for imp in _imports(path)), (
        f"{path.name} imports a domain module — domain packs are located by path, "
        f"never imported by name."
    )

@pytest.mark.parametrize("path", sorted(ENGINE.glob("*.py")))
def test_core_engine_never_imports_an_adapter(path):
    if path.stem in ADAPTERS:
        return
    assert not any(imp.rsplit(".", 1)[-1] in ADAPTERS for imp in _imports(path)), (
        f"{path.name} imports an adapter — the library is the product."
    )
```

```python
# tests/integration/test_cli.py
import json
import subprocess
import sys
import pytest

pytestmark = pytest.mark.integration

def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "grain.engine.cli", *args],
                          capture_output=True, text=True, check=True)

def test_describe_emits_json_with_the_rules():
    out = json.loads(run_cli("describe").stdout)
    assert "non_additivity" in out["rules"]

def test_explain_emits_sql_without_executing():
    spec = json.dumps({"object": "Customer", "group_by": ["country"],
                       "metrics": ["customer_count"]})
    out = json.loads(run_cli("explain", "--spec", spec).stdout)
    assert "SELECT" in out["compiled_sql"].upper()
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/unit/test_boundary.py -v`
Expected: PASS already (no adapters exist yet) — this test guards the next step. Then run the CLI test: FAIL with `No module named grain.engine.cli`.

- [ ] **Step 3: Implement the CLI**

```python
# src/grain/engine/cli.py
"""Thin adapter. Everything it does, the library already did."""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine

from .api import Grain
from .spec import QuerySpec


def _default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    return str(obj)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grain")
    parser.add_argument("command", choices=["describe", "explain", "query"])
    parser.add_argument("--spec", help="QuerySpec as JSON")
    parser.add_argument("--object", help="object name, for describe")
    parser.add_argument("--domain", default=None, help="path to a domain pack directory")
    args = parser.parse_args(argv)

    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        print("GRAIN_DATABASE_URL is not set", file=sys.stderr)
        return 2

    if args.domain:
        domain_dir = Path(args.domain)
    else:
        from grain.domains.chinook import CHINOOK_DIR
        domain_dir = CHINOOK_DIR

    g = Grain.load(domain_dir, create_engine(url, future=True))

    if args.command == "describe":
        print(json.dumps(g.describe(args.object), indent=2, default=_default))
        return 0

    if not args.spec:
        print("--spec is required", file=sys.stderr)
        return 2
    spec = QuerySpec.model_validate_json(args.spec)

    if args.command == "explain":
        print(json.dumps(g.explain(spec), indent=2, default=_default))
        return 0

    result = g.query(spec)
    print(json.dumps({
        "columns": result.columns,
        "rows": result.rows,
        "compiled_sql": result.compiled_sql,
        "rewrites": [r.__dict__ for r in result.rewrites],
        "additive": result.additive,
        "non_additive_reason": result.non_additive_reason,
    }, indent=2, default=_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: the CLI names `grain.domains.chinook` as its default domain. That is legitimate — an adapter may pick a default; the **engine core** may not. The boundary test exempts `cli` and `server` for exactly this reason, and enforces the rule strictly everywhere else. The import stays function-scoped so importing the module never drags a domain in.

- [ ] **Step 4: Implement the MCP server**

```python
# src/grain/engine/server.py
"""MCP adapter — the transport, and nothing else. Any logic that appears here
has to be re-implemented in every other harness, and will drift."""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine

from .api import Grain
from .errors import GrainError
from .spec import QuerySpec

mcp = FastMCP("grain")


def _grain() -> Grain:
    url = os.environ["GRAIN_DATABASE_URL"]
    domain = os.environ.get("GRAIN_DOMAIN")
    if domain:
        domain_dir = Path(domain)
    else:
        from grain.domains.chinook import CHINOOK_DIR
        domain_dir = CHINOOK_DIR
    return Grain.load(domain_dir, create_engine(url, future=True))


@mcp.tool()
def describe_ontology(object: str | None = None) -> dict:
    """List the object types, links and metrics available, with the rules that
    govern how metrics may be aggregated."""
    return _grain().describe(object)


@mcp.tool()
def explain(spec: dict) -> dict:
    """Compile a QuerySpec to SQL and report the grain analysis. Executes nothing."""
    try:
        return _grain().explain(QuerySpec.model_validate(spec))
    except GrainError as exc:
        return {"error": type(exc).__name__, "message": str(exc),
                "alternatives": exc.alternatives}


@mcp.tool()
def query(spec: dict) -> dict:
    """Run a QuerySpec. Returns rows plus the compiled SQL, any rewrites the
    engine applied, and whether the result is additive."""
    try:
        result = _grain().query(QuerySpec.model_validate(spec))
    except GrainError as exc:
        return {"error": type(exc).__name__, "message": str(exc),
                "alternatives": exc.alternatives}
    return {
        "columns": result.columns,
        "rows": [[str(v) for v in row] for row in result.rows],
        "compiled_sql": result.compiled_sql,
        "rewrites": [r.__dict__ for r in result.rewrites],
        "additive": result.additive,
        "non_additive_reason": result.non_additive_reason,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest tests/ -v`
Expected: PASS — all unit and integration tests, including both boundary tests.

- [ ] **Step 6: Register the MCP server and smoke-test it by hand**

```bash
claude mcp add grain -- uv run --directory ~/projects/grain python -m grain.engine.server
```

Then ask, in a fresh session: *"What's total revenue by country?"* Confirm the agent calls `describe_ontology`, then `query`, and that the returned `compiled_sql` contains a subquery. Ask *"and by playlist?"* — confirm `additive: false` comes back.

- [ ] **Step 7: Commit**

```bash
git add src/grain/engine/cli.py src/grain/engine/server.py tests/
git commit -m "feat: CLI and MCP adapters with a continuous engine-boundary test"
```

---

## Self-Review

**Spec coverage.** §5 object types → Task 3, 5. §5 link types incl. all three kinds → Tasks 3, 5, 10, 11, 12. §5 metrics with grain → Tasks 3, 5, 13. §6 QuerySpec → Task 6. §7 step 1 join graph → Task 7. §7 step 2 EXISTS filters → Task 11. §7 step 3 grain analysis → Task 8. §7 step 3b additivity → Task 9. §7 step 3c rewrites surfaced → Task 14. §7 step 4 multi-grain subqueries → Task 13. §7 step 5 chasm → Task 9. §7 step 6 recursive CTE → Task 12. §7 step 7 emit + compiled SQL → Tasks 10, 14. §8 guard → Task 14. §9 three tools + result shape → Tasks 14, 15, 16. §10 error taxonomy → Task 2. §11 unit and integration tiers → throughout; §11 eval tier is out of scope by design. §2.1 standards: MCP → Task 16; `ai_context` → Tasks 3, 5, 15.

**Chasm detection dropped, with reason.** §7 step 5 of the spec plans a chasm pass. Task 9 shows it is unreachable in v1 — `traverse` is linear, and off-path metric grains are already refused — and unnecessary in general, because aggregate-then-join isolates each metric at its own grain. The isolation property is pinned by a regression test instead. **This should be fed back into the spec.**

**Known gaps, deliberate.** Milestones 8–10 are a separate plan. `AmbiguousPath` is defined in Task 2 but never raised — Chinook has no two links with the same `(from, to)` pair, so there is nothing to trigger it until a second domain arrives; it is defined now so the vocabulary is complete. Filters currently reach one hop deep (`Link.property`); the spec's `Album.Artist.name` multi-hop form is not implemented and no Chinook golden question needs it.

**Type consistency checked.** `ColumnRef.parse` accepts str/dict/ColumnRef and is used by `JoinPair` and `Property`. `LinkType.fans_out` is consumed by `resolve.fanning_edges` and `grain.analyse`. `MetricPlan.forced_by` is a link *name* and is used as such in `api.Rewrite`. `GrainPlan.additive` flows to `Result.additive`. `Grain.explain` returns a dict; `Grain.query` returns `Result` — the CLI and MCP adapters serialise both.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-08-17-grain-engine.md` (repo-local rather than the skill default, matching where `docs/architecture.html` already lives).

Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — tasks executed in this session with checkpoints for review.

Which approach?
