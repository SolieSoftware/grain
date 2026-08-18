"""Defence in depth. The typed spec already makes writes inexpressible; the
read-only role means a compiler bug still cannot mutate anything. Neither of
those facts makes this module optional — a compiler bug that turns a single
metric into an unbounded cross join, or a query that legitimately touches ten
million rows, is still an outage even when it cannot corrupt a single row."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import Connection, Engine, text


@dataclass(frozen=True)
class GuardConfig:
    statement_timeout_ms: int = 10_000
    row_cap: int = 10_000


@contextmanager
def guarded_connection(engine: Engine, config: GuardConfig) -> Iterator[Connection]:
    """A single statement executed under `SET LOCAL statement_timeout`.

    `SET LOCAL` only takes effect for the remainder of the CURRENT transaction
    -- outside one it is silently scoped to nothing and the timeout would
    never apply. `Connection.execute()` autobegins a transaction the first
    time it is called on a connection that isn't already in one (verified
    against the live database: `conn.in_transaction()` is False right after
    `engine.connect()`, and True immediately after this SET LOCAL executes),
    so this SET LOCAL statement is itself what opens the transaction it needs
    to bind to -- no explicit `conn.begin()` required. The transaction is then
    always closed via `rollback()`, whether the body raised or not, since this
    connection is read-only by construction and never has anything to commit.
    """
    with engine.connect() as conn:
        try:
            conn.execute(
                text(f"SET LOCAL statement_timeout = {int(config.statement_timeout_ms)}")
            )
            yield conn
        finally:
            conn.rollback()
