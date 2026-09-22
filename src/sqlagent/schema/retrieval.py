"""Select the handful of tables relevant to a question, by walking the graph.

The problem
-----------
A 56-table schema is far too much to put in a prompt. Most questions touch two
or three tables. So: identify the table the question is *about*, then walk
outward along foreign keys to pick up the tables it joins to, and stop early.

Breadth-first, not depth-first
------------------------------
Breadth-first search (BFS) visits everything one hop away, then everything two
hops away, and so on. That ordering is exactly what we want, because hop
distance is a decent proxy for relevance: ``orders`` (one hop from
``customers``) is far likelier to matter than ``shipping_carriers`` (three hops
away through two intermediate tables).

Depth-first search would instead tunnel down a single chain of foreign keys and
might return a distant, irrelevant table before a directly-adjacent one.

Why direction is ignored during the walk
----------------------------------------
Foreign keys point child -> parent. ``orders`` declares a key referencing
``customers``, so the edge is ``orders -> customers``.

Now consider a question about customers. Starting at ``customers`` and
following edges *forwards* finds nothing: ``customers`` is a parent, it declares
no keys, it has no outgoing edges. But ``orders`` is obviously relevant.

So traversal treats the graph as undirected — it follows edges in both
directions. Direction is still recorded on each edge and is used later, when
constructing the actual join; it just must not constrain *discovery*.

Why a hop cap is mandatory, not a tuning knob
---------------------------------------------
Two reasons:

1. **Relevance decays.** Three hops out in a normalised schema can reach most of
   the database. Returning 40 tables is no better than returning all 56.
2. **The graph can contain cycles.** ``employees.manager_id -> employees.id`` is
   a self-loop; mutually-referencing tables form a two-node cycle. The visited
   set below prevents infinite looping, and the cap bounds the work regardless.

Strategy: start narrow (one hop), and widen only if generation later fails
because a needed table was missing. Most questions never pay for the wider
search.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import networkx as nx

DEFAULT_MAX_HOPS = 1
"""Start with immediate neighbours only.

A question like "what did each customer spend" needs ``customers`` plus
``orders`` — one hop. Widening happens on demand, driven by failure, not
speculatively on every request.
"""

HARD_MAX_HOPS = 3
"""Absolute ceiling, even when retrying.

Past three hops the candidate set stops being a focused neighbourhood and
starts being "most of the schema". If the answer genuinely is not reachable
within three hops, the right move is to ask the user, not to widen further.
"""


@dataclass(frozen=True, slots=True)
class Neighbourhood:
    """The result of a walk: which tables were found, and how far away each is."""

    seeds: tuple[str, ...]
    """The starting table(s) the walk began from."""

    hops: int
    """The hop limit that produced this result."""

    distances: dict[str, int]
    """Table name -> hop distance from the nearest seed. Seeds are distance 0."""

    @property
    def tables(self) -> set[str]:
        return set(self.distances)

    def ordered(self) -> list[str]:
        """Tables sorted nearest-first, ties broken alphabetically.

        Prompt order matters: models weight earlier content more heavily, so
        the most likely-relevant tables should appear first. The alphabetical
        tiebreak keeps output deterministic, which matters for caching and for
        reproducible evaluation runs.
        """
        return sorted(self.distances, key=lambda table: (self.distances[table], table))

    def at_hop(self, distance: int) -> list[str]:
        """Tables at exactly this hop distance, sorted."""
        return sorted(t for t, d in self.distances.items() if d == distance)


def expand(
    graph: nx.DiGraph,
    seeds: str | list[str] | tuple[str, ...],
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Neighbourhood:
    """Walk outward from one or more starting tables.

    Args:
        graph: The schema graph from :func:`sqlagent.schema.graph.build_graph`.
        seeds: Starting table name, or several. Multiple seeds are normal — a
            question like "orders by customers in Germany" names two entities,
            and starting from both finds the connecting path faster than
            starting from one and hoping to reach the other.
        max_hops: How far to walk. Clamped to :data:`HARD_MAX_HOPS`. Zero is
            legal and returns only the seeds, which is useful when the caller
            already knows the exact tables.

    Returns:
        A :class:`Neighbourhood`. Distance is measured to the *nearest* seed.

    Raises:
        KeyError: If a seed table is not in the graph. This is a programming
            error rather than a user error — the caller is expected to have
            resolved the question to real table names first — so it fails loudly
            rather than silently returning nothing.

    Implementation note:
        This is a hand-written BFS rather than
        ``nx.single_source_shortest_path_length`` because we need multiple
        seeds, and because building an undirected copy of the graph on every
        request would be wasteful. Instead we inspect successors and
        predecessors at each step, which is equivalent to walking the
        undirected graph without materialising one.
    """
    seeds = (seeds,) if isinstance(seeds, str) else tuple(seeds)

    if not seeds:
        raise ValueError("expand() requires at least one seed table")

    missing = [table for table in seeds if table not in graph]
    if missing:
        raise KeyError(
            f"seed table(s) not present in schema graph: {', '.join(sorted(missing))}"
        )

    hop_limit = max(0, min(max_hops, HARD_MAX_HOPS))

    # `distances` doubles as the visited set. A table already recorded here has
    # been reached by an equal or shorter path, so it is never re-enqueued.
    # This is what makes cycles safe: a self-loop or mutual foreign key finds
    # its target already visited and stops.
    distances: dict[str, int] = {seed: 0 for seed in seeds}
    queue: deque[str] = deque(seeds)

    while queue:
        current = queue.popleft()
        current_distance = distances[current]

        if current_distance >= hop_limit:
            # Far enough. Do not expand this node, but keep draining the queue —
            # other entries may still be closer in and need expanding.
            continue

        for neighbour in _adjacent(graph, current):
            if neighbour not in distances:
                distances[neighbour] = current_distance + 1
                queue.append(neighbour)

    return Neighbourhood(seeds=seeds, hops=hop_limit, distances=distances)


def _adjacent(graph: nx.DiGraph, table: str) -> set[str]:
    """Every table directly linked to this one, regardless of edge direction.

    ``successors`` are the tables this one references (its parents);
    ``predecessors`` are the tables referencing it (its children). Relevance
    runs both ways, so both count as neighbours.

    A self-referencing table (``employees.manager_id -> employees.id``) appears
    in its own successor set. It is excluded here — a table is not its own
    neighbour, and including it would report a spurious distance.
    """
    neighbours = set(graph.successors(table)) | set(graph.predecessors(table))
    neighbours.discard(table)
    return neighbours


def widen(graph: nx.DiGraph, neighbourhood: Neighbourhood) -> Neighbourhood:
    """Re-walk one hop further than last time.

    Called when SQL generation failed because a required table was not in the
    candidate set — the recovery path described as "Loop B" in the design
    document. Returns an unchanged neighbourhood once the hard ceiling is hit,
    so callers can detect "cannot widen further" by comparing ``hops``.
    """
    if neighbourhood.hops >= HARD_MAX_HOPS:
        return neighbourhood
    return expand(graph, neighbourhood.seeds, max_hops=neighbourhood.hops + 1)
