import pytest
from grain.engine.errors import (
    GrainError, UnknownName, NoPath, AmbiguousPath, FanOutRefused, GuardTripped,
)

def test_unknown_name_lists_suggestions_in_message():
    err = UnknownName("metric", "revenu", ["revenue", "invoice_total"])
    assert "revenu" in str(err)
    assert "revenue" in str(err)
    assert err.alternatives == ["revenue", "invoice_total"]

def test_no_path_names_what_does_connect():
    err = NoPath("Customer", "Track", ["Customer_Invoices", "Customer_SupportRep"])
    assert "Customer" in str(err) and "Track" in str(err)
    assert err.alternatives == ["Customer_Invoices", "Customer_SupportRep"]

def test_fan_out_refused_names_grain_edge_and_fix():
    err = FanOutRefused("invoice_total", "invoice", "Invoice_Lines", ["revenue"])
    msg = str(err)
    assert "invoice_total" in msg and "invoice" in msg and "Invoice_Lines" in msg
    assert err.alternatives == ["revenue"]

def test_every_error_is_a_grain_error():
    for err in (
        UnknownName("metric", "x", []),
        NoPath("A", "B", []),
        AmbiguousPath("A", "B", ["L1", "L2"]),
        FanOutRefused("m", "g", "e", []),
        GuardTripped("row_cap", 10_000),
    ):
        assert isinstance(err, GrainError)
        assert isinstance(err.alternatives, list)
