"""grain's encoding vs Looker's, on the two failure modes Looker documents.

Looker's docs say symmetric aggregation "might cause decimal overflow errors
when attempting to perform symmetric aggregation with values exceeding 14 digits
(greater than 10,000,000,000,000)", and that the fix is to LOWER `precision` to
5 or fewer decimal places. Both are consequences of packing a FLOOR-scaled value
and a hash into a fixed-width NUMERIC(38,0).

grain uses arbitrary-precision `numeric` and the real integer key, so neither
limit should apply. This measures whether that is true or merely claimed.

No tables are created. Everything runs over a VALUES CTE, so the database is
untouched.
"""
from __future__ import annotations

import os
from decimal import Decimal, getcontext

from sqlalchemy import create_engine, text

getcontext().prec = 60
engine = create_engine(os.environ["GRAIN_DATABASE_URL"])

PKS = [1, 2, 3]
FAN = 3  # each grain row duplicated three times by a notional fanning join

GRAIN = """
(sum(distinct pk::numeric * 1e30 + coalesce(v, 0))
 - sum(distinct pk::numeric * 1e30))
"""

# Looker's shape: FLOOR-scale the value to `precision` decimal places, add a
# hashed key, and hold the lot in NUMERIC(38,0).
LOOKER = """
(
  cast((
      sum(distinct cast(floor(coalesce(v,0) * (10::numeric ^ {p})) as numeric(38,0))
                   + cast(('x' || substr(md5(pk::text), 1, 15))::bit(60)::bigint
                          as numeric(38,0)) * (10::numeric ^ {p}))
    - sum(distinct cast(('x' || substr(md5(pk::text), 1, 15))::bit(60)::bigint
                        as numeric(38,0)) * (10::numeric ^ {p}))
  ) as numeric)
  / (10::numeric ^ {p})
)
"""


def values_cte(vals):
    rows = []
    for pk, v in zip(PKS, vals):
        for _ in range(FAN):
            rows.append(f"({pk}, {v}::numeric)")
    return "with r(pk, v) as (values " + ", ".join(rows) + ")"


def run(sql, vals):
    with engine.connect() as conn:
        try:
            return conn.execute(
                text(f"{values_cte(vals)} select {sql} from r")).scalar()
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {str(exc).splitlines()[0][:60]}"


def report(title, vals, precision=6):
    truth = sum(Decimal(v) for v in vals)
    g, lk = run(GRAIN, vals), run(LOOKER.format(p=precision), vals)
    def ok(x):
        # Compared NUMERICALLY. A first pass compared strings and flagged
        # 60000000001.0000000000 as wrong against 60000000001.00 — the same
        # number at a different scale. Scale is presentation; value is not.
        if isinstance(x, str):
            return "<-- ERROR"
        return "OK" if Decimal(x) == truth else f"<-- WRONG (off by {Decimal(x) - truth})"
    print(f"\n{title}")
    print(f"  true         {truth}")
    print(f"  grain        {g}   {ok(g)}")
    print(f"  looker-like  {lk}   {ok(lk)}")


print("=" * 76)
print("MAGNITUDE - Looker documents overflow above 14 digits (>1e13)")
print("=" * 76)
for exp in (10, 13, 15, 18, 25, 28):
    report(f"magnitude 1e{exp}",
           [f"{10**exp}.55", f"{2 * 10**exp}.25", f"{3 * 10**exp}.20"])

print()
print("=" * 76)
print("PRECISION - Looker returns 6 decimals by default and floors beyond")
print("=" * 76)
report("8 decimal places (beyond Looker's 6)",
       ["1.12345678", "2.87654321", "3.00000001"])
report("12 decimal places",
       ["1.123456789012", "2.000000000001", "3.999999999999"])
report("Looker at precision=5, as its docs advise for large values",
       ["1.12345678", "2.87654321", "3.00000001"], precision=5)
