"""Enumerate every valid (root, path, group key, metric) combination and check
all three answers: subquery engine, symmetric engine, independent oracle.

Hand-picked cases test what the author thought to test. Enumeration tests what
the author did not.
"""
from __future__ import annotations

import os
from collections import Counter
from decimal import Decimal

from sqlalchemy import create_engine

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.errors import GrainError
from grain.engine.spec import Hop, QuerySpec
from oracle import METRICS, OBJECT_TABLE, answer, Db

# root object -> list of (link path, table each hop lands on)
PATHS = [
    ("Customer", []),
    ("Customer", ["Customer_Invoices"]),
    ("Customer", ["Customer_Invoices", "Invoice_Lines"]),
    ("Customer", ["Customer_SupportRep"]),
    ("Playlist", []),
    ("Playlist", ["Playlist_Tracks"]),
    ("Playlist", ["Playlist_Tracks", "Track_InvoiceLines"]),
    ("Playlist", ["Playlist_Tracks", "Track_Album"]),
    ("Artist", ["Artist_Albums"]),
    ("Artist", ["Artist_Albums", "Album_Tracks"]),
    ("Artist", ["Artist_Albums", "Album_Tracks", "Track_InvoiceLines"]),
    ("Album", ["Album_Tracks"]),
    ("Album", ["Album_Tracks", "Track_InvoiceLines"]),
    ("Track", ["Track_InvoiceLines"]),
    ("Track", ["Track_Album"]),
    ("Invoice", ["Invoice_Lines"]),
]

# root object -> (spec group_by key, (oracle table, column))
# Only properties the chinook pack actually declares. A first pass invented
# `id` on Customer and Invoice, and the 12 resulting UnknownName refusals looked
# like an engine finding until I checked — they were invalid specs.
GROUP_KEYS = {
    "Customer": [("country", ("customer", "country")),
                 ("email", ("customer", "email")),
                 ("city", ("customer", "city")),
                 ("last_name", ("customer", "last_name"))],
    "Playlist": [("id", ("playlist", "playlist_id")),
                 ("name", ("playlist", "name"))],
    "Artist": [("name", ("artist", "name"))],
    "Album": [("title", ("album", "title"))],
    "Track": [("name", ("track", "name"))],
    "Invoice": [("billing_country", ("invoice", "billing_country"))],
}


def norm(v):
    if v is None:
        return None
    if isinstance(v, (int, Decimal, float)):
        return Decimal(str(v)).normalize()
    return v


def main():
    db_engine = create_engine(os.environ["GRAIN_DATABASE_URL"])
    with db_engine.connect() as conn:
        db = Db(conn)
    sub = Grain.load(CHINOOK_DIR, db_engine, engine_name="subquery")
    sym = Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric")

    tally = Counter()
    problems = []
    total = 0

    for root, links in PATHS:
        reachable = {OBJECT_TABLE[root]}
        for link in links:
            from oracle import LINKS
            for (_, _, tt, _) in LINKS[link]:
                reachable.add(tt)
        for gkey, gprop in GROUP_KEYS.get(root, []):
            if gprop[0] not in reachable:
                continue
            for metric, (grain_tbl, _, _) in METRICS.items():
                if grain_tbl not in reachable:
                    continue
                total += 1
                spec = QuerySpec(object=root,
                                 traverse=[Hop(link=x) for x in links],
                                 group_by=[gkey], metrics=[metric], limit=None)
                truth = {(norm(k[0]),): norm(v)
                         for k, v in answer(db, obj=root, links=links,
                                            group_props=[gprop],
                                            metric_name=metric).items()}

                def run(g):
                    try:
                        r = g.query(spec)
                        return "ok", {(norm(row[0]),): norm(row[1]) for row in r.rows}
                    except GrainError as e:
                        return "refused", type(e).__name__

                s_state, s_val = run(sub)
                y_state, y_val = run(sym)
                s_ok = s_state == "ok" and s_val == truth
                y_ok = y_state == "ok" and y_val == truth

                label = f"{root} {'->'.join(links) or '(no hops)'} by {gkey} :: {metric}"
                if s_ok and y_ok:
                    tally["both correct"] += 1
                elif y_ok and s_state == "refused":
                    tally["symmetric answers, subquery refuses"] += 1
                    problems.append(("SYM-ONLY", label, s_val))
                elif s_ok and y_state == "refused":
                    tally["subquery answers, symmetric refuses"] += 1
                    problems.append(("SUB-ONLY", label, y_val))
                elif s_state == "refused" and y_state == "refused":
                    tally["both refuse"] += 1
                    problems.append(("BOTH-REFUSE", label, f"{s_val} / {y_val}"))
                else:
                    wrong = []
                    if s_state == "ok" and not s_ok:
                        wrong.append("subquery")
                    if y_state == "ok" and not y_ok:
                        wrong.append("symmetric")
                    tally[f"WRONG: {'+'.join(wrong)}"] += 1
                    problems.append(("WRONG", label, wrong))

    print(f"{total} enumerated (root, path, group key, metric) combinations\n")
    for k, v in tally.most_common():
        print(f"  {v:>4}  {k}")
    if problems:
        print(f"\n{len(problems)} divergences:")
        for kind, label, detail in problems[:40]:
            print(f"  [{kind}] {label}  -> {detail}")


if __name__ == "__main__":
    main()
