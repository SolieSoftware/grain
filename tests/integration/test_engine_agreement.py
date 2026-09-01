"""Two independent engines over one ontology are a correctness oracle for each
other.

A disagreement is a defect in one of them, surfaced without anyone
hand-computing the expected figure. This is the main payoff of the two-engine
design — worth more than the performance story, which is unestablished: chinook
is too small to time, with the same query measuring 15.8 ms and 23.2 ms on
separate runs.

It only proves anything because the engines share nothing below the loaded
ontology. Each owns its own resolver, so a resolution bug shows up here rather
than being inherited by both and agreed upon.
"""
import pytest

from grain.domains.chinook import CHINOOK_DIR
from grain.engine.api import Grain
from grain.engine.errors import GrainError
from tests.corpus import CORPUS, DIVERGENT

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def engines(db_engine):
    return (
        Grain.load(CHINOOK_DIR, db_engine, engine_name="subquery"),
        Grain.load(CHINOOK_DIR, db_engine, engine_name="symmetric"),
    )


def _normalise(rows):
    """Compare as an order-insensitive multiset of stringified tuples.

    Stringified because the two engines can return the same number under
    different Python types — `Decimal('2107.71')` from a summed column and
    `Decimal('2107.7100')` from the encoded arithmetic are the same money and
    should not be reported as a disagreement. Scale is normalised away, value
    is not.
    """
    out = []
    for row in rows:
        out.append(tuple(
            format(v.normalize(), "f") if hasattr(v, "normalize") else v
            for v in row
        ))
    return sorted(out, key=repr)


@pytest.mark.parametrize("name,spec", CORPUS, ids=[c[0] for c in CORPUS])
def test_both_engines_return_the_same_rows(name, spec, engines):
    sub, sym = engines
    expected = sub.query(spec)
    got = sym.query(spec)
    assert _normalise(got.rows) == _normalise(expected.rows)


@pytest.mark.parametrize("name,spec", CORPUS, ids=[c[0] for c in CORPUS])
def test_both_engines_agree_on_additivity(name, spec, engines):
    """A number both engines agree on, reported additive by one and not the
    other, is still a disagreement — the caveat is part of the answer."""
    sub, sym = engines
    assert sym.query(spec).additive == sub.query(spec).additive


@pytest.mark.parametrize(
    "name,spec,why", DIVERGENT, ids=[c[0] for c in DIVERGENT]
)
def test_the_recorded_divergences_are_real_and_one_directional(name, spec, why, engines):
    """Every divergence must be the SAME direction: subquery refuses, symmetric
    answers. A divergence the other way would mean the new engine lost a
    capability, which nothing here intends."""
    sub, sym = engines
    with pytest.raises(GrainError):
        sub.query(spec)
    result = sym.query(spec)
    assert result.rows, f"{name}: symmetric should answer this ({why})"


def test_the_corpus_actually_exercises_both_strategies(engines):
    """A harness where every spec took the same path through both engines would
    pass while proving nothing."""
    _, sym = engines
    strategies = set()
    for _, spec in CORPUS:
        for r in sym.explain(spec)["rewrites"]:
            strategies.add(r["strategy"])
    assert "symmetric" in strategies


def test_the_corpus_exercises_the_subquery_rewrite_too(engines):
    sub, _ = engines
    strategies = set()
    for _, spec in CORPUS:
        for r in sub.explain(spec)["rewrites"]:
            strategies.add(r["strategy"])
    assert "aggregate_then_join" in strategies
