"""The one tool the model gets, and what happens when it calls it.

`QuerySpec.model_json_schema()` is used VERBATIM as the tool's input schema.
That is not a shortcut -- it means the contract the model is held to and the
contract the engine enforces are the same object, and cannot drift. `QuerySpec`
already sets `extra="forbid"`, so Pydantic emits `additionalProperties: false`
and the schema satisfies strict tool use as-is.
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

If the query is refused, the error names legal alternatives. Use them."""


def tool_definition() -> dict[str, Any]:
    """The tool as the API wants it.

    `strict: True` is a top-level field on the tool, not part of `tool_choice`.
    It guarantees the input validates against the schema exactly, which is what
    makes `QuerySpec.model_validate` below a formality in the normal case rather
    than the only thing standing between a bad generation and the engine.
    """
    return {
        "name": TOOL_NAME,
        "description": DESCRIPTION,
        "input_schema": QuerySpec.model_json_schema(),
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
