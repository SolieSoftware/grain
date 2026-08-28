"""Run a compiled statement through the guard and hand back rows the caller
can trust are complete. Every value here is read straight off the cursor by
column label — `Select.columns` and `plan.metric_plans` order the SELECT list
as keys, then inline metrics, then rewritten metrics, which is NOT necessarily
the order the caller's `metrics=[...]` named them in. `cursor.keys()` reports
the labels the database actually returned them under, in that same real order,
so `columns[i]` always names `row[i]` correctly no matter which order the
caller asked for things."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Select

from .errors import GuardTripped
from .guard import GuardConfig, guarded_connection


@dataclass(frozen=True)
class Rewrite:
    """One metric the compiler moved off the naive inline path.

    `forced_by` is the link NAME that forced the rewrite (e.g. `"Customer_
    Invoices"`) -- machine-addressable, and asserted verbatim by
    `test_rewrite_is_surfaced_when_the_engine_changes_the_query`. `reason` is
    the human-readable sentence built from it. The two are never conflated:
    callers that branch on the rewrite (a UI badge, a second query) branch on
    `forced_by`; callers that only display something branch on `reason`.
    """

    metric: str
    strategy: str
    forced_by: str
    reason: str


@dataclass
class Result:
    """What one `Grain.query()` call hands back.

    `additive` and `non_additive_reason` are read straight off the plan-level
    `GrainPlan` properties -- never recomputed here -- so there is exactly one
    place that decides what "additive" means for a query as a whole; a caller
    that wants the per-metric detail behind that verdict reads it off the
    `MetricPlan`s the facade already built, not off `Result`.

    A metric that never matched a group during the rewrite's rejoin comes
    back as `None`, not `0`. Task 13 removed the `coalesce(x, 0)` that used to
    paper over exactly this case, because a miss there is never "a group with
    no facts" -- the subquery's key set is provably a superset of the outer's,
    so a miss can only be a broken comparison. Re-introducing a zero here,
    one layer up, would quietly undo that fix and hide the same failure mode
    one hop further from where it happens. `None` is what actually happened;
    it is passed through rather than replaced.

    `limit_reached` says the row count is exactly the requested `limit`, so
    there may be more rows the caller never saw. It is not cosmetic: with
    `limit` defaulting to 100 and `order_by` previously ignored, a caller asking
    for "the top 10" got 10 arbitrary rows with nothing to distinguish
    10-of-10 from 10-of-24 (defect I1). It reports a POSSIBLE truncation --
    a result of exactly `limit` rows out of exactly `limit` is flagged too,
    because the two cases are indistinguishable without fetching more.
    """

    rows: list[tuple[Any, ...]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    compiled_sql: str = ""
    rewrites: list[Rewrite] = field(default_factory=list)
    additive: bool = True
    non_additive_reason: str | None = None
    limit_reached: bool = False
    ontology_elements_used: list[str] = field(default_factory=list)
    engine: str = "subquery"
    """Which engine produced this.

    A result that cannot say which engine answered is not comparable, and
    comparing two engines is the entire reason the seam exists. Defaulted so
    that a caller constructing a `Result` directly in a test does not have to
    care, but the facade always sets it explicitly.
    """


def execute(
    engine: Engine, stmt: Select[Any], config: GuardConfig
) -> tuple[list[tuple[Any, ...]], list[str]]:
    """Fetch one row past the cap so a full result is distinguishable from one
    truncated at exactly the cap -- fetching only `row_cap` rows can never tell
    those two cases apart."""
    with guarded_connection(engine, config) as conn:
        cursor = conn.execute(stmt)
        columns = list(cursor.keys())
        rows = cursor.fetchmany(config.row_cap + 1)
        if len(rows) > config.row_cap:
            raise GuardTripped("row_cap", config.row_cap)
        return [tuple(r) for r in rows], columns
