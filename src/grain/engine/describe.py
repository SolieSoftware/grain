"""How the agent learns the domain. This replaces dumping a schema: it is
smaller than the DDL and stated in the language the question is asked in.

The non-additivity rule is stated ONCE here, never enumerated per
`(metric x dimension)` pair (S1). Publishing that per pair would grow
multiplicatively with the ontology; instead this module states the general
rule -- a metric grouped by a dimension reached through a `many_to_many` link
is non-additive -- and the agent derives the rest, because it already sees
every link's cardinality below. Constant cost, total coverage, unchanged at
200 tables. Do not add a per-metric `non_additive_dimensions` field back in;
`test_does_not_enumerate_metric_dimension_pairs` guards against it.

`ai_context` (synonyms + instructions) is surfaced faithfully because it is
the mitigation for this design's weakest joint: two metrics (or two objects)
can sound identical to an agent while being grain-incompatible in a way the
symbolic layer only catches when the pick is *also* grain-wrong. A
grain-compatible but semantically wrong pick passes silently, so the prose
that disambiguates it has to travel with the ontology, not live only in a
human's head.

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


def _ai(ctx: AiContext | None) -> dict[str, Any]:
    if ctx is None:
        return {}
    fields = {"synonyms": ctx.synonyms, "instructions": ctx.instructions}
    return {k: v for k, v in fields.items() if v}


def describe(onto: Ontology, object_name: str | None = None) -> dict[str, Any]:
    """A description of the ontology, in the agent's own vocabulary.

    With `object_name`, the `objects` and `links` views narrow to that object
    and the links touching it; `metrics` and `rules` are unscoped -- a metric
    or rule is a fact about the whole domain, not about one object.
    """
    names = [object_name] if object_name else list(onto.objects)
    return {
        "domain": onto.name,
        "description": onto.description,
        "rules": {"grain": GRAIN_RULE, "non_additivity": NON_ADDITIVITY_RULE},
        "objects": {
            name: {
                "description": onto.objects[name].description,
                "properties": {
                    p: {"type": prop.type, "nullable": prop.nullable}
                    for p, prop in onto.objects[name].properties.items()
                },
                **(
                    {"ai_context": _ai(onto.objects[name].ai_context)}
                    if onto.objects[name].ai_context
                    else {}
                ),
            }
            for name in names
        },
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
