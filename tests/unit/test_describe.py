from grain.engine.describe import (
    GRAIN_MATCHING_RULE,
    NON_ADDITIVITY_RULE,
    describe,
)


def test_lists_objects_links_and_metrics(chinook_lite):
    out = describe(chinook_lite)
    assert "Customer" in out["objects"]
    assert "Customer_Invoices" in out["links"]
    assert "revenue" in out["metrics"]


def test_metrics_report_their_grain(chinook_lite):
    assert describe(chinook_lite)["metrics"]["revenue"]["grain"] == "invoice_line"


def test_metrics_report_their_grain_object(chinook_lite):
    """The bridge from a metric's (table-named) grain to the (object-named)
    keys of `objects` must be explicit, not case-conversion the agent has to
    guess -- that only happens to work for Chinook."""
    metrics = describe(chinook_lite)["metrics"]
    assert metrics["revenue"]["grain_object"] == "InvoiceLine"
    assert metrics["invoice_total"]["grain_object"] == "Invoice"


def test_objects_report_their_primary_table(chinook_lite):
    out = describe(chinook_lite)
    assert out["objects"]["InvoiceLine"]["primary"] == "invoice_line"
    assert out["objects"]["Invoice"]["primary"] == "invoice"


def test_links_report_cardinality(chinook_lite):
    links = describe(chinook_lite)["links"]
    assert links["Customer_Invoices"]["cardinality"] == "one_to_many"
    assert links["Playlist_Tracks"]["cardinality"] == "many_to_many"


def test_states_the_non_additivity_rule_once(chinook_lite):
    out = describe(chinook_lite)
    assert "many_to_many" in out["rules"]["non_additivity"]
    assert out["rules"]["non_additivity"] == NON_ADDITIVITY_RULE


def test_states_the_grain_matching_rule(chinook_lite):
    out = describe(chinook_lite)
    assert out["rules"]["grain_matching"] == GRAIN_MATCHING_RULE
    assert "grain" in out["rules"]["grain_matching"].lower()


def test_does_not_enumerate_metric_dimension_pairs(chinook_lite):
    """S1: the rule scales; an enumeration would grow multiplicatively.

    Asserted structurally, by pinning the permitted key set, so a
    reintroduction under any other name (e.g. `non_additive_dims`, or nested
    inside `ai_context`) fails loudly instead of passing silently.
    """
    out = describe(chinook_lite)
    allowed = {"grain", "grain_object", "type", "description", "ai_context"}
    for metric in out["metrics"].values():
        assert set(metric) <= allowed


def test_properties_report_a_description_key(chinook_lite):
    """Objects, links and metrics all surface their `description`; a
    property must not be the one construct that silently drops it."""
    prop = describe(chinook_lite)["objects"]["Customer"]["properties"]["country"]
    assert "description" in prop


def test_single_object_view_is_narrower(chinook_lite):
    out = describe(chinook_lite, "Customer")
    full = describe(chinook_lite)
    assert "Customer" in out["objects"]
    assert set(out["objects"]) < set(full["objects"])
    # Every link's endpoints must be present in `objects`, or the agent
    # cannot build a dotted filter through it (e.g. "Customer_Invoices.total").
    for link in out["links"].values():
        assert link["from"] in out["objects"]
        assert link["to"] in out["objects"]
    # And the properties of a one-hop neighbour are actually there.
    assert "total" in out["objects"]["Invoice"]["properties"]
