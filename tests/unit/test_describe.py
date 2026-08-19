from grain.engine.describe import NON_ADDITIVITY_RULE, describe


def test_lists_objects_links_and_metrics(chinook_lite):
    out = describe(chinook_lite)
    assert "Customer" in out["objects"]
    assert "Customer_Invoices" in out["links"]
    assert "revenue" in out["metrics"]


def test_metrics_report_their_grain(chinook_lite):
    assert describe(chinook_lite)["metrics"]["revenue"]["grain"] == "invoice_line"


def test_links_report_cardinality(chinook_lite):
    links = describe(chinook_lite)["links"]
    assert links["Customer_Invoices"]["cardinality"] == "one_to_many"
    assert links["Playlist_Tracks"]["cardinality"] == "many_to_many"


def test_states_the_non_additivity_rule_once(chinook_lite):
    out = describe(chinook_lite)
    assert "many_to_many" in out["rules"]["non_additivity"]
    assert out["rules"]["non_additivity"] == NON_ADDITIVITY_RULE


def test_does_not_enumerate_metric_dimension_pairs(chinook_lite):
    """S1: the rule scales; an enumeration would grow multiplicatively."""
    out = describe(chinook_lite)
    for metric in out["metrics"].values():
        assert "non_additive_dimensions" not in metric


def test_single_object_view_is_narrower(chinook_lite):
    out = describe(chinook_lite, "Customer")
    assert set(out["objects"]) == {"Customer"}
