"""Create and seed `daily_inventory` in the chinook database.

OPT-IN AND SEPARATE FROM LOADING, deliberately. Until now a domain pack only
DESCRIBED a database it did not own; shipping this SQL makes the pack a producer
of schema. Running it is an operation on someone's database and must never be a
side effect of `Grain.load`.

Re-runnable: `inventory.sql` creates IF NOT EXISTS and truncates before
inserting, so a second run leaves the same eight rows rather than sixteen.

    GRAIN_DATABASE_URL=... uv run python tools/seed_inventory.py

The tests that need it skip when the table is absent, so not running this is a
supported state — it costs the stock anchors, not the suite.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from grain.domains.chinook_inventory import INVENTORY_SQL


def main() -> int:
    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        print("GRAIN_DATABASE_URL is not set")
        return 2
    sql = INVENTORY_SQL.read_text(encoding="utf-8")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(sql))
    with engine.connect() as conn:
        n = conn.execute(text("select count(*) from daily_inventory")).scalar()
    print(f"daily_inventory seeded: {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
