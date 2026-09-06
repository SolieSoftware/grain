"""Does the symmetric encoding scale worse than a pre-aggregating subquery?

chinook cannot answer this: 2240 rows, and run-to-run variance (15.8ms vs
23.2ms on the same query) exceeds the effect. So the two SQL shapes are
benchmarked directly on synthetic data of increasing size.

No tables are created — `generate_series` builds the data inside the query, so
the database is untouched. That also means both shapes pay the same generation
cost, so the difference between them is the thing being measured.

Shape: `facts` is the metric's grain (one row per id, with a value), `dim` is a
grouping dimension, and `fan` multiplies each fact row FANOUT times — the
join that makes a naive SUM wrong.
"""
from __future__ import annotations

import os
import statistics
import time

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["GRAIN_DATABASE_URL"])

DATA = """
with facts as (
  select g as id, mod(g, 97) + 1 as dim_id, (mod(g, 1000) + 0.55)::numeric as v
  from generate_series(1, {n}) g
),
fan as (
  select f.id, f.dim_id, f.v, s as copy
  from facts f, generate_series(1, {fanout}) s
)
"""

# One pass, encoded.
SYMMETRIC = DATA + """
select dim_id,
       sum(distinct id::numeric * 1e30 + coalesce(v, 0))
     - sum(distinct id::numeric * 1e30) as total
from fan group by dim_id
"""

# Pre-aggregate at the grain, then join back — what the subquery engine emits.
PREAGG = DATA + """
, agg as (select dim_id, sum(v) as total from facts group by dim_id)
select f.dim_id, a.total
from fan f left join agg a on a.dim_id = f.dim_id
group by f.dim_id, a.total
"""

NAIVE = DATA + "select dim_id, sum(v) as total from fan group by dim_id"


def time_ms(sql, reps=5):
    with engine.connect() as conn:
        conn.execute(text(sql)).all()  # warm
        runs = []
        for _ in range(reps):
            t0 = time.perf_counter()
            conn.execute(text(sql)).all()
            runs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(runs), min(runs), max(runs)


def correctness(n, fanout):
    """Confirm the shapes actually disagree — otherwise the fan isn't fanning
    and the benchmark measures nothing interesting."""
    with engine.connect() as conn:
        s = conn.execute(text(SYMMETRIC.format(n=n, fanout=fanout))).all()
        p = conn.execute(text(PREAGG.format(n=n, fanout=fanout))).all()
        nv = conn.execute(text(NAIVE.format(n=n, fanout=fanout))).all()
    agree = sorted(map(tuple, s)) == sorted(map(tuple, p))
    naive_differs = sorted(map(tuple, nv)) != sorted(map(tuple, p))
    return agree, naive_differs


FANOUT = 5
print(f"fanout {FANOUT}x, median of 5 runs (min-max), 97 groups\n")
print(f"{'grain rows':>11} {'joined rows':>12} {'symmetric':>20} "
      f"{'pre-aggregate':>20} {'ratio':>7}  correct?")
print("-" * 88)

for n in (1_000, 10_000, 100_000, 500_000, 1_000_000):
    sym = time_ms(SYMMETRIC.format(n=n, fanout=FANOUT))
    pre = time_ms(PREAGG.format(n=n, fanout=FANOUT))
    agree, naive_differs = correctness(n, FANOUT)
    flag = "yes" if agree and naive_differs else ("AGREE-BUT-NO-FAN" if agree else "MISMATCH")
    print(f"{n:>11,} {n * FANOUT:>12,} "
          f"{sym[0]:>10.1f}ms ({sym[1]:.0f}-{sym[2]:.0f}) "
          f"{pre[0]:>10.1f}ms ({pre[1]:.0f}-{pre[2]:.0f}) "
          f"{sym[0] / pre[0]:>6.2f}x  {flag}")


