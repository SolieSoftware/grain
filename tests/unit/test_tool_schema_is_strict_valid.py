"""The tool schema must satisfy strict tool use, not merely be valid JSON Schema.

The agent hands `QuerySpec.model_json_schema()` to the API verbatim with
`strict: true`. That mode requires EVERY schema node to declare a type, and
Pydantic emits no type for a field annotated `Any`. So the first real API call
the agent ever made was rejected before it reached the model:

    tools.0.custom: Invalid schema: Schema type is missing for schema:
    {'default': None, 'title': 'Value'}

Nothing caught it because every test asserted the schema was *the QuerySpec
schema*, which it was, and no test asserted the schema was *acceptable*. The two
are different claims and only one of them was being made.
"""
import pytest

from grain.agent.tools import tool_definition
from grain.engine.spec import Filter, QuerySpec

TYPE_KEYS = ("type", "anyOf", "allOf", "oneOf", "$ref", "enum", "const")


def _untyped(node, path="root"):
    """Every node that declares no type, by path."""
    found = []
    if not isinstance(node, dict):
        return found
    describes_a_value = bool(node.get("title") or "default" in node) or node == {}
    if describes_a_value and not any(k in node for k in TYPE_KEYS):
        found.append(path)
    for key, sub in node.get("properties", {}).items():
        found += _untyped(sub, f"{path}.{key}")
    for i, sub in enumerate(node.get("anyOf", []) + node.get("oneOf", [])):
        found += _untyped(sub, f"{path}[{i}]")
    if isinstance(node.get("items"), dict):
        found += _untyped(node["items"], f"{path}.items")
    return found


def test_no_schema_node_is_missing_a_type():
    """The exact condition the API rejected on."""
    schema = tool_definition()["input_schema"]
    problems = list(_untyped(schema))
    for name, sub in schema.get("$defs", {}).items():
        problems += _untyped(sub, name)
    assert not problems, f"strict tool use will reject these nodes: {problems}"


def test_the_filter_value_node_specifically_declares_a_type():
    """The node that actually failed, named so a regression is unambiguous."""
    value = QuerySpec.model_json_schema()["$defs"]["Filter"]["properties"]["value"]
    assert any(k in value for k in TYPE_KEYS), value


def test_every_object_node_forbids_extra_properties():
    """The other strict-mode requirement. It happens to hold because QuerySpec
    sets extra="forbid", but nothing asserted it, which is how the type gap
    survived."""
    schema = QuerySpec.model_json_schema()
    for name, sub in [("root", schema), *schema.get("$defs", {}).items()]:
        if sub.get("type") == "object" or "properties" in sub:
            assert sub.get("additionalProperties") is False, name


@pytest.mark.parametrize("value", [
    "USA", 5, 5.5, True, None, ["a", "b"], [1, 2],
])
def test_typing_the_value_field_did_not_narrow_what_a_filter_accepts(value):
    """The union replaced `Any` to make the schema expressible, NOT to restrict
    the model. Every value `Any` took must still round-trip with its own type —
    an int silently becoming a float would change what a filter matches."""
    got = Filter(property="country", op="eq", value=value).value
    assert got == value
    assert type(got) is type(value)


def test_an_in_filter_still_takes_a_list():
    """`op: in` is the reason `value` can be a list at all."""
    f = Filter(property="country", op="in", value=["USA", "Canada"])
    assert f.value == ["USA", "Canada"]


def test_a_scalar_is_still_refused_for_in():
    """The pre-existing validator, unaffected by the retyping."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="requires a list"):
        Filter(property="country", op="in", value="USA")
