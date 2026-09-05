"""An independent oracle for grain queries.

Computes the answer in pure Python from raw table rows. It shares no code with
either engine, and — crucially — no SQL with the hand-written comparison queries
in the test suite, which could otherwise encode the same misconception twice.

The semantics it implements are the SPECIFICATION, stated directly:

  For each group, the answer is the aggregate over the SET OF DISTINCT ROWS of
  the metric's grain table that are reachable from the root rows in that group,
  along the declared traversal, under the declared filters.

Everything else — fan-out, replication, rewrites, encodings — is implementation.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import create_engine, text

URL = os.environ["GRAIN_DATABASE_URL"]

# The chinook links, as (from_table, from_col, to_table, to_col) steps. A
# `through` link is two steps; a recursive link is handled separately.
LINKS = {
    "Customer_Invoices": [("customer", "customer_id", "invoice", "customer_id")],
    "Invoice_Lines": [("invoice", "invoice_id", "invoice_line", "invoice_id")],
    "Customer_SupportRep": [("customer", "support_rep_id", "employee", "employee_id")],
    "Track_InvoiceLines": [("track", "track_id", "invoice_line", "track_id")],
    "Track_Album": [("track", "album_id", "album", "album_id")],
    "Album_Tracks": [("album", "album_id", "track", "album_id")],
    "Artist_Albums": [("artist", "artist_id", "album", "artist_id")],
    "Playlist_Tracks": [
        ("playlist", "playlist_id", "playlist_track", "playlist_id"),
        ("playlist_track", "track_id", "track", "track_id"),
    ],
}

OBJECT_TABLE = {
    "Customer": "customer", "Invoice": "invoice", "InvoiceLine": "invoice_line",
    "Track": "track", "Playlist": "playlist", "Employee": "employee",
    "Album": "album", "Artist": "artist", "Genre": "genre",
    "MediaType": "media_type",
}

PK = {
    "customer": "customer_id", "invoice": "invoice_id",
    "invoice_line": "invoice_line_id", "track": "track_id",
    "playlist": "playlist_id", "employee": "employee_id",
    "album": "album_id", "artist": "artist_id",
}

# metric name -> (grain table, aggregate, python value function)
METRICS = {
    "revenue": ("invoice_line", "sum", lambda r: r["unit_price"] * r["quantity"]),
    "units_sold": ("invoice_line", "sum", lambda r: r["quantity"]),
    "invoice_total": ("invoice", "sum", lambda r: r["total"]),
    "customer_count": ("customer", "count_distinct", None),
    "track_count": ("track", "count_distinct", None),
    "employee_count": ("employee", "count_distinct", None),
    # Order statistics. p is carried alongside the aggregate because the oracle
    # has no ontology to read it from -- it restates the question in its own
    # vocabulary, deliberately, so a misreading of the ontology cannot reach it.
    "median_duration": ("track", "median", lambda r: r["milliseconds"]),
    "p90_duration": ("track", "percentile", lambda r: r["milliseconds"]),
}

PERCENTILE_P = {"median_duration": 0.5, "p90_duration": 0.9}


def load(conn, table):
    rows = conn.execute(text(f"select * from {table}")).mappings().all()
    return [dict(r) for r in rows]


class Db:
    def __init__(self, conn):
        self.t = {}
        for name in set(OBJECT_TABLE.values()) | {"playlist_track"}:
            self.t[name] = load(conn, name)

    def index(self, table, col):
        idx = defaultdict(list)
        for row in self.t[table]:
            idx[row[col]].append(row)
        return idx


def walk(db, root_table, path_links):
    """Every tuple of rows the traversal produces, as dicts keyed by table.

    This is the join, built by hand: start with root rows, and for each step
    multiply out the matches. Replication is not avoided — it is exactly what
    the engines have to survive, so the oracle must reproduce it faithfully and
    then dedupe at the end, which is where the specification says to dedupe.
    """
    tuples = [{root_table: r} for r in db.t[root_table]]
    for link in path_links:
        for (ft, fc, tt, tc) in LINKS[link]:
            idx = db.index(tt, tc)
            out = []
            for tup in tuples:
                if ft not in tup:
                    continue
                for match in idx.get(tup[ft][fc], []):
                    nxt = dict(tup)
                    nxt[tt] = match
                    out.append(nxt)
            tuples = out
    return tuples


def answer(db, obj, links, group_props, metric_name, filters=None):
    """The specification, executed."""
    root_table = OBJECT_TABLE[obj]
    grain, agg, value_of = METRICS[metric_name]
    tuples = walk(db, root_table, links)

    if filters:
        for (table, col, val) in filters:
            tuples = [t for t in tuples if table in t and t[table][col] == val]

    # group key -> set of distinct grain rows (by pk)
    groups: dict[tuple, dict] = defaultdict(dict)
    for tup in tuples:
        if grain not in tup:
            continue
        key = tuple(tup[table][col] for (table, col) in group_props)
        groups[key][tup[grain][PK[grain]]] = tup[grain]

    out = {}
    for key, rows in groups.items():
        if agg in ("median", "percentile"):
            # percentile_disc: the first value whose cumulative fraction reaches
            # p, over the DISTINCT grain rows. Computed from raw Python rows,
            # sharing no SQL with either engine.
            values = sorted(value_of(r) for r in rows.values())
            p = PERCENTILE_P[metric_name]
            out[key] = values[max(1, math.ceil(p * len(values))) - 1]
        elif agg == "count_distinct":
            out[key] = len(rows)
        else:
            total = sum((Decimal(str(value_of(r))) for r in rows.values()),
                        Decimal("0"))
            out[key] = total
    return out


if __name__ == "__main__":
    engine = create_engine(URL)
    with engine.connect() as conn:
        db = Db(conn)
    print("loaded:", {k: len(v) for k, v in db.t.items()})
    r = answer(db, "Playlist", ["Playlist_Tracks", "Track_InvoiceLines"],
               [("playlist", "playlist_id")], "revenue")
    print("revenue by playlist id, total of groups:", sum(r.values()))
    print("playlist 1:", r.get((1,)))
