"""The conversation loop.

One user question in, one answer out, with as many tool calls in between as the
model needs to get a spec the engine accepts.

A hand-written loop rather than the SDK's tool runner, for one reason: a failed
query here is not an exception to surface but a REPAIR to feed back, and the
repair budget is a property of this application (a model that cannot fix a spec
in three attempts is not converging and should say so to the user rather than
burn tokens). That policy is the loop, so the loop is written out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..engine.api import Grain
from . import tools
from .prompt import system_blocks

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import Anthropic

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_REPAIRS = 3

_MISSING_SDK = (
    "The agent needs the Anthropic SDK, which is not installed.\n"
    "Install it with:  uv pip install -e '.[agent]'"
)


def _client(api_key: str | None = None) -> "Anthropic":
    """Built here, not at import, so the library, CLI and MCP paths keep working
    with the SDK uninstalled -- it is an optional extra."""
    try:
        import anthropic
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise RuntimeError(_MISSING_SDK) from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


@dataclass
class Turn:
    """What one question cost and what it did, for the caller to display."""

    answer: str
    specs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class AgentSession:
    """A conversation bound to one `Grain`.

    History is in memory for the session's lifetime; nothing is persisted. The
    API is stateless, so the full history is sent each turn -- which is why the
    system prompt carries a cache breakpoint.
    """

    def __init__(
        self,
        grain: Grain,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_repairs: int = MAX_REPAIRS,
        client: Any | None = None,
        strict: bool = True,
    ) -> None:
        self.grain = grain
        self.model = model
        self.max_repairs = max_repairs
        # Strict tool use constrains generation to the schema, at the cost of
        # the schema being expressible only in a subset of JSON Schema. Off, the
        # real bounds go across but malformed specs get likelier. Correctness is
        # unaffected either way -- Pydantic validates every call regardless.
        self.strict = strict
        # `client` is injectable so the loop can be tested without a network
        # call or an API key. Nothing else about the class changes.
        self.client = client if client is not None else _client(api_key)
        self.system = system_blocks(grain.describe())
        self.messages: list[dict[str, Any]] = []

    def _create(self) -> Any:
        return self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            # Adaptive thinking: choosing a grain-correct spec over a domain
            # with overlapping metric names is exactly what it helps with.
            # `budget_tokens` is rejected on this model family.
            thinking={"type": "adaptive"},
            tools=[tools.tool_definition(strict=self.strict)],
            messages=self.messages,
        )

    def ask(self, question: str) -> Turn:
        """One question, one answer. Raises only on API-level failures."""
        self.messages.append({"role": "user", "content": question})
        turn = Turn(answer="")
        repairs = 0

        while True:
            response = self._create()
            turn.input_tokens += response.usage.input_tokens
            turn.output_tokens += response.usage.output_tokens
            turn.cache_read_tokens += getattr(
                response.usage, "cache_read_input_tokens", 0
            ) or 0

            # The whole content list goes back, thinking blocks included: they
            # must be echoed unchanged when continuing on the same model.
            self.messages.append({"role": "assistant", "content": response.content})

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                turn.answer = "\n".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                return turn

            # Every tool_result for one assistant turn goes back in a SINGLE
            # user message. Splitting them trains the model out of parallel
            # calls, and the API rejects a turn with some results missing.
            results = []
            for call in calls:
                text, is_error = tools.run(self.grain, call.input)
                if is_error:
                    turn.errors.append(text)
                    repairs += 1
                else:
                    turn.specs.append(call.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": text,
                    "is_error": is_error,
                })

            if repairs > self.max_repairs:
                # Stop the loop by telling the model to stop, rather than by
                # cutting the conversation off: the person is owed an
                # explanation, and the model has the errors needed to give one.
                results.append({
                    "type": "text",
                    "text": (
                        f"You have failed {repairs} times on this question. Do "
                        f"not call the tool again. Explain to the user what you "
                        f"tried and why the domain cannot answer it."
                    ),
                })
                self.messages.append({"role": "user", "content": results})
                final = self._create()
                turn.input_tokens += final.usage.input_tokens
                turn.output_tokens += final.usage.output_tokens
                self.messages.append({"role": "assistant", "content": final.content})
                turn.answer = "\n".join(
                    b.text for b in final.content if b.type == "text"
                ).strip()
                return turn

            self.messages.append({"role": "user", "content": results})
