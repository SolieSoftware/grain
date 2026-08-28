"""Engines are selected by name and produce one engine-agnostic value.

The facade must never touch an engine's internals: each engine owns its own
resolver, so `ResolvedQuery` is a different class per engine and nothing outside
an engine may depend on its shape.
"""
import pytest

from grain.engine import adapter  # noqa: F401  -- registers "subquery"
from grain.plan import EnginePlan, engine_names, get_engine, register_engine


def test_the_existing_engine_is_registered_as_subquery():
    assert "subquery" in engine_names()
    assert get_engine("subquery") is not None


def test_an_unknown_engine_names_the_legal_ones():
    with pytest.raises(KeyError, match="subquery"):
        get_engine("nope")


def test_engine_plan_carries_only_engine_agnostic_values():
    """If a field here ever becomes an engine-owned type, the seam has leaked."""
    assert set(EnginePlan.__dataclass_fields__) == {
        "stmt", "rewrites", "additive", "non_additive_reason",
        "ontology_elements_used", "limit",
    }


def test_registering_a_duplicate_name_is_refused():
    """Two engines sharing a name would make `Result.engine` a lie, and
    comparing engines is the reason any of this exists."""
    class Fake:
        def plan(self, spec, ontology, metadata):
            raise NotImplementedError

    register_engine("fake-dup", Fake())
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_engine("fake-dup", Fake())
    finally:
        from grain.plan import _ENGINES
        _ENGINES.pop("fake-dup", None)


def test_the_subquery_engine_produces_a_plan(chinook_lite, lite_metadata):
    from grain.engine.spec import QuerySpec

    ep = get_engine("subquery").plan(
        QuerySpec(object="Customer", group_by=["country"],
                  metrics=["customer_count"], limit=5),
        chinook_lite, lite_metadata,
    )
    assert ep.additive is True
    assert ep.rewrites == []
    assert ep.limit == 5
    assert "Customer" in ep.ontology_elements_used


def test_a_grain_built_with_an_unknown_engine_is_refused_at_construction():
    """Fail at the door, not on the first query."""
    from sqlalchemy import MetaData

    from grain.engine.api import Grain
    from grain.engine.ontology import Ontology

    with pytest.raises(KeyError, match="subquery"):
        Grain(Ontology(name="t"), MetaData(), None, engine_name="nope")
