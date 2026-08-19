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
    # QuerySpec.limit no longer has an upper bound (a caller may legally pass
    # `limit=None` for "no LIMIT at all", or any limit above this number), so
    # this is now the ONLY backstop against an unbounded result -- reachable
    # by construction, not just by test contrivance.
    #
    # 10_000 was checked against this database's actual data, not picked as a
    # round number: the largest table is playlist_track at 8,715 rows, and it
    # is also the largest UNGROUPED join reachable by any one legally-chained
    # traversal here -- every fanning link downstream of it (Track_
    # InvoiceLines, avg well under 1 invoice_line per track) only shrinks the
    # count further (e.g. Playlist -> Playlist_Tracks -> Track_InvoiceLines
    # measures 5,572, not more). A GROUP BY can only collapse rows, never add
    # them. So no accepted spec against this data can legitimately produce
    # more than ~8,715 rows, and 10_000 clears that with headroom while still
    # being small enough to catch a real bug (a missing join condition on
    # tables these sizes would produce a cross product in the hundreds of
    # thousands, not low tens of thousands). Revisit this number if the
    # ontology grows a traversal that legitimately needs more rows than the
    # data currently supports.
    row_cap: int = 10_000


@contextmanager
def guarded_connection(engine: Engine, config: GuardConfig) -> Iterator[Connection]:
    """A single statement executed under `SET LOCAL statement_timeout`.

    `SET LOCAL` only takes effect for the remainder of the CURRENT transaction
    -- outside one it is silently scoped to nothing and the timeout never
    applies. Two independent ways that can happen, both verified live against
    the database rather than assumed:

    1. No transaction has been opened yet. `Connection.execute()` autobegins
       one the first time it runs on a connection not already in one, so
       *today*, with this function's statements in this order, the SET LOCAL
       call is itself what opens the transaction it binds to. But that is an
       accident of this function's statement order, not a guarantee -- a
       future edit that adds any statement (a ping, a `search_path` call)
       before the SET LOCAL would still autobegin correctly, but nothing
       stops a *later* edit from wrapping this in code that already committed
       or rolled back that autobegun transaction first. `conn.begin()` is
       called explicitly, up front, so the transaction's existence no longer
       depends on which statement happens to run first.
    2. The connection is in AUTOCOMMIT isolation. Confirmed live: under
       AUTOCOMMIT, `conn.in_transaction()` still reports `True` after
       `SET LOCAL`, but `SHOW statement_timeout` immediately after reports it
       unset -- AUTOCOMMIT gives every statement its own server-side
       transaction, so `SET LOCAL`'s scope ends before the very next
       statement runs, silently. This can't happen from anything the caller
       does to `conn` (this function owns the connection's whole lifecycle
       before yielding it), but it CAN happen if `engine` itself was ever
       configured with an AUTOCOMMIT default. Pinning the isolation level
       back to `READ COMMITTED` here removes that dependency on the engine's
       configuration too -- also verified live: the same AUTOCOMMIT
       connection, forced back to `READ COMMITTED` before `begin()`, reports
       the timeout correctly.

    The transaction is always closed via `rollback()`, whether the body
    raised or not, since this connection is read-only by construction and
    never has anything to commit.
    """
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="READ COMMITTED")
        conn.begin()
        try:
            conn.execute(
                text(f"SET LOCAL statement_timeout = {int(config.statement_timeout_ms)}")
            )
            yield conn
        finally:
            conn.rollback()
