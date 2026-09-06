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
from grain.domains.chinook_inventory import INVENTORY_DIR
from grain.engine.api import Grain
from grain.engine.errors import GrainError
from grain.engine.spec import Hop, QuerySpec
from oracle import METRICS, OBJECT_TABLE, answer, Db

# Which pack each root object is declared in. `Inventory` is not in chinook: it
# describes a table `tools/seed_inventory.py` creates, and a pack that named a
# table the database may not have would refuse to load at all.
DOMAIN = {"Inventory": INVENTORY_DIR}

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
    # A stock, which only the subquery engine can serve. Enumerated anyway: the
    # oracle is the only independent judge of it, so the one comparison this
    # row makes is the one that matters.
    ("Inventory", []),
    # The same stock across a fan. Unpinned it is refused by both engines;
    # pinned by the fanning edge's unique key the subquery engine answers, and
    # the oracle is what says the answer is right.
    ("Inventory", ["Inventory_Track", "Track_InvoiceLines"]),
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
    "Inventory": [("as_of", ("daily_inventory", "as_of_date")),
                  ("track", ("daily_inventory", "track_id")),
                  ("Track_InvoiceLines.id", ("invoice_line", "invoice_line_id"))],
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
    loaded: dict[tuple, Grain] = {}

    def grain_for(root, engine_name):
        """One Grain per (pack, engine). Packs beyond chinook exist because a
        pack may only name tables the database actually has."""
        key = (DOMAIN.get(root, CHINOOK_DIR), engine_name)
        if key not in loaded:
            loaded[key] = Grain.load(key[0], db_engine, engine_name=key[1])
        return loaded[key]

    tally = Counter()
    problems = []
    total = 0
    skipped = []

    for root, links in PATHS:
        try:
            sub = grain_for(root, "subquery")
            sym = grain_for(root, "symmetric")
        except GrainError as exc:
            # An optional pack whose table has not been seeded.
            skipped.append(f"{root}: {exc}")
            continue
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
                # The oracle knows metrics the packs do not declare —
                # `opening_level` is built on the fly by the stock anchors.
                # Enumerating one would report a pair of UnknownName refusals
                # as though the engines had disagreed with something.
                if metric not in sub.ontology.metrics:
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
    for line in skipped:
        print(f"  skipped  {line}")
    for k, v in tally.most_common():
        print(f"  {v:>4}  {k}")
    if problems:
        print(f"\n{len(problems)} divergences:")
        for kind, label, detail in problems[:40]:
            print(f"  [{kind}] {label}  -> {detail}")


if __name__ == "__main__":
    main()
