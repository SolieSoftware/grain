import pytest
from pydantic import ValidationError
from grain.engine.spec import Filter, Hop, QuerySpec

def test_minimal_spec_defaults():
    spec = QuerySpec(object="Customer")
    assert spec.filters == [] and spec.metrics == [] and spec.limit == 100

def test_limit_is_capped():
    with pytest.raises(ValidationError):
        QuerySpec(object="Customer", limit=10_001)

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
