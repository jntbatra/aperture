"""Turn a schema snapshot into a navigable graph of tables.

The idea in one paragraph
-------------------------
To answer "how much did each customer spend last month?", the agent needs the
``customers`` table *and* the ``orders`` table, plus the exact columns that join
them. Dumping all 56 tables into the prompt would work but is wasteful and
degrades accuracy. Instead we model the database as a graph — each table is a
node, each foreign key is an edge — and retrieve only the neighbourhood around
the tables the question actually mentions.

If you have not used NetworkX before
------------------------------------
NetworkX is a pure-Python graph library. A ``DiGraph`` is a *directed* graph:
edges have a direction, ``A -> B`` is not the same as ``B -> A``. Nodes here are
plain strings (table names) and edges carry a dictionary of attributes (which
columns form the join). That's the whole API surface we use:

* ``graph.add_node("orders", ...)`` — add a table.
* ``graph.add_edge("orders", "customers", ...)`` — record that ``orders`` has a
  foreign key pointing at ``customers``.
* ``graph.successors("orders")`` — tables that ``orders`` points *at* (parents).
* ``graph.predecessors("customers")`` — tables that point *at* ``customers``
  (children).

Why the graph holds structure only
----------------------------------
Nodes carry no column lists. Traversal only ever asks "what is connected to
what", and column detail would make every node heavy for no benefit. The full
column/type detail stays in the ``SchemaSnapshot`` and is looked up by name
*after* traversal has decided which handful of tables matter.

Why it is not a tree (and not even acyclic)
-------------------------------------------
It is tempting to picture foreign keys as a neat hierarchy. Real schemas are
not:

* **Self-references** — ``employees.manager_id -> employees.id``. This is a
  self-loop: an edge from a node to itself.
* **Mutual references** — ``orders.invoice_id -> invoices.id`` while
  ``invoices.order_id -> orders.id``. This is a two-node cycle, and it is legal
  (one side is usually nullable, or the constraint is deferred).

So any traversal must track visited nodes, or it will loop forever. The
retrieval module does exactly that; see ``sqlagent.schema.retrieval``.
"""

from __future__ import annotations

import networkx as nx

from sqlagent.schema.introspect import ForeignKey, SchemaSnapshot

# Edge attribute keys. Defined as constants so a typo becomes an ImportError
# rather than a silently missing attribute at query-construction time.
EDGE_FOREIGN_KEY = "foreign_key"
"""The :class:`ForeignKey` object backing this edge, kept whole.

Storing the object rather than loose column names means composite keys survive
the trip: rendering the join later uses every column pair, not just the first.
"""


def build_graph(snapshot: SchemaSnapshot) -> nx.DiGraph:
    """Build the table graph from a reflected schema.

    Edge direction is **child -> parent**: an edge ``orders -> customers`` means
    "``orders`` holds a foreign key referencing ``customers``". That matches how
    the constraint is declared and is what you need to write the ``ON`` clause.

    Direction is preserved even though retrieval ignores it (see
    ``retrieval.expand``). Keeping it means later stages can still distinguish
    the many-side from the one-side of a relationship, which matters when
    deciding whether a join can multiply rows.

    Args:
        snapshot: The reflected schema.

    Returns:
        A directed graph whose nodes are table names.

    Note:
        Two tables can be joined through more than one foreign key (say, an
        order referencing a ``billing_address`` and a ``shipping_address``, both
        in ``addresses``). A plain ``DiGraph`` stores one edge per node pair, so
        the second key would overwrite the first. We therefore keep a *list* of
        foreign keys on each edge rather than a single one.
    """
    graph = nx.DiGraph()

    # Add every table first, including isolated ones. A table with no foreign
    # keys at all must still be reachable — plenty of useful questions concern
    # a single standalone table, and if it never became a node the agent could
    # never select it.
    for table_name in snapshot.tables:
        graph.add_node(table_name)

    for foreign_key in snapshot.foreign_keys:
        source = foreign_key.source_table
        target = foreign_key.target_table

        # Defensive: a constraint could reference a table outside the reflected
        # scope (another schema, or one excluded by a filter). Add the missing
        # node rather than dropping the relationship silently — a dangling edge
        # is still information, and dropping it would hide a real join path.
        if target not in graph:
            graph.add_node(target)

        if graph.has_edge(source, target):
            graph[source][target][EDGE_FOREIGN_KEY].append(foreign_key)
        else:
            graph.add_edge(source, target, **{EDGE_FOREIGN_KEY: [foreign_key]})

    return graph


def foreign_keys_between(graph: nx.DiGraph, table_a: str, table_b: str) -> list[ForeignKey]:
    """Return every foreign key joining two tables, in either direction.

    Callers generally do not care which side declared the constraint — they just
    need the join predicate. This checks both directions and returns all
    matches, so a pair linked by two different keys yields both.

    Returns an empty list when the tables are not directly related.
    """
    keys: list[ForeignKey] = []

    if graph.has_edge(table_a, table_b):
        keys.extend(graph[table_a][table_b][EDGE_FOREIGN_KEY])
    if table_a != table_b and graph.has_edge(table_b, table_a):
        keys.extend(graph[table_b][table_a][EDGE_FOREIGN_KEY])

    return keys


def describe_edges(graph: nx.DiGraph, tables: set[str]) -> list[str]:
    """Render the joins available *among* a set of tables, as SQL predicates.

    This is what gets placed into the prompt. Showing the model the exact
    predicate (``orders.customer_id = customers.id``) is far more reliable than
    listing table names and trusting it to infer the linking columns from
    naming conventions — which fails the moment a schema uses ``cust_ref`` or
    joins on a composite key.

    Only edges where *both* endpoints are in ``tables`` are included; a join to
    a table the model was not given is not actionable.

    Returns:
        Sorted, de-duplicated predicate strings. Sorted for determinism: the
        same schema must produce the same prompt every time, otherwise
        caching and reproducible evaluation both break.
    """
    predicates: set[str] = set()

    for source, target, data in graph.edges(data=True):
        if source in tables and target in tables:
            for foreign_key in data[EDGE_FOREIGN_KEY]:
                predicates.add(foreign_key.join_condition())

    return sorted(predicates)
