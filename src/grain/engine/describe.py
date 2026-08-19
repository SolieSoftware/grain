"""How the agent learns the domain. This replaces dumping a schema: it is
smaller than the DDL and stated in the language the question is asked in.

The non-additivity rule is stated ONCE here, never enumerated per
`(metric x dimension)` pair (S1). Publishing that per pair would grow
multiplicatively with the ontology; instead this module states the general
rule -- a metric grouped by a dimension reached through a `many_to_many` link
is non-additive -- and the agent derives the rest, because it already sees
every link's cardinality below. Constant cost, total coverage, unchanged at
200 tables. Do not add a per-metric `non_additive_dimensions` field back in,
at the top level of a metric dict or nested inside its `ai_context`;
`test_does_not_enumerate_metric_dimension_pairs` pins the permitted keys of
both, so any new field -- under any name, at either level -- has to be a
deliberate change to that test.

The same S1 pattern applies to grain-matching: a metric's grain must match
the entity being *measured*, never the entity it is merely grouped by --
"average invoice value" measures invoices (invoice grain, not invoice_line),
and "average tracks per playlist" measures tracks and groups by playlist
(track grain, with playlist as the group-by). That distinction cannot be
closed by prose on two metrics; it generalises to every metric an agent has
never seen. `GRAIN_MATCHING_RULE` states it once, as a rule, rather than as
an enumerated warning per metric pair.

`ai_context` (synonyms + instructions) is surfaced faithfully because it is
the mitigation for this design's weakest joint: two metrics (or two objects)
can sound identical to an agent while being grain-incompatible in a way the
symbolic layer only catches when the pick is *also* grain-wrong. A
grain-compatible but semantically wrong pick passes silently, so the prose
that disambiguates it has to travel with the ontology, not live only in a
human's head.

A metric's `grain` names a TABLE (e.g. `invoice_line`); `objects` is keyed by
OBJECT NAME (e.g. `InvoiceLine`). Nothing about that mapping is guaranteed to
be a case-conversion of the other in a domain pack other than Chinook, so
`grain_object` states it explicitly -- the object type name whose `primary`
table equals the metric's grain, or `null` if no object declares it. Each
object also reports its own `primary`, so the mapping is inspectable from
either direction rather than implied.

`Result.additive` (in `execute.py`) is the authoritative, per-query verdict.
This module carries the rule an agent can apply in advance; it never repeats
that verdict here, to avoid two sources of truth for the same fact.
"""
from __future__ import annotations

from typing import Any

from .ontology import AiContext, Ontology

NON_ADDITIVITY_RULE = (
    "A metric grouped by a dimension reached through a many_to_many link is "
    "non-additive -- the groups overlap, and the column will not sum to the total. "
    "Each group is still correct on its own."
)

GRAIN_RULE = (
    "Every metric is aggregated at its declared grain. If the query path fans out "
    "relative to that grain, the engine rewrites the query rather than double-counting, "
    "and reports the rewrite. If it cannot, it refuses and names the alternative."
)

GRAIN_MATCHING_RULE = (
    "A metric's grain must match the entity being measured -- the thing one row "
    "of the metric represents -- never the entity it is grouped by. 'Average "
    "invoice value' measures invoices, so the metric must be at invoice grain, not "
    "invoice_line grain, even though both describe the same money. 'Average tracks "
    "per playlist' measures tracks and groups by playlist: the metric is at track "
    "grain, and playlist enters only as a group-by, reached through a link. When a "
    "question names more than one entity, the metric's grain is the one being "
    "measured, not the one being grouped by."
)


def _ai(ctx: AiContext | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    fields = {"synonyms": ctx.synonyms, "instructions": ctx.instructions}
    return {k: v for k, v in fields.items() if v}


def _grain_object(onto: Ontology, grain_table: str) -> str | None:
    """The object type name whose `primary` table is `grain_table`, or `None`
    if no declared object reaches it -- the explicit bridge from a metric's
    (table-named) `grain` to the (object-named) keys of `objects`."""
    obj = onto.object_for_table(grain_table)
    return obj.name if obj else None


def _touching_objects(onto: Ontology, object_name: str) -> set[str]:
    """`object_name` plus every object one hop away via a link touching it.
    Not transitive: an agent using the narrowed view needs to see the
    properties of a linked object to build a dotted filter like
    'Customer_Invoices.total', but a further hop is out of scope for a view
    that asked about one object."""
    names = {object_name}
    for link in onto.links.values():
        if object_name in (link.from_, link.to):
            names.add(link.from_)
            names.add(link.to)
    return names


def _describe_object(onto: Ontology, name: str) -> dict[str, Any]:
    obj = onto.objects[name]
    out: dict[str, Any] = {
        "primary": obj.primary,
        "description": obj.description,
        "properties": {
            p: {
                "type": prop.type,
                "nullable": prop.nullable,
                "description": prop.description,
            }
            for p, prop in obj.properties.items()
        },
    }
    if obj.ai_context:
        out["ai_context"] = _ai(obj.ai_context)
    return out


def describe(onto: Ontology, object_name: str | None = None) -> dict[str, Any]:
    """A description of the ontology, in the agent's own vocabulary.

    With `object_name`, `objects` narrows to that object plus every object one
    hop away via a link touching it (so every link's endpoints are always
    present in `objects`, and the agent can build a dotted filter through
    them), and `links` narrows to the links touching it. `metrics` and
    `rules` are unscoped -- a metric or rule is a fact about the whole
    domain, not about one object.
    """
    names = _touching_objects(onto, object_name) if object_name else set(onto.objects)
    return {
        "domain": onto.name,
        "description": onto.description,
        "rules": {
            "grain": GRAIN_RULE,
            "non_additivity": NON_ADDITIVITY_RULE,
            "grain_matching": GRAIN_MATCHING_RULE,
        },
        "objects": {name: _describe_object(onto, name) for name in names},
        "links": {
            name: {
                "from": link.from_,
                "to": link.to,
                "kind": link.kind,
                "cardinality": link.cardinality,
                "description": link.description,
            }
            for name, link in onto.links.items()
            if object_name is None or object_name in (link.from_, link.to)
        },
        "metrics": {
            name: {
                "grain": metric.grain,
                "grain_object": _grain_object(onto, metric.grain),
                "type": metric.type,
                "description": metric.description,
                **(
                    {"ai_context": _ai(metric.ai_context)}
                    if metric.ai_context
                    else {}
                ),
            }
            for name, metric in onto.metrics.items()
        },
    }
