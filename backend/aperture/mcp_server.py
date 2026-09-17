"""MCP server.

Exposes the same guarded path over stdio, so any MCP-capable agent or IDE can
query the database without being handed a connection string: writes are refused
by the validator and by the read-only role underneath it.
"""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer

from .charts import jsonable
from .config import settings
from .db import Database, load_schema
from .graph import build_analyst, sync_checkpointer
from .guards.validator import validate_sql
from .schema import SchemaLinker

log = logging.getLogger(__name__)

server = MCPServer(name="aperture")

_STATE: dict = {}


def _ctx():
    if "graph" not in _STATE:
        graph, ctx = build_analyst(checkpointer=sync_checkpointer())
        _STATE["graph"] = graph
        _STATE["ctx"] = ctx
        _STATE["linker"] = SchemaLinker(ctx.bundle.snapshot, ctx.bundle.profile)
    return _STATE


@server.tool(
    name="ask_database",
    description=(
        "Answer a natural-language question about the connected SQL database. "
        "Returns the generated SQL, the rows, and a short summary. Read-only."
    ),
)
def ask_database(question: str, thread_id: str = "mcp") -> str:
    state = _ctx()
    result = state["graph"].invoke(
        {"question": question}, {"configurable": {"thread_id": thread_id}}
    )
    return json.dumps(
        {
            "answer": result.get("answer"),
            "sql": result.get("sql"),
            "columns": result.get("columns", []),
            "rows": [[jsonable(v) for v in row] for row in (result.get("rows") or [])[:50]],
            "row_count": result.get("row_count", 0),
            "status": result.get("status"),
            "assumptions": result.get("assumptions"),
        },
        indent=2,
    )


@server.tool(
    name="run_sql",
    description=(
        "Execute a read-only SQL query against the connected database. Writes, "
        "multiple statements and locking reads are rejected before execution."
    ),
)
def run_sql(sql: str) -> str:
    state = _ctx()
    db: Database = state["ctx"].db
    result = validate_sql(sql, dialect=db.sqlglot_dialect, row_limit=settings().row_limit)
    if not result.ok:
        return json.dumps({"error": result.reason, "kind": result.kind})

    output = db.run(result.sql)
    return json.dumps(
        {
            "sql": result.sql,
            "columns": output.columns,
            "rows": [[jsonable(v) for v in row] for row in output.rows[:200]],
            "row_count": output.row_count,
            "truncated": output.truncated,
        },
        indent=2,
    )


@server.tool(
    name="describe_schema",
    description="Describe the database: tables, row counts, foreign keys and empty tables.",
)
def describe_schema(table: str = "") -> str:
    state = _ctx()
    bundle = state["ctx"].bundle
    if table:
        return bundle.snapshot.ddl_for([table], profile=bundle.profile) or f"no such table: {table}"
    lines = [f"{len(bundle.snapshot.tables)} tables, {len(bundle.snapshot.foreign_keys)} foreign keys"]
    for name, profile in sorted(
        bundle.profile.tables.items(), key=lambda pair: -pair[1].exact_rows
    )[:25]:
        lines.append(f"  {name}: {profile.exact_rows:,} rows")
    if bundle.profile.empty_tables:
        lines.append("empty: " + ", ".join(bundle.profile.empty_tables))
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
