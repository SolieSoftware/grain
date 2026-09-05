"""The encoded aggregate: a grain-correct SUM in one pass, no subquery.

    SUM(DISTINCT k*K + COALESCE(v, 0)) - SUM(DISTINCT k*K)

equals the sum of `v` over the DISTINCT rows of the metric's grain table present
in the join, however many times the join replicated them.

Verified against chinook before this was written. Through
`invoice_line -> track -> playlist_track`, the naive sum reports 5738.28 where
the true total is 2328.60; this form returns 2328.60 exactly. Grouped by
`customer.country` through the same fanning join: 24 countries, 24 exact
matches, 0 mismatches.

Four conditions, each proved or enforced:

(a) `k` identifies ONE row of the grain table, so equal `k` implies equal `v`
    and replicated rows produce an identical encoded term that collapses under
    DISTINCT. Enforced by requiring a single-column integer PRIMARY KEY, read
    from the database rather than declared.

(b) Distinct `k` give distinct encoded terms, which holds when
    |COALESCE(v,0)| < K/2. Proof: if k1*K + v1 == k2*K + v2 with k1 != k2 then
    |k1-k2|*K == |v2-v1| >= K, while |v2-v1| <= |v1| + |v2| < K -- a
    contradiction. The loader checks the bound against the data.

(c) The arithmetic is EXACT. Postgres `numeric` is arbitrary-precision decimal,
    so there is no overflow ceiling and no truncation.

(d) COALESCE is mandatory, not cosmetic. Without it a NULL `v` makes the first
    term NULL for that row and SUM drops it, while `k*K` still appears in the
    subtracted term. Measured on a two-row case: a true 10.00 came back as
    -1999999999999999999999999999990.00.

WHERE THIS DEPARTS FROM LOOKER. Looker hashes the key (MD5 -> bigint) and
FLOOR-scales the value into a fixed-width NUMERIC(38,0). That buys portability
across MySQL, Redshift and BigQuery, and it costs two silent failure modes:
hash COLLISIONS drop a row's value with no signal, and FLOOR truncation loses
digits below the chosen scale. grain is Postgres-only, so this uses the real
integer key and an unscaled value: no hashing, therefore no collisions; no fixed
width, therefore no truncation. A project whose previous branch was spent
replacing plausible wrong numbers with enforced invariants should not adopt a
technique whose failure mode is a silently dropped row.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    cast,
    distinct,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.sql.elements import ColumnElement

from ..engine.errors import MetricNotSymmetric, NoIntegerKeyForGrain
from ..engine.ontology import Metric
from .scale import column_scale

K = literal_column("1e30")
"""The key offset.

Postgres types this literal as `numeric`, not `double precision` -- verified,
and pinned by `test_the_offset_is_numeric_not_float`. A float here would
silently reintroduce the inexactness condition (c) exists to avoid.
"""

BOUND = Decimal("5e29")
"""Half of K. Condition (b) holds while |v| < BOUND.

Unreachable for monetary and count data -- but "unreachable" was reasoning about
money, not a measurement, so `loader._check_symmetric_headroom` checks it
against the data. That is a CHECK, not a guarantee: rows written after load can
cross it and condition (b) then fails silently. The design records this as its
weakest point and keeps a self-enforcing SQL guard in reserve.
"""

EXACT_TYPES: frozenset[str] = frozenset({"integer", "decimal"})
"""Value types that encode exactly. Anything else is refused, never rounded."""

ORDER_STATISTICS: frozenset[str] = frozenset({"median", "percentile"})
"""Aggregates needing the ARRAY encoding rather than the sum encoding.

A sum decomposes -- `SUM(DISTINCT k*K + v) - SUM(DISTINCT k*K)` recovers the
total because addition is associative and the key contribution cancels exactly.
An order statistic does not: it needs the multiset of distinct values IN ORDER,
so it is built by sorting an encoded array and indexing it.

The BI literature says a median has no distinct-sum rewrite. True, and narrower
than it sounds -- it rules out THAT rewrite, not every encoding.
"""

KEY_OFFSET = literal_column("1e19")
"""The key offset for the ARRAY encoding. Deliberately NOT `K`.

Both constants separate a value from a key, and their guarantees are not the
same. `K = 1e30` pairs with `BOUND` -- a limit on a VALUE, which nothing in the
schema constrains, so it can only be checked at load and later writes can
violate it silently.

This one bounds a KEY. The requirement is `KEY_OFFSET > max(pk)`, and a bigint
cannot exceed 9.22e18, so the key's own type guarantees it. A proof rather than
a measurement: nothing to check, nothing to drift.

Sharing one symbol would hide that difference, and the difference is the whole
reason this encoding is sounder than the one beside it.
"""

BARE_COLUMN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$")
"""A value that is nothing but one `table.column`. Order statistics accept only
this shape: a scale can be read off a column, not off `a * b`."""

NEEDS_ENCODING: frozenset[str] = frozenset({"sum", "avg"})
"""Aggregates whose value must be carried through the encoding.

`count` needs only the key; `min`/`max`/`count_distinct` are immune to
replication and need nothing at all.
"""


def grain_key(metric: Metric, metadata: MetaData) -> Column[Any]:
    """The grain table's single-column integer primary key -- condition (a)."""
    table = metadata.tables[metric.grain]
    cols = list(table.primary_key.columns)
    if len(cols) != 1:
        raise NoIntegerKeyForGrain(metric.name, metric.grain)
    (col,) = cols
    try:
        if col.type.python_type is not int:
            raise NoIntegerKeyForGrain(metric.name, metric.grain)
    except NotImplementedError as exc:
        # A dialect type that cannot name a Python type cannot be PROVED an
        # integer, and an unproved key is exactly what condition (a) forbids.
        raise NoIntegerKeyForGrain(metric.name, metric.grain) from exc
    return col


