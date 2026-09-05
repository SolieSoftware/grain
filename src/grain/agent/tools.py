"""The one tool the model gets, and what happens when it calls it.

The tool's input schema is derived from `QuerySpec.model_json_schema()` -- one
generated object rather than a hand-written copy, so the shape the model is
asked for cannot drift from the shape the engine accepts.

It is DERIVED, not verbatim. Strict tool use accepts a subset of JSON Schema and
rejects the whole tool definition if anything falls outside it, so `_strict_safe`
strips the keywords it cannot carry. An earlier version of this module claimed
the schema was used verbatim; the first real API call proved otherwise, twice.

What that costs is nothing that matters. The stripped keywords are value
constraints (`minimum`, `pattern`, ...), so the ADVERTISED schema is looser than
the real one -- but `run()` below still validates every call through Pydantic,
which enforces the full constraint set. A model that sends `limit: 0` gets a
validation error handed back as a repair, exactly like any other bad spec. The
constraints that matter to the model are restated in `DESCRIPTION` instead,
where prose can carry what the schema cannot.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..engine.api import Grain
from ..engine.errors import GrainError
from ..engine.spec import QuerySpec

TOOL_NAME = "run_query"

DESCRIPTION = """Run a query against the domain and return the rows.

Every name you use must be declared in the ontology you were given: `object` and
`link` names, `group_by` and `filter` properties, and `metrics`. A name that is
not declared is an error, not a guess -- if you are unsure which metric answers
the question, ask the user rather than picking one.

A dotted group_by key like 'Customer_SupportRep.last_name' reads a property of
an object you traversed to, and requires that link to be in `traverse`. A dotted
filter does NOT require the traversal: it selects which root objects the query
is about.

`limit` must be 1 or more if you set it, and `max_depth` on a hop must be
between 1 and 50. These are enforced even though the schema does not state them.

If the query is refused, the error names legal alternatives. Use them."""


# Keywords strict tool use rejects outright. Stripping them changes what is
# ADVERTISED, never what is enforced -- Pydantic still applies every one of them
# to the input. Two were found the hard way, each as a 400 on a real call:
#   "Schema type is missing for schema: {'default': None, 'title': 'Value'}"
#   "For 'integer' type, properties maximum, minimum are not supported"
# The rest are listed pre-emptively because they are the same class of thing and
# discovering each one costs a failed request in front of a user.
UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "uniqueItems",
})


def _strict_safe(node: Any) -> Any:
    """The schema with everything strict tool use cannot express removed."""
    if isinstance(node, dict):
        return {
            k: _strict_safe(v)
            for k, v in node.items()
            if k not in UNSUPPORTED_KEYWORDS
        }
    if isinstance(node, list):
        return [_strict_safe(v) for v in node]
    return node


def tool_definition(strict: bool = True) -> dict[str, Any]:
    """The tool as the API wants it.

    `strict: True` is a top-level field on the tool, not part of `tool_choice`.
    It constrains the generation to the schema, which is why `run()` rarely sees
    a malformed spec -- but it is not why a malformed spec is safe. That is
    Pydantic's job, and it does it whether or not this flag is set.
    """
    schema = QuerySpec.model_json_schema()
    if not strict:
        # Outside strict mode the API accepts far more JSON Schema, so the
        # constraints go across intact and the model sees the real bounds. The
        # cost is that generation is no longer constrained to the schema, so
        # malformed specs get likelier — each one costs a repair round-trip.
        # Never a wrong ANSWER though: Pydantic is the boundary either way.
        return {"name": TOOL_NAME, "description": DESCRIPTION,
                "input_schema": schema}
    return {
        "name": TOOL_NAME,
        "description": DESCRIPTION,
        "input_schema": _strict_safe(schema),
        "strict": True,
    }


def _caveats(result: Any) -> list[str]:
    """Facts the caller must be told, attached by CODE rather than by prompt.

    A model that summed a non-additive column into a "total" would undo the
    entire point of the engine. Instructing it not to is worth doing, but an
    instruction is not a strong enough guarantee for that, so the warning
    travels with the data.
    """
    out: list[str] = []
    if not result.additive:
        out.append(
            f"WARNING - NOT ADDITIVE: {result.non_additive_reason} Report each "
            f"group's figure, and do NOT add them together or present a total."
        )
    if result.limit_reached:
        out.append(
            "WARNING - TRUNCATED: exactly the requested limit of rows came back, "
            "so there may be more that were never fetched. Do not describe these "
            "as all of them."
        )
    for r in result.rewrites:
        out.append(f"note: '{r.metric}' was computed as {r.strategy} ({r.reason}).")
    return out


def run(grain: Grain, raw_input: dict[str, Any]) -> tuple[str, bool]:
    """Validate, execute, and render one tool call.

    Returns `(text, is_error)`. Both failure modes are returned rather than
    raised, because a failed query is a REPAIR, not a crash: grain's errors each
    name legal alternatives that themselves resolve, which makes them usable
    instructions for a second attempt.
    """
    try:
        spec = QuerySpec.model_validate(raw_input)
    except ValidationError as exc:
        # Never coerced into something runnable. Strict tool use makes this
        # unlikely; when it happens the model needs to see exactly what it got
        # wrong.
        return (f"The query spec was malformed:\n{exc}", True)

    try:
        result = grain.query(spec)
    except GrainError as exc:
        alternatives = (
            f"\nLegal alternatives: {', '.join(exc.alternatives)}"
            if exc.alternatives
            else ""
        )
        return (f"{type(exc).__name__}: {exc}{alternatives}", True)

    if not result.rows:
        return ("The query ran and matched no rows.", False)

    lines = [" | ".join(result.columns)]
    lines += [" | ".join("NULL" if v is None else str(v) for v in row)
              for row in result.rows]
    body = "\n".join(lines)
    caveats = _caveats(result)
    if caveats:
        body = "\n".join(caveats) + "\n\n" + body
    return (body, False)
