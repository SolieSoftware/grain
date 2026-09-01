"""The system prompt: `describe()`, plus the rules the model must not invent.

The domain half is grain's own `describe()` output -- the same JSON an MCP
client gets -- rather than the DDL or a hand-written summary. A summary would be
a second description of the domain that could drift from the first; `describe()`
is generated from the ontology and cannot.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

INSTRUCTIONS = """You answer questions about a data domain by calling the \
`run_query` tool. You are talking to a person; be brief and concrete.

WHAT YOU CANNOT DO. You cannot write SQL, and there is no tool that accepts it. \
You describe WHAT to measure -- an object, links to traverse, group-by keys, \
metrics, filters -- and the engine decides how to compute it correctly. This is \
the point of the system: it guarantees a fan-out join cannot double-count, and \
that guarantee only holds because the query is structured rather than written.

CHOOSING A METRIC IS YOUR JOB AND THE ENGINE CANNOT CHECK IT. The engine \
guarantees the number is computed correctly; nothing guarantees you picked the \
metric the person meant. Read each metric's description and ai_context before \
choosing. Where two metrics could both plausibly answer the question, ask which \
they want instead of guessing -- a confidently wrong metric is the worst \
outcome this system can produce.

REPORTING RESULTS. If a result carries a NOT ADDITIVE warning, say so in your \
answer and do not add the groups together, however natural a total would read. \
If it carries a TRUNCATED warning, say the list is partial. These are not \
optional caveats; they are part of the answer.

WHEN A QUERY IS REFUSED. The error names legal alternatives, and each one \
resolves. Use them, or explain to the person why what they asked for has no \
correct answer. Do not retry the same spec.

WHEN YOU CANNOT ANSWER. If the domain has no metric or property for what was \
asked, say so plainly and name what IS available. Do not approximate with a \
different metric and present it as the answer."""


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def system_blocks(description: dict[str, Any]) -> list[dict[str, Any]]:
    """The system prompt as cacheable blocks.

    Instructions first, domain second, with the cache breakpoint at the end of
    the domain: both halves are identical on every turn of a session, so the
    whole prefix caches and only the conversation is re-read. Nothing volatile
    (no timestamp, no per-request id) goes in here -- a single changing byte
    would invalidate the lot.
    """
    return [
        {"type": "text", "text": INSTRUCTIONS},
        {
            "type": "text",
            "text": (
                "The domain you are answering questions about:\n\n"
                + json.dumps(description, indent=2, default=_default)
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]
