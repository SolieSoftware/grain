"""The two resolvers are a deliberate copy. This does not stop them diverging --
it makes divergence visible instead of silent.

It matters more than it looks: the symmetric compiler reuses the subquery
engine's join-tree helpers, which were written against the ORIGINAL resolver's
Edge and ResolvedProperty. That works by duck typing, so a copied resolver that
drifts in shape breaks the shared helpers at runtime rather than at import.
"""
import grain.engine.resolve as original
import grain.engine_symmetric.resolve as copy


def _public(mod):
    return {n for n in dir(mod) if not n.startswith("_")}


def test_the_two_resolvers_expose_the_same_public_names():
    missing = _public(original) - _public(copy)
    assert not missing, f"the symmetric resolver is missing: {sorted(missing)}"


def test_resolved_property_has_the_same_fields_in_both():
    assert (set(original.ResolvedProperty.__dataclass_fields__)
            == set(copy.ResolvedProperty.__dataclass_fields__))


def test_resolved_query_has_the_same_fields_in_both():
    assert (set(original.ResolvedQuery.__dataclass_fields__)
            == set(copy.ResolvedQuery.__dataclass_fields__))


def test_they_are_genuinely_different_classes():
    """If these ever became the same object the differential harness would stop
    proving anything about resolution."""
    assert original.ResolvedQuery is not copy.ResolvedQuery
