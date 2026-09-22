"""Schema understanding: reflection, graph construction, and retrieval.

Pipeline through this package:

1. :func:`~sqlagent.schema.introspect.reflect_schema` reads the live database
   and returns an immutable :class:`~sqlagent.schema.introspect.SchemaSnapshot`
   (tables, columns, foreign keys, plus a version fingerprint).
2. :func:`~sqlagent.schema.graph.build_graph` turns that snapshot into a
   NetworkX graph: tables are nodes, foreign keys are edges.
3. :func:`~sqlagent.schema.retrieval.expand` walks the graph outward from the
   tables a question mentions, returning only a nearby neighbourhood rather
   than the whole schema.

Steps 1 and 2 run once per schema version and are cached. Step 3 runs per
request and is pure in-memory graph work — no database round trip.
"""

from sqlagent.schema.graph import build_graph, describe_edges, foreign_keys_between
from sqlagent.schema.introspect import (
    Column,
    ForeignKey,
    SchemaSnapshot,
    Table,
    compute_version,
    reflect_schema,
)
from sqlagent.schema.retrieval import (
    DEFAULT_MAX_HOPS,
    HARD_MAX_HOPS,
    Neighbourhood,
    expand,
    widen,
)

__all__ = [
    "DEFAULT_MAX_HOPS",
    "HARD_MAX_HOPS",
    "Column",
    "ForeignKey",
    "Neighbourhood",
    "SchemaSnapshot",
    "Table",
    "build_graph",
    "compute_version",
    "describe_edges",
    "expand",
    "foreign_keys_between",
    "reflect_schema",
    "widen",
]
