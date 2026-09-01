"""The agent's only channel into the engine, and what it does with failure.

These need no API key and make no network call: `tools.run` is the boundary
between a model's generated object and the engine, and it is pure given a Grain.
"""
import pytest

from grain.agent import tools
from grain.engine.spec import QuerySpec


def test_the_tool_schema_is_the_queryspec_schema_itself():
    """Not a hand-written copy. The contract the model is held to and the
    contract the engine enforces must be the same object, or they drift."""
    assert tools.tool_definition()["input_schema"] == QuerySpec.model_json_schema()


def test_the_schema_is_strict_and_closed():
    """Strict tool use requires `additionalProperties: false`. QuerySpec already
    sets extra="forbid", so this holds without the agent layer adding anything —
    if that ever changes, the API rejects the tool rather than silently
    accepting invented fields."""
    d = tool = tools.tool_definition()
    assert d["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False


def test_a_malformed_spec_is_returned_as_a_repair_not_raised():
    """A failed generation is something the model can fix, given the error."""
    text, is_error = tools.run(None, {"object": "Customer", "invented_field": 1})
    assert is_error
    assert "malformed" in text.lower()


def test_a_malformed_spec_is_never_coerced():
    """Dropping the unknown key and running the rest would answer a question
    nobody asked."""
    text, is_error = tools.run(None, {"nonsense": True})
    assert is_error


class _FakeResult:
    def __init__(self, **kw):
        self.rows = kw.get("rows", [("USA", 10)])
        self.columns = kw.get("columns", ["country", "n"])
        self.additive = kw.get("additive", True)
        self.non_additive_reason = kw.get("non_additive_reason", None)
        self.limit_reached = kw.get("limit_reached", False)
        self.rewrites = kw.get("rewrites", [])


class _FakeGrain:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def query(self, spec):
        if self._error:
            raise self._error
        return self._result


SPEC = {"object": "Customer", "group_by": ["country"], "metrics": ["customer_count"]}


def test_a_non_additive_result_carries_a_warning_in_the_data():
    """Attached by code, not left to the prompt. A model that summed a
    non-additive column into a total would undo the point of the engine, and an
    instruction is not a strong enough guarantee for that."""
    g = _FakeGrain(_FakeResult(additive=False, non_additive_reason="Groups overlap."))
    text, is_error = tools.run(g, SPEC)
    assert not is_error
    assert "NOT ADDITIVE" in text
    assert "Groups overlap." in text
    assert "do NOT add them together" in text


def test_a_truncated_result_says_so():
    g = _FakeGrain(_FakeResult(limit_reached=True))
    text, _ = tools.run(g, SPEC)
    assert "TRUNCATED" in text
    assert "not describe these as all of them" in text.lower()


def test_a_clean_result_carries_no_warnings():
    """A warning on every result is a warning on none."""
    text, is_error = tools.run(_FakeGrain(_FakeResult()), SPEC)
    assert not is_error
    assert "WARNING" not in text
    assert text.startswith("country | n")


def test_a_refusal_hands_back_the_legal_alternatives():
    """grain's errors each name alternatives that themselves resolve, which is
    what makes a refusal a usable instruction rather than a dead end."""
    from grain.engine.errors import UnknownName

    g = _FakeGrain(error=UnknownName("metric", "revenu", ["revenue", "units_sold"]))
    text, is_error = tools.run(g, SPEC)
    assert is_error
    assert "UnknownName" in text
    assert "revenue" in text


def test_an_empty_result_is_not_an_error():
    """No rows is an answer. Reporting it as a failure would send the model
    into a repair loop over a query that worked."""
    text, is_error = tools.run(_FakeGrain(_FakeResult(rows=[])), SPEC)
    assert not is_error
    assert "matched no rows" in text


def test_nulls_render_as_null_not_none():
    """`None` in a table would read as a Python artefact rather than a value."""
    g = _FakeGrain(_FakeResult(rows=[(None, 3)]))
    text, _ = tools.run(g, SPEC)
    assert "NULL | 3" in text


@pytest.mark.parametrize("phrase", ["cannot write SQL", "ask which they want",
                                    "NOT ADDITIVE", "name what IS available"])
def test_the_system_prompt_states_the_rules_that_matter(phrase):
    from grain.agent.prompt import INSTRUCTIONS

    assert phrase in INSTRUCTIONS


def test_the_domain_block_is_cacheable_and_last():
    """It is large and identical every turn; the breakpoint goes at the end of
    the stable prefix so only the conversation is re-read."""
    from grain.agent.prompt import system_blocks

    blocks = system_blocks({"objects": {}, "rules": {}})
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_the_domain_block_serialises_decimals():
    """describe() carries Decimals; json.dumps refuses them by default, and the
    failure would be at the first chat rather than at import."""
    from decimal import Decimal

    from grain.agent.prompt import system_blocks

    blocks = system_blocks({"x": Decimal("1.5")})
    assert "1.5" in blocks[-1]["text"]
