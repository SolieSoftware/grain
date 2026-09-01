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

from decimal import Decimal
from typing import Any

from sqlalchemy import Column, MetaData, Numeric, cast, distinct, func, literal_column
from sqlalchemy.sql.elements import ColumnElement

from ..engine.errors import MetricNotSymmetric, NoIntegerKeyForGrain
from ..engine.ontology import Metric

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
    if metric.agg in NEEDS_ENCODING or metric.agg == "count":
        grain_key(metric, metadata)


def symmetric_expr(metric: Metric, metadata: MetaData) -> ColumnElement[Any]:
    """The metric as a single-pass, fan-out-correct aggregate."""
    require_eligible(metric, metadata)

    if metric.agg in ("min", "max", "count_distinct"):
        # Immune to replication -- the plain aggregate is already the right
        # number, and encoding it would add cost for nothing.
        return literal_column(metric.sql_expr)

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
        return encoded - keys_only
    if metric.agg == "avg":
        # `avg` ignores NULL values, so the divisor must count only the rows that
        # contributed to the numerator. A COALESCE-to-zero numerator over an
        # unfiltered count would drag the mean towards zero.
        divisor = func.count(distinct(key)).filter(value.isnot(None))
        return (encoded - keys_only) / func.nullif(divisor, 0)

    raise MetricNotSymmetric(
        metric.name, f"aggregate '{metric.agg}' has no symmetric form"
    )
