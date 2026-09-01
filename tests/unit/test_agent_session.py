"""The conversation loop, driven by a scripted client.

No API key, no network. The loop's job is protocol discipline and the repair
budget, and both are testable given a client that returns what you tell it to.
"""
from types import SimpleNamespace

import pytest

from grain.agent.session import AgentSession


def _text(s):
    return SimpleNamespace(type="text", text=s)


def _tool_use(spec, id="tu_1"):
    return SimpleNamespace(type="tool_use", id=id, name="run_query", input=spec)


def _thinking():
    return SimpleNamespace(type="thinking", thinking="...")


class _ScriptedClient:
    """Returns each scripted response in turn and records what it was sent."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        # Snapshot the message list. The session appends to it in place, so
        # recording the reference would make every captured request show the
        # final state and silently pass assertions about earlier ones.
        self.requests.append({**kw, "messages": list(kw["messages"])})
        content = self._responses.pop(0)
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0),
        )


class _Grain:
    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda spec: SimpleNamespace(
            rows=[("USA", 10)], columns=["country", "n"], additive=True,
            non_additive_reason=None, limit_reached=False, rewrites=[]))
        self.calls = []

    def describe(self, object=None):
        return {"objects": {"Customer": {}}, "rules": {}}

    def query(self, spec):
        self.calls.append(spec)
        return self.behaviour(spec)


SPEC = {"object": "Customer", "group_by": ["country"], "metrics": ["customer_count"]}


def _session(grain, *responses, **kw):
    return AgentSession(grain, client=_ScriptedClient(*responses), **kw)


def test_a_plain_answer_needs_no_tool_call():
    s = _session(_Grain(), [_text("The domain covers customers and invoices.")])
    turn = s.ask("what is this domain about?")
    assert turn.answer == "The domain covers customers and invoices."
    assert turn.specs == []


def test_a_tool_call_runs_the_spec_and_feeds_the_rows_back():
    g = _Grain()
    s = _session(g, [_tool_use(SPEC)], [_text("There are 10 customers in the USA.")])
    turn = s.ask("how many customers per country?")
    assert g.calls[0].object == "Customer"
    assert turn.specs == [SPEC]
    assert turn.answer == "There are 10 customers in the USA."


def test_the_rows_reach_the_model_as_a_tool_result():
    g = _Grain()
    s = _session(g, [_tool_use(SPEC)], [_text("done")])
    s.ask("q")
    second = s.client.requests[1]["messages"]
    results = second[-1]["content"]
    assert results[0]["type"] == "tool_result"
    assert results[0]["tool_use_id"] == "tu_1"
    assert "country | n" in results[0]["content"]
    assert results[0]["is_error"] is False


def test_thinking_blocks_are_echoed_back_unchanged():
    """They must be replayed verbatim when continuing on the same model."""
    g = _Grain()
    s = _session(g, [_thinking(), _tool_use(SPEC)], [_text("done")])
    s.ask("q")
    assistant = s.client.requests[1]["messages"][-2]
    assert any(b.type == "thinking" for b in assistant["content"])


def test_a_refusal_is_fed_back_as_a_repair_and_the_retry_succeeds():
    """grain's errors name legal alternatives, so a second attempt is the
    expected outcome rather than a failure."""
    from grain.engine.errors import UnknownName

    calls = {"n": 0}

    def behaviour(spec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnknownName("metric", "revenu", ["revenue"])
        return SimpleNamespace(rows=[("USA", 1)], columns=["c", "n"], additive=True,
                               non_additive_reason=None, limit_reached=False,
                               rewrites=[])

    g = _Grain(behaviour)
    s = _session(g, [_tool_use(SPEC)], [_tool_use(SPEC, id="tu_2")], [_text("2328.60")])
    turn = s.ask("revenue?")
    assert len(turn.errors) == 1
    assert "revenue" in turn.errors[0]
    assert turn.answer == "2328.60"

    repair = s.client.requests[1]["messages"][-1]["content"][0]
    assert repair["is_error"] is True


def test_the_repair_budget_stops_the_loop_and_still_answers():
    """A model that cannot fix a spec in three tries is not converging. It is
    told to stop and explain, rather than having the conversation cut off —
    the person is owed a reason."""
    from grain.engine.errors import UnknownName

    def always_fails(spec):
        raise UnknownName("metric", "nope", ["revenue"])

    s = _session(
        _Grain(always_fails),
        [_tool_use(SPEC, id="a")], [_tool_use(SPEC, id="b")],
        [_tool_use(SPEC, id="c")], [_tool_use(SPEC, id="d")],
        [_text("I could not answer that: no such metric exists.")],
        max_repairs=3,
    )
    turn = s.ask("give me the nope metric")
    assert len(turn.errors) == 4
    assert "could not answer" in turn.answer
    instruction = s.client.requests[-1]["messages"][-1]["content"][-1]
    assert instruction["type"] == "text"
    assert "Do not call the tool again" in instruction["text"]


def test_parallel_tool_calls_return_in_one_user_message():
    """Splitting them across messages trains the model out of parallel calls,
    and the API rejects a turn with results missing."""
    g = _Grain()
    s = _session(g, [_tool_use(SPEC, id="x"), _tool_use(SPEC, id="y")], [_text("ok")])
    s.ask("two things")
    results = s.client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["x", "y"]


def test_the_request_carries_the_strict_tool_and_adaptive_thinking():
    s = _session(_Grain(), [_text("hi")])
    s.ask("hello")
    req = s.client.requests[0]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["tools"][0]["strict"] is True
    assert req["model"] == "claude-opus-5"


def test_history_accumulates_across_turns():
    """The API is stateless; the full history goes every time."""
    s = _session(_Grain(), [_text("one")], [_text("two")])
    s.ask("first")
    s.ask("second")
    assert s.client.requests[1]["messages"][0]["content"] == "first"
    # user, assistant, user — the second question's request carries both
    # halves of the first exchange.
    assert len(s.client.requests[1]["messages"]) == 3
    assert s.client.requests[1]["messages"][-1]["content"] == "second"


def test_token_usage_is_reported():
    s = _session(_Grain(), [_tool_use(SPEC)], [_text("ok")])
    turn = s.ask("q")
    assert turn.input_tokens == 20
    assert turn.output_tokens == 10


def test_a_missing_sdk_names_the_install_command():
    """The SDK is an optional extra, so its absence must be actionable rather
    than an ImportError traceback."""
    from grain.agent import session as mod

    assert "[agent]" in mod._MISSING_SDK


@pytest.mark.parametrize("path", ["grain.engine.api", "grain.engine.cli"])
def test_the_engine_does_not_import_the_agent(path):
    """`agent/` is an adapter. The standing rule is that nothing below it may
    import upward, so the library and CLI keep working with no SDK installed."""
    import importlib

    src = importlib.import_module(path).__file__
    assert "grain.agent" not in open(src).read()
