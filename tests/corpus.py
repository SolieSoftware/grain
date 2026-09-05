"""Specs both engines are run against, by name.

Shared by the differential harness and the benchmark. Every entry is a real
question over the chinook pack; between them they cover no traversal, a
non-fanning hop, a fanning hop, a many_to_many, a qualified group key, a
recursive traversal, a dotted filter, multiple grains in one query, ordering,
and the two aggregate families (summing and immune).
"""
from grain.engine.spec import Filter, Hop, OrderBy, QuerySpec

CORPUS: list[tuple[str, QuerySpec]] = [
    (
        "no-traversal-count",
        QuerySpec(object="Customer", group_by=["country"],
                  metrics=["customer_count"], limit=None),
    ),
    (
        "no-traversal-no-keys",
        QuerySpec(object="Invoice", metrics=["invoice_total"], limit=None),
    ),
    (
        "one-fanning-hop-sum",
        QuerySpec(object="Customer", traverse=[Hop(link="Customer_Invoices")],
                  group_by=["country"], metrics=["invoice_total"], limit=None),
    ),
    (
        "two-fanning-hops-sum-at-the-far-end",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                  group_by=["country"], metrics=["revenue"], limit=None),
    ),
    (
        "two-fanning-hops-root-grain-immune",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                  group_by=["country"], metrics=["customer_count"], limit=None),
    ),
    (
        "two-grains-in-one-query",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                  group_by=["country"], metrics=["revenue", "invoice_total"],
                  limit=None),
    ),
    (
        "many-to-many-with-a-unique-key",
        QuerySpec(object="Playlist",
                  traverse=[Hop(link="Playlist_Tracks"),
                            Hop(link="Track_InvoiceLines")],
                  group_by=["id"], metrics=["revenue"], limit=None),
    ),
    (
        "many-to-many-immune-metric",
        QuerySpec(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
                  group_by=["id"], metrics=["track_count"], limit=None),
    ),
    (
        "qualified-group-key",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_SupportRep")],
                  group_by=["Customer_SupportRep.last_name"],
                  metrics=["customer_count"], limit=None),
    ),
    (
        "non-fanning-hop",
        QuerySpec(object="Track", traverse=[Hop(link="Track_Album")],
                  group_by=["Track_Album.title"], metrics=["track_count"],
                  limit=None),
    ),
    (
        "ordered-and-limited",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                  group_by=["country"], metrics=["revenue"],
                  order_by=[OrderBy(key="revenue", desc=True)], limit=5),
    ),
    (
        "filtered-on-a-root-property",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices")],
                  group_by=["country"], metrics=["invoice_total"],
                  filters=[Filter(property="country", op="eq", value="USA")],
                  limit=None),
    ),
    (
        "units-sold-at-line-grain",
        QuerySpec(object="Customer",
                  traverse=[Hop(link="Customer_Invoices"), Hop(link="Invoice_Lines")],
                  group_by=["country"], metrics=["units_sold"], limit=None),
    ),
    (
        "median-over-a-many-to-many",
        QuerySpec(object="Playlist", traverse=[Hop(link="Playlist_Tracks")],
                  group_by=["id"], metrics=["median_duration"], limit=None),
    ),
    (
        "p90-over-a-one-to-many",
        QuerySpec(object="Album", traverse=[Hop(link="Album_Tracks")],
                  group_by=["title"], metrics=["p90_duration"], limit=None),
    ),
    (
        "recursive-one-hop",
        QuerySpec(object="Employee",
                  traverse=[Hop(link="Employee_Manager", max_depth=1)],
                  group_by=["Employee_Manager.last_name"],
                  metrics=["employee_count"], limit=None),
    ),
]

# Specs the two engines legitimately handle differently. Each MUST be justified
# here; an unexplained entry is a bug being suppressed rather than a difference
# being recorded.
DIVERGENT: list[tuple[str, QuerySpec, str]] = [
    (
        "non-unique-group-key-over-many-to-many",
        QuerySpec(object="Playlist",
                  traverse=[Hop(link="Playlist_Tracks"),
                            Hop(link="Track_InvoiceLines")],
                  group_by=["name"], metrics=["revenue"], limit=None),
        # The subquery engine refuses (NonAdditiveRefused, defect C2): merging
        # two playlists named 'Music' makes IT double-count everything both
        # hold. The symmetric engine answers, because the encoding dedupes by
        # invoice_line_id and each line is counted once per group regardless.
        "subquery refuses C2's shape; symmetric answers it correctly",
    ),
    (
        "key-beyond-the-grain-across-a-fan",
        QuerySpec(object="Employee",
                  traverse=[Hop(link="Employee_Manager")],
                  group_by=["Employee_Manager.last_name"],
                  metrics=["employee_count"], limit=None),
        # The subquery engine refuses (KeyBeyondGrain): its pre-aggregate would
        # have to walk across the fan to reach the key. The symmetric engine
        # builds no subquery, so the refusal has nothing to protect — this is a
        # stated deliverable.
        "subquery refuses KeyBeyondGrain; symmetric has no subquery to protect",
    ),
]
