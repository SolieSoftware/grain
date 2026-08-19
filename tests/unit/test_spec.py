import pytest
from pydantic import ValidationError
from grain.engine.spec import Filter, Hop, QuerySpec

def test_minimal_spec_defaults():
    spec = QuerySpec(object="Customer")
    assert spec.filters == [] and spec.metrics == [] and spec.limit == 100

def test_limit_has_no_upper_bound():
    """This database is small and known; a hard ceiling here would add
    friction without buying real protection. GuardConfig.row_cap is the
    actual backstop, and it can be sized to the data instead of to an
    arbitrary round number."""
    assert QuerySpec(object="Customer", limit=10_001).limit == 10_001

def test_limit_none_means_no_limit_clause_is_legal():
    assert QuerySpec(object="Customer", limit=None).limit is None

def test_limit_must_be_positive():
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", limit=0)

def test_unknown_operator_is_rejected():
    with pytest.raises(ValidationError):
        Filter(property="country", op="regex", value="^U")

def test_is_null_needs_no_value():
    assert Filter(property="composer", op="is_null").value is None

def test_spec_rejects_unknown_fields():
    """A hallucinated field must fail loudly, not be silently ignored."""
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", sql="select 1")

def test_hop_carries_optional_depth():
    assert Hop(link="Employee_Manager", max_depth=3).max_depth == 3
    assert Hop(link="Customer_Invoices").max_depth is None

def test_in_requires_a_sequence_not_a_bare_string():
    """Without this, a bare string reaches SQLAlchemy and raises ArgumentError
    from inside the compiler — an untyped error the agent cannot act on."""
    with pytest.raises(ValidationError):
        Filter(property="country", op="in", value="Brazil")

def test_in_accepts_a_list():
    assert Filter(property="country", op="in", value=["Brazil", "France"]).value == [
        "Brazil", "France"
    ]

def test_is_null_rejects_a_stray_value():
    with pytest.raises(ValidationError):
        Filter(property="composer", op="is_null", value="ignored")