# ---------------------------------------------------------------------------
# The window shape: what a `stock` costs over a plain `sum`.
# ---------------------------------------------------------------------------
#
# Its own data, deliberately: adding a time column to `facts` above would have
# changed the generation cost the fan-out numbers were measured against, and a
# benchmark whose baseline moved is not a benchmark.
#
# A stock cannot be summed across time, so the engine windows to one instant
# first: `t = max(t) over (partition by <group keys>)`, then aggregates what
# survives. That mandatory Sort + WindowAgg is what this measures. The cost is
# inherent to computing the boundary correctly, not a defect — but this repo
# has a scar exactly here. `SUM(DISTINCT ...)` was claimed to be "usually
# faster" and measured 4x SLOWER the first time anyone timed it, so the number
# is tracked rather than asserted.
#
# READ THE BIG ROWS ONLY. At 1k rows the ratio was measured at 0.91x, 1.18x and
# 1.28x on three consecutive runs of this file — run-to-run variance exceeds the
# effect, so any figure quoted from that row is noise with a decimal point on
# it. From 10k up it is stable (~1.6x) and at 1M it reproduces at 1.86x. This
# warning is here because a small-input number from this file was quoted in the
# README once and did not reproduce.
#
# NOT the cost with an index. `daily_inventory`'s primary key is
# (track_id, as_of_date) precisely so the planner can feed the WindowAgg
# pre-sorted; `generate_series` has no index at all, so what follows is the
# unindexed worst case.
DATA_T = """
with facts as (
  select g as id,
         mod(g, {groups}) + 1 as dim_id,
         date '2026-01-01' + mod(g, {dates}) as t,
         (mod(g, 1000) + 0.55)::numeric as v
  from generate_series(1, {n}) g
)
"""

# The baseline a flow pays: one HashAggregate, no ordering required.
FLAT = DATA_T + "select dim_id, sum(v) as total from facts group by dim_id"

# What a stock pays: the boundary instant per group, then the sum over it.
# This is the shape `_window_to_boundary` emits, down to the bookkeeping names.
WINDOWED = DATA_T + """
select dim_id, sum(__grain_value) as total
from (
  select dim_id, v as __grain_value, t as __grain_t,
         max(t) over (partition by dim_id) as __grain_pick
  from facts
) w
where w.__grain_t = w.__grain_pick
group by dim_id
"""

# An independent formulation of the same question, used only to confirm the
# windowed shape answers it. Written as a semi-join on the boundary, which
# cannot fan: one (dim_id, boundary) row per group.
WINDOW_ORACLE = DATA_T + """
, bound as (select dim_id, max(t) as b from facts group by dim_id)
select f.dim_id, sum(f.v) as total
from facts f join bound m on m.dim_id = f.dim_id and m.b = f.t
group by f.dim_id
"""

GROUPS, DATES = 97, 30
print(f"\n\nwindow (stock) vs flat aggregate, {GROUPS} groups over {DATES} dates,"
      f" median of 5 runs (min-max)\n")
print(f"{'rows':>11} {'flat sum':>20} {'windowed':>20} {'ratio':>7}  agrees?")
print("-" * 72)

for n in (1_000, 10_000, 100_000, 500_000, 1_000_000):
    fmt = {"n": n, "groups": GROUPS, "dates": DATES}
    flat = time_ms(FLAT.format(**fmt))
    win = time_ms(WINDOWED.format(**fmt))
    with engine.connect() as conn:
        w = conn.execute(text(WINDOWED.format(**fmt))).all()
        o = conn.execute(text(WINDOW_ORACLE.format(**fmt))).all()
        f = conn.execute(text(FLAT.format(**fmt))).all()
    agrees = sorted(map(tuple, w)) == sorted(map(tuple, o))
    # If the window were a no-op the ratio would be measuring nothing.
    windows = sorted(map(tuple, w)) != sorted(map(tuple, f))
    flag = "yes" if agrees and windows else ("NO-OP-WINDOW" if agrees else "MISMATCH")
    print(f"{n:>11,} "
          f"{flat[0]:>10.1f}ms ({flat[1]:.0f}-{flat[2]:.0f}) "
          f"{win[0]:>10.1f}ms ({win[1]:.0f}-{win[2]:.0f}) "
          f"{win[0] / flat[0]:>6.2f}x  {flag}")
