"""Tests for turning a schema snapshot into a graph of tables."""

from __future__ import annotations

import networkx as nx
import pytest

from sqlagent.schema.graph import (
    EDGE_FOREIGN_KEY,
    build_graph,
    describe_edges,
    foreign_keys_between,
)
from sqlagent.schema.introspect import ForeignKey, SchemaSnapshot, Table, compute_version

# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def test_every_table_becomes_a_node(graph: nx.DiGraph, snapshot: SchemaSnapshot):
    assert set(graph.nodes) == set(snapshot.tables)


def test_isolated_table_is_still_a_node(graph: nx.DiGraph):
    """A table with no foreign keys must be selectable.

    If it never became a node, no question could ever reach it.
    """
    assert "audit_log" in graph
    assert graph.degree("audit_log") == 0


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_edge_points_from_child_to_parent(graph: nx.DiGraph):
    """orders holds the key referencing customers, so orders -> customers."""
    assert graph.has_edge("orders", "customers")
    assert not graph.has_edge("customers", "orders")


def test_two_keys_to_the_same_table_are_both_kept(graph: nx.DiGraph):
    """orders references addresses twice (billing + shipping).

    A DiGraph stores a single edge per node pair, so without the list-valued
    edge attribute the second key would silently overwrite the first and one
    of the two join paths would vanish.
    """
    keys = graph["orders"]["addresses"][EDGE_FOREIGN_KEY]
    assert len(keys) == 2
    assert {key.source_columns[0] for key in keys} == {
        "billing_address_id",
        "shipping_address_id",
    }


def test_self_reference_creates_a_self_loop(graph: nx.DiGraph):
    assert graph.has_edge("employees", "employees")


def test_mutual_references_create_a_cycle(graph: nx.DiGraph):
    """orders <-> invoices. The graph is explicitly not acyclic."""
    assert graph.has_edge("orders", "invoices")
    assert graph.has_edge("invoices", "orders")
    assert not nx.is_directed_acyclic_graph(graph)


def test_edge_to_table_outside_snapshot_is_preserved():
    """A key referencing an unreflected table adds the node rather than dropping it.

    Silently discarding the relationship would hide a real join path; adding
    the node keeps the information visible to later stages.
    """
    tables = {
        "orders": Table(
            name="orders",
            columns=(),
            foreign_keys=(ForeignKey("orders", ("archive_id",), "archived_orders", ("id",)),),
        )
    }
    snapshot = SchemaSnapshot(tables=tables, version=compute_version(tables))

    graph = build_graph(snapshot)

    assert "archived_orders" in graph
    assert graph.has_edge("orders", "archived_orders")


def test_empty_schema_produces_empty_graph():
    snapshot = SchemaSnapshot(tables={}, version=compute_version({}))
    assert len(build_graph(snapshot).nodes) == 0


# --------------------------------------------------------------------------
# foreign_keys_between
# --------------------------------------------------------------------------


def test_foreign_keys_between_is_direction_agnostic(graph: nx.DiGraph):
    """Callers want the join predicate, not a lesson in who declared it."""
    forward = foreign_keys_between(graph, "orders", "customers")
    backward = foreign_keys_between(graph, "customers", "orders")
    assert len(forward) == 1
    assert forward == backward


def test_foreign_keys_between_returns_both_keys_of_a_double_link(graph: nx.DiGraph):
    assert len(foreign_keys_between(graph, "orders", "addresses")) == 2


def test_foreign_keys_between_collects_both_sides_of_a_cycle(graph: nx.DiGraph):
    """orders and invoices reference each other, so both keys are returned."""
    keys = foreign_keys_between(graph, "orders", "invoices")
    assert len(keys) == 2
    assert {key.source_table for key in keys} == {"orders", "invoices"}


def test_foreign_keys_between_unrelated_tables_is_empty(graph: nx.DiGraph):
    assert foreign_keys_between(graph, "orders", "audit_log") == []


def test_foreign_keys_between_self_reference_is_not_double_counted(graph: nx.DiGraph):
    """A self-loop must be reported once, not twice."""
    assert len(foreign_keys_between(graph, "employees", "employees")) == 1


# --------------------------------------------------------------------------
# describe_edges
# --------------------------------------------------------------------------


def test_describe_edges_renders_join_predicates(graph: nx.DiGraph):
    predicates = describe_edges(graph, {"orders", "customers"})
    assert predicates == ["orders.customer_id = customers.id"]


def test_describe_edges_skips_edges_leaving_the_selection(graph: nx.DiGraph):
    """A join to a table the model was not shown is not actionable."""
    predicates = describe_edges(graph, {"orders", "customers"})
    assert not any("addresses" in predicate for predicate in predicates)


def test_describe_edges_includes_every_key_between_a_pair(graph: nx.DiGraph):
    predicates = describe_edges(graph, {"orders", "addresses"})
    assert predicates == [
        "orders.billing_address_id = addresses.id",
        "orders.shipping_address_id = addresses.id",
    ]


def test_describe_edges_output_is_sorted_and_deterministic(graph: nx.DiGraph):
    """Prompts must be byte-identical across runs or caching breaks."""
    tables = {"orders", "customers", "addresses", "invoices"}
    first = describe_edges(graph, tables)
    assert first == sorted(first)
    assert first == describe_edges(graph, tables)


def test_describe_edges_with_single_table_returns_nothing(graph: nx.DiGraph):
    assert describe_edges(graph, {"customers"}) == []


def test_describe_edges_renders_composite_keys_fully():
    """Both column pairs must appear, ANDed together."""
    tables = {
        "order_items": Table(
            name="order_items",
            columns=(),
            foreign_keys=(
                ForeignKey("order_items", ("tenant_id", "order_id"), "orders", ("tenant_id", "id")),
            ),
        ),
        "orders": Table(name="orders", columns=(), foreign_keys=()),
    }
    snapshot = SchemaSnapshot(tables=tables, version=compute_version(tables))
    graph = build_graph(snapshot)

    assert describe_edges(graph, {"order_items", "orders"}) == [
        "order_items.tenant_id = orders.tenant_id AND order_items.order_id = orders.id"
    ]


# --------------------------------------------------------------------------
# Integration
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_graph_from_live_schema_matches_handbuilt(live_snapshot: SchemaSnapshot, graph: nx.DiGraph):
    live_graph = build_graph(live_snapshot)
    assert set(live_graph.nodes) == set(graph.nodes)
    assert set(live_graph.edges) == set(graph.edges)
