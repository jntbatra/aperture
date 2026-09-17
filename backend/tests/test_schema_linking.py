from aperture.schema.graph import SchemaGraph
from aperture.schema.linker import SchemaLinker, tokenize


def test_tokenize_splits_camel_case_and_drops_stopwords():
    tokens = tokenize("How many kitchenProfiles order_items")
    # camelCase and snake_case both reduce to the vocabulary of the question
    assert {"kitchen", "profile", "item", "order"} <= tokens
    # "how" carries no signal, and "created" appears in every table
    assert "how" not in tokens
    assert "created" not in tokenize("createdAt")


def test_graph_expands_across_foreign_keys(snapshot, profile):
    graph = SchemaGraph.build(snapshot, profile)
    expanded = graph.expand(["orders"], hops=1, budget=10)
    assert "users" in expanded


def test_expansion_skips_empty_tables(snapshot, profile):
    graph = SchemaGraph.build(snapshot, profile)
    assert "refunds" not in graph.expand(["orders"], hops=1, budget=10)


def test_seeded_empty_table_is_kept(snapshot, profile):
    graph = SchemaGraph.build(snapshot, profile)
    assert "refunds" in graph.expand(["refunds"], hops=1, budget=5)


def test_fan_out_warning_fires_for_larger_child(snapshot, profile):
    graph = SchemaGraph.build(snapshot, profile)
    warnings = graph.fan_out_warnings(["orders", "order_status_history"])
    assert warnings and "order_status_history" in warnings[0]


def test_fan_out_ignores_tiny_parents(snapshot, profile):
    profile.tables["orders"].exact_rows = 3
    graph = SchemaGraph.build(snapshot, profile)
    assert graph.fan_out_warnings(["orders", "order_status_history"]) == []


def test_value_linking_pins_observed_literal(snapshot, profile):
    linker = SchemaLinker(snapshot, profile)
    linked = linker.link("how many orders were delivered")
    assert any("DELIVERED" in hint for hint in linked.value_hints)


def test_value_linking_requires_every_token(snapshot, profile):
    linker = SchemaLinker(snapshot, profile)
    linked = linker.link("coupon usage by customer")
    assert not any("CONFUSED_CUSTOMER" in hint for hint in linked.value_hints)


def test_sensitive_values_never_reach_the_prompt(snapshot, profile):
    linker = SchemaLinker(snapshot, profile)
    section = linker.link("which email addresses ordered most").as_prompt_section()
    assert "@" not in section
