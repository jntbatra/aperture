"""The foreign-key graph.

Similarity alone retrieves the tables a question *names*. It misses the
junction tables a query must join *through* -- ask for revenue per kitchen and
retrieval returns `orders` and `kitchen_profiles`, never `item_kitchens`, the
table that actually connects them. Walking foreign keys finds those.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import networkx as nx

from ..db.introspect import SchemaSnapshot
from ..db.profile import DatabaseProfile


@dataclass
class SchemaGraph:
    graph: nx.Graph
    snapshot: SchemaSnapshot

    @classmethod
    def build(cls, snapshot: SchemaSnapshot, profile: DatabaseProfile | None = None) -> "SchemaGraph":
        """Build the table graph.

        Row counts come from the profile when available: `pg_class.reltuples`
        is -1 for a table that has never been analysed, which would silently
        disable fan-out detection.
        """
        graph = nx.Graph()
        for name, table in snapshot.tables.items():
            rows = table.approx_rows
            if profile is not None and name in profile.tables:
                rows = profile.tables[name].exact_rows
            graph.add_node(name, rows=max(rows, 0), columns=len(table.columns))

        for fk in snapshot.foreign_keys:
            if fk.src_table in graph and fk.tgt_table in graph:
                graph.add_edge(
                    fk.src_table,
                    fk.tgt_table,
                    src_column=fk.src_column,
                    tgt_column=fk.tgt_column,
                )
        return cls(graph=graph, snapshot=snapshot)

    def expand(
        self, seeds: list[str], *, hops: int = 1, budget: int = 25, skip_empty: bool = True
    ) -> list[str]:
        """BFS out from `seeds`, nearest first, capped at `budget` tables.

        Empty tables are skipped unless explicitly seeded: they cannot
        contribute rows to any answer, so spending prompt budget on them buys
        nothing. A seeded empty table is kept, because "that table has no rows"
        is the correct answer to a question about it.
        """
        selected = list(dict.fromkeys(s for s in seeds if s in self.graph))
        if not selected:
            return []

        queue: deque[tuple[str, int]] = deque((s, 0) for s in selected)
        seen = set(selected)
        while queue and len(selected) < budget:
            node, depth = queue.popleft()
            if depth >= hops:
                continue
            candidates = [
                n
                for n in self.graph.neighbors(node)
                if not (skip_empty and self.graph.nodes[n].get("rows", 0) == 0)
            ]
            # Among live neighbours, bigger tables are likelier to be the fact
            # table the question is really about.
            neighbours = sorted(
                candidates, key=lambda n: -self.graph.nodes[n].get("rows", 0)
            )
            for neighbour in neighbours:
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                selected.append(neighbour)
                queue.append((neighbour, depth + 1))
                if len(selected) >= budget:
                    break
        return selected

    def join_path(self, source: str, target: str) -> list[str]:
        """Shortest table path between two tables, empty if unconnected."""
        if source not in self.graph or target not in self.graph:
            return []
        try:
            return nx.shortest_path(self.graph, source, target)
        except nx.NetworkXNoPath:
            return []

    def join_hints(self, tables: list[str]) -> list[str]:
        """Render the foreign-key edges among `tables` as join conditions."""
        chosen = set(tables)
        hints = [
            f"{src}.{data['src_column']} = {tgt}.{data['tgt_column']}"
            for src, tgt, data in self.graph.edges(data=True)
            if src in chosen and tgt in chosen
        ]
        return sorted(set(hints))

    def fan_out_warnings(
        self, tables: list[str], *, ratio_threshold: float = 2.0, parent_min_rows: int = 100
    ) -> list[str]:
        """Flag child tables far larger than their parent.

        A legal join through such a table multiplies parent rows, so any SUM
        over parent columns is silently inflated -- no error, just a wrong
        number. This is the failure mode a demo audience never catches.
        """
        chosen = set(tables)
        warnings = []
        for left, right, _ in self.graph.edges(data=True):
            if left not in chosen or right not in chosen:
                continue
            # The graph is undirected and edge orientation is an accident of
            # insertion order, so compare by size rather than by position.
            pair = sorted(
                (left, right), key=lambda name: self.graph.nodes[name].get("rows", 0)
            )
            parent, child = pair[0], pair[1]
            parent_rows = self.graph.nodes[parent].get("rows", 0)
            child_rows = self.graph.nodes[child].get("rows", 0)
            # A one-row lookup table is not a fan-out risk, it is a lookup
            # table; warning about it drowns the warning that matters.
            if parent_rows >= parent_min_rows and child_rows > parent_rows * ratio_threshold:
                warnings.append(
                    f"{child} holds ~{child_rows / max(parent_rows, 1):.1f}x the rows of "
                    f"{parent}; joining them multiplies {parent} rows -- aggregate {child} "
                    f"in a subquery before joining, or use COUNT(DISTINCT ...)"
                )
        return sorted(set(warnings))
