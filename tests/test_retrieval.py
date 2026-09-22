"""Tests for the bounded, bidirectional, cycle-safe graph walk."""

from __future__ import annotations

import networkx as nx
import pytest

from sqlagent.schema.retrieval import (
    DEFAULT_MAX_HOPS,
    HARD_MAX_HOPS,
    expand,
    widen,
)

# --------------------------------------------------------------------------
# Basic walking
# --------------------------------------------------------------------------


def test_zero_hops_returns_only_the_seed(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=0)
    assert result.tables == {"customers"}
    assert result.distances["customers"] == 0


def test_one_hop_follows_a_declared_key_forwards(graph: nx.DiGraph):
    """customers -> regions: customers declares the key, so this is 'forwards'."""
    result = expand(graph, "customers", max_hops=1)
    assert "regions" in result.tables
    assert result.distances["regions"] == 1


def test_one_hop_follows_an_incoming_key_backwards(graph: nx.DiGraph):
    """The case that breaks direction-respecting traversal.

    ``customers`` declares no key pointing at ``orders`` — the edge runs
    orders -> customers. A walk that only followed outgoing edges would never
    find orders, which is the single most relevant table for most questions
    about customers.
    """
    result = expand(graph, "customers", max_hops=1)
    assert "orders" in result.tables
    assert result.distances["orders"] == 1


def test_one_hop_does_not_reach_two_hops_away(graph: nx.DiGraph):
    """order_items is two hops from customers (via orders) and must be excluded."""
    result = expand(graph, "customers", max_hops=1)
    assert "order_items" not in result.tables


def test_two_hops_reaches_the_second_ring(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=2)
    assert result.distances["order_items"] == 2
    assert result.distances["addresses"] == 2


def test_unrelated_component_is_never_reached(graph: nx.DiGraph):
    """audit_log has no keys at all; no walk should ever pull it in."""
    result = expand(graph, "customers", max_hops=HARD_MAX_HOPS)
    assert "audit_log" not in result.tables


def test_isolated_seed_returns_only_itself(graph: nx.DiGraph):
    result = expand(graph, "audit_log", max_hops=HARD_MAX_HOPS)
    assert result.tables == {"audit_log"}


# --------------------------------------------------------------------------
# Cycles and self-references
# --------------------------------------------------------------------------


def test_self_reference_does_not_list_the_table_as_its_own_neighbour(graph: nx.DiGraph):
    """employees.manager_id -> employees.id must not report distance 1 to itself."""
    result = expand(graph, "employees", max_hops=2)
    assert result.tables == {"employees"}
    assert result.distances["employees"] == 0


def test_mutual_reference_terminates(graph: nx.DiGraph):
    """orders <-> invoices is a cycle; the walk must stop, not spin.

    If the visited set were missing this test would hang rather than fail,
    which is precisely why it exists.
    """
    result = expand(graph, "orders", max_hops=HARD_MAX_HOPS)
    assert result.distances["invoices"] == 1


def test_distance_is_shortest_path_not_discovery_order(graph: nx.DiGraph):
    """Reached by several routes, a table keeps its *shortest* distance."""
    result = expand(graph, "orders", max_hops=HARD_MAX_HOPS)
    assert result.distances["customers"] == 1
    assert result.distances["regions"] == 2


# --------------------------------------------------------------------------
# Multiple seeds
# --------------------------------------------------------------------------


def test_multiple_seeds_are_all_distance_zero(graph: nx.DiGraph):
    result = expand(graph, ["customers", "addresses"], max_hops=1)
    assert result.distances["customers"] == 0
    assert result.distances["addresses"] == 0


def test_distance_is_measured_to_the_nearest_seed(graph: nx.DiGraph):
    """regions is 2 hops from orders but 1 from customers; the nearer wins."""
    result = expand(graph, ["orders", "customers"], max_hops=2)
    assert result.distances["regions"] == 1


def test_seed_order_does_not_change_the_result(graph: nx.DiGraph):
    forward = expand(graph, ["orders", "customers"], max_hops=2)
    backward = expand(graph, ["customers", "orders"], max_hops=2)
    assert forward.distances == backward.distances


# --------------------------------------------------------------------------
# Ordering helpers
# --------------------------------------------------------------------------


def test_ordered_puts_nearest_tables_first(graph: nx.DiGraph):
    ordered = expand(graph, "customers", max_hops=2).ordered()
    assert ordered[0] == "customers"
    assert ordered.index("orders") < ordered.index("order_items")


def test_ordered_breaks_ties_alphabetically_for_determinism(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=1)
    one_hop = [table for table in result.ordered() if result.distances[table] == 1]
    assert one_hop == sorted(one_hop)


def test_at_hop_selects_a_single_ring(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=2)
    assert result.at_hop(0) == ["customers"]
    assert result.at_hop(1) == ["orders", "regions"]


# --------------------------------------------------------------------------
# Limits and widening
# --------------------------------------------------------------------------


def test_max_hops_is_clamped_to_the_hard_ceiling(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=99)
    assert result.hops == HARD_MAX_HOPS


def test_negative_hops_is_treated_as_zero(graph: nx.DiGraph):
    result = expand(graph, "customers", max_hops=-5)
    assert result.tables == {"customers"}


def test_default_is_a_single_hop():
    """Starting narrow is a deliberate cost decision, not an accident."""
    assert DEFAULT_MAX_HOPS == 1


def test_widen_adds_exactly_one_hop(graph: nx.DiGraph):
    narrow = expand(graph, "customers", max_hops=1)
    wider = widen(graph, narrow)
    assert wider.hops == 2
    assert narrow.tables < wider.tables


def test_widen_stops_at_the_ceiling(graph: nx.DiGraph):
    """Callers detect 'cannot widen further' by an unchanged hop count."""
    at_ceiling = expand(graph, "customers", max_hops=HARD_MAX_HOPS)
    assert widen(graph, at_ceiling).hops == HARD_MAX_HOPS


def test_widen_preserves_the_original_seeds(graph: nx.DiGraph):
    narrow = expand(graph, ["customers", "addresses"], max_hops=1)
    assert widen(graph, narrow).seeds == narrow.seeds


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def test_unknown_seed_raises_keyerror(graph: nx.DiGraph):
    """A caller passing a hallucinated table name should hear about it loudly."""
    with pytest.raises(KeyError, match="nonexistent"):
        expand(graph, "nonexistent")


def test_error_names_every_missing_seed(graph: nx.DiGraph):
    with pytest.raises(KeyError) as excinfo:
        expand(graph, ["customers", "ghost_a", "ghost_b"])
    message = str(excinfo.value)
    assert "ghost_a" in message
    assert "ghost_b" in message


def test_empty_seed_list_raises_valueerror(graph: nx.DiGraph):
    with pytest.raises(ValueError, match="at least one seed"):
        expand(graph, [])


# --------------------------------------------------------------------------
# Result immutability
# --------------------------------------------------------------------------


def test_neighbourhood_is_frozen(graph: nx.DiGraph):
    """Results are cached and shared, so accidental mutation must be impossible."""
    result = expand(graph, "customers")
    with pytest.raises(AttributeError):
        result.hops = 3  # type: ignore[misc]