def require_eligible(metric: Metric, metadata: MetaData) -> None:
    """Raise unless this metric can be served symmetrically.

    Called by the planner BEFORE a connection is acquired, per the engine-wide
    rule that every failure except `GuardTripped` is raised first.
    """
    if not metric.is_structured:
        raise MetricNotSymmetric(
            metric.name,
            "it is declared as an opaque 'expr', so the aggregate function and "
            "the per-row value cannot be separated",
        )
    if metric.agg in NEEDS_ENCODING and metric.type not in EXACT_TYPES:
        raise MetricNotSymmetric(
            metric.name,
            f"its type '{metric.type}' does not encode exactly, and an inexact "
            f"encoding loses digits silently",
        )
    if metric.agg in ORDER_STATISTICS:
        # Building the expression IS the eligibility check -- it raises for a
        # non-bare value or a column with no exact scale. Done here so the
        # refusal lands before a connection is acquired.
        order_statistic_expr(metric, metadata)
        return
    if metric.agg in NEEDS_ENCODING or metric.agg == "count":
        grain_key(metric, metadata)



def _as_declared(metric: Metric, expr: ColumnElement[Any]) -> ColumnElement[Any]:
    """Return the metric in the type it declares.

    The encoding is numeric arithmetic whatever the value's own type, so a
    metric declared `integer` would otherwise come back as `Decimal` from this
    engine and `int` from the subquery engine. The differential harness caught
    exactly that on `units_sold`: same number, different type, and a caller
    serialising the two would see a real difference.

    Safe because the encoding is EXACT -- the sum of integers it recovers is an
    integer already, so this cast cannot round anything. It is applied only to
    `sum`; see `avg`.
    """
    if metric.type == "integer":
        return cast(expr, Integer)
    return expr


def order_statistic_expr(metric: Metric, metadata: MetaData) -> ColumnElement[Any]:
    """A median or percentile that survives a fanning join, in one pass.

        encoded = (v * 10^s) * K + pk
        result  = floor(sorted_distinct[greatest(1, ceil(p*n))] / K) / 10^s

    ORDERING: `10^s` and `K` are positive constants, so the encoded number sorts
    in the same order as `v`, with the key in the low digits breaking ties
    without disturbing that order.

    DEDUPLICATION: DISTINCT applies to the encoded pair, which is equivalent to
    deduplicating on the key, because `v` is functionally determined by `pk`.
    That is WHY the key is in the encoding at all -- `array_agg(DISTINCT v)`
    would collapse two different rows that happen to share a value, and those
    are two data points.

`KEY_OFFSET` NEEDS NO RUNTIME CHECK, unlike the sum encoding's `|v| < 5e29`.
    The requirement is `KEY_OFFSET > max(pk)`, and a bigint cannot exceed
    9.22e18, so the key's own TYPE guarantees it. A proof rather than a
    measurement, and the reason this encoding cannot drift as data grows.

    `10^s` IS EXACT because `s` is the column's own reflected scale, so
    multiplying clears the fraction with nothing to round. This is precisely
    where Looker loses digits: it FLOOR-scales to a guessed precision.
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
    # Built ONCE and passed to both the DISTINCT and the ORDER BY: Postgres
    # requires them to be the same expression, and two structurally-equal but
    # distinct objects render as two expressions.
    encoded = cast(column, Numeric) * factor * KEY_OFFSET + cast(key, Numeric)
    p = 0.5 if metric.agg == "median" else metric.percentile
    index = func.greatest(
        1, func.ceil(literal_column(str(p)) * func.count(distinct(key)))
    )
    picked = func.array_agg(aggregate_order_by(distinct(encoded), encoded))[index]
    # Cast back to the declared type, exactly as the sum encoding does. The
    # encoding is numeric arithmetic whatever the value's own type, so a metric
    # declared `integer` would otherwise return Decimal here and int from the
    # subquery engine. The differential harness caught precisely that, for the
    # second time -- it is the same defect `_as_declared` was written for.
    return _as_declared(metric, func.floor(picked / KEY_OFFSET) / factor)


def symmetric_expr(metric: Metric, metadata: MetaData) -> ColumnElement[Any]:
    """The metric as a single-pass, fan-out-correct aggregate."""
    require_eligible(metric, metadata)

    if metric.agg in ("min", "max", "count_distinct"):
        # Immune to replication -- the plain aggregate is already the right
        # number, and encoding it would add cost for nothing.
        return literal_column(metric.sql_expr)

    if metric.agg in ORDER_STATISTICS:
        return order_statistic_expr(metric, metadata)

    key = grain_key(metric, metadata)
    if metric.agg == "count":
        # count(*) over a fanned join counts replicas; count(distinct key)
        # counts rows. Needs no encoding.
        return func.count(distinct(key))

    offset = cast(key, Numeric) * K
    value = literal_column(str(metric.value))
    encoded = func.sum(distinct(offset + func.coalesce(value, 0)))
    keys_only = func.sum(distinct(offset))

    if metric.agg == "sum":
        return _as_declared(metric, encoded - keys_only)
    if metric.agg == "avg":
        # `avg` ignores NULL values, so the divisor must count only the rows that
        # contributed to the numerator. A COALESCE-to-zero numerator over an
        # unfiltered count would drag the mean towards zero.
        divisor = func.count(distinct(key)).filter(value.isnot(None))
        # NOT cast back to the declared type: a mean of integers is not an
        # integer, and rounding it here would be a silent answer change.
        return (encoded - keys_only) / func.nullif(divisor, 0)

    raise MetricNotSymmetric(
        metric.name, f"aggregate '{metric.agg}' has no symmetric form"
    )
