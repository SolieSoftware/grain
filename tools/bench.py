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
