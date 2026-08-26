from grain.engine.describe import (
    GRAIN_MATCHING_RULE,
    NON_ADDITIVITY_RULE,
    describe,
)
from grain.engine.ontology import AiContext, Metric, Ontology


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

    Asserted structurally, by pinning the permitted key set at BOTH levels --
    the metric dict and, if present, its nested `ai_context` -- so a
    reintroduction under any other name, at either level (e.g.
    `non_additive_dims` alongside `grain`, or nested one level deeper inside
    `ai_context`), fails loudly instead of passing silently.
    """
    out = describe(chinook_lite)
    allowed = {"grain", "grain_object", "type", "description", "ai_context"}
    allowed_ai_context = {"synonyms", "instructions"}
    for metric in out["metrics"].values():
        assert set(metric) <= allowed
        if "ai_context" in metric:
            assert set(metric["ai_context"]) <= allowed_ai_context


def test_ai_context_shape_is_pinned():
    """The chinook_lite fixture has no metric with ai_context set, so this
    constructs one directly: pinning only the metric's top-level keys would
    let a look-alike field sail through one level deeper, inside ai_context
    itself -- this proves that path is closed too."""
    onto = Ontology(
        name="t",
        metrics={
            "m": Metric(
                name="m",
                grain="invoice",
                expr="sum(invoice.total)",
                type="decimal",
                ai_context=AiContext(synonyms=["x"], instructions="y"),
            )
        },
    )
    out = describe(onto)
    assert set(out["metrics"]["m"]["ai_context"]) <= {"synonyms", "instructions"}


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


def test_a_unique_property_is_visible_to_the_agent(chinook_lite):
    """`NON_ADDITIVITY_RULE` instructs the agent to group by a property marked
    unique, so the description has to say which ones are. A rule naming a fact
    the reader cannot see is not actionable — and this flag is the difference
    between a query being answered and refused."""
    from grain.engine.describe import describe

    props = describe(chinook_lite, "Playlist")["objects"]["Playlist"]["properties"]
    assert props["id"]["unique"] is True
    assert props["name"]["unique"] is False


def test_the_non_additivity_rule_states_its_own_condition(chinook_lite):
    """It used to publish "Each group is still correct on its own" flatly, which
    was false for chinook's duplicate playlist names (defect C2) — a wrong domain
    fact, asserted to the agent by the very thing meant to teach it the domain."""
    from grain.engine.describe import describe

    rule = describe(chinook_lite)["rules"]["non_additivity"]
    assert "unique" in rule
    assert "refused" in rule


def test_the_qualified_key_and_recursive_rules_are_published(chinook_lite):
    """Both are semantics an agent cannot guess from the spec's shape, and both
    were unstated while the code enforced them. `max_depth` in particular is the
    difference between "the manager" and "the management chain", which is the
    difference between an additive answer and a non-additive one."""
    from grain.engine.describe import describe

    rules = describe(chinook_lite)["rules"]
    assert "traverse" in rules["traversed_keys"]
    assert "max_depth" in rules["recursive_links"]
    assert "many_to_many" in rules["recursive_links"]
    assert "unique" in rules["recursive_links"]
