"""Expose the agent over the Model Context Protocol.

What MCP is, in one paragraph
-----------------------------
MCP is a standard way for a program to offer *tools* to a model-driven client —
Claude Desktop, an IDE assistant, another agent. The client asks the server what
tools exist, gets back a name, a description and a JSON schema for each, and can
then call them. It is, roughly, "an API described in a form a model can read".

Why this agent is worth exposing that way
-----------------------------------------
Someone else's assistant, answering a question about a product, may need a
figure out of this database. The alternatives are worse:

* Hand it database credentials and let it write SQL. That discards every
  protection here — the read-only role, the validator, the cost gate, the row
  cap — and puts an unreviewed statement in front of production.
* Wrap the HTTP API by hand, per client. Works, and has to be redone for each.

Over MCP the client gets the *questions*, not the connection. Every call goes
through the same pipeline, the same guards and the same limits as a question
typed into the web interface.

What is deliberately not exposed
--------------------------------
No tool that takes raw SQL. That is the whole point: offering ``run_sql`` would
reduce this to a database proxy and hand the calling model exactly the freedom
the validator exists to remove. The tools here take questions and table names.

The shape of the tools
----------------------
Three, kept deliberately few. A large tool surface makes a model choose badly;
these map onto the three things a caller actually wants — ask something, see
what is there, look at one table.

Running it::

    python -m sqlagent.mcp_server          # stdio, for a desktop client
    python -m sqlagent.mcp_server --http   # streamable HTTP, for a remote one
"""

from __future__ import annotations

import argparse
import logging
from functools import lru_cache
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import create_engine

from sqlagent.config import Settings, settings
from sqlagent.pipeline import SqlAgent

logger = logging.getLogger(__name__)

server = MCPServer(
    name="aperture-sql-analyst",
    instructions=(
        "Answers questions about a specific SQL database in plain language, and "
        "returns the SQL it ran so you can check it. The database is read-only: "
        "no tool here can modify data. Ask questions in natural language — there "
        "is deliberately no tool that accepts raw SQL."
    ),
)


@lru_cache(maxsize=1)
def get_agent() -> SqlAgent:
    """Build the agent once.

    Cached for the same reason the HTTP API caches it: construction reflects the
    whole schema and builds the table graph, which would otherwise be paid on
    every tool call.
    """
    config: Settings = settings()
    engine = create_engine(config.database_url, pool_pre_ping=True)
    agent = SqlAgent(engine, config=config)
    agent.warm()
    return agent


@server.tool(
    name="ask_database",
    description=(
        "Answer a question about the database in plain language. Returns the "
        "answer, the SQL that produced it, and the result rows. Read-only."
    ),
)
def ask_database(question: str) -> dict[str, Any]:
    """One question, one answer.

    The SQL comes back alongside the answer deliberately. A calling model that
    can see the query can tell the difference between "the data says 400" and
    "a query about the wrong column says 400" — which is the same reason the web
    interface shows it.

    Result rows are capped by the agent's own row limit, so a question like
    "list every customer" cannot flood the caller's context.
    """
    agent = get_agent()
    result = agent.ask(question, source="mcp")

    payload: dict[str, Any] = {
        "answer": result.answer,
        "sql": result.sql,
        "ok": result.ok,
        "row_count": result.result.row_count if result.result else 0,
    }

    if result.result is not None:
        payload["columns"] = list(result.result.columns)
        # Bounded independently of the agent's row limit: a thousand rows is a
        # reasonable answer to read on screen and an unreasonable thing to push
        # into another model's context window.
        payload["rows"] = [
            [_jsonable(cell) for cell in row] for row in result.result.rows[:50]
        ]
        payload["rows_shown"] = len(payload["rows"])
        payload["truncated"] = result.result.truncated or result.result.row_count > 50

    if not result.ok:
        payload["error"] = result.error

    # Surfaced so a caller can weigh the answer rather than just take it.
    if result.trace.inflation_warnings:
        payload["warnings"] = result.trace.inflation_warnings
    if result.trace.answer_unverified:
        payload["warnings"] = payload.get("warnings", []) + [
            "the answer could not be verified against the result rows"
        ]

    return payload


@server.tool(
    name="list_tables",
    description=(
        "List the tables in the database with their column counts and the "
        "tables they reference. Use this to find out what can be asked about."
    ),
)
def list_tables() -> dict[str, Any]:
    """Orientation. A caller that cannot see the schema asks about tables that
    do not exist, and gets an error instead of an answer."""
    snapshot = get_agent().snapshot
    return {
        "table_count": len(snapshot),
        "tables": [
            {
                "name": table.name,
                "columns": len(table.columns),
                "references": sorted({fk.target_table for fk in table.foreign_keys}),
            }
            for table in sorted(snapshot.tables.values(), key=lambda t: t.name)
        ],
    }


@server.tool(
    name="describe_table",
    description=(
        "Show one table's columns, types, primary key and foreign keys, "
        "including the exact join conditions to related tables."
    ),
)
def describe_table(table: str) -> dict[str, Any]:
    """Detail for one table, including real join predicates.

    The join conditions come from actual foreign-key constraints rather than
    from column-name guessing, which is the single most valuable thing this
    server can tell a caller that it could not work out for itself.
    """
    snapshot = get_agent().snapshot
    found = snapshot.tables.get(table)

    if found is None:
        # A near-miss suggestion rather than a bare error: the usual cause is a
        # singular/plural slip, and "did you mean orders?" ends the exchange in
        # one turn instead of three.
        close = [name for name in snapshot.tables if table.lower() in name.lower()]
        return {
            "error": f"No table named {table!r}.",
            "did_you_mean": close[:5],
        }

    return {
        "name": found.name,
        "primary_key": list(found.primary_key),
        "columns": [
            {
                "name": column.name,
                "type": column.type,
                "nullable": column.nullable,
                "primary_key": column.primary_key,
            }
            for column in found.columns
        ],
        "foreign_keys": [
            {
                "references": fk.target_table,
                "join_condition": fk.join_condition(),
            }
            for fk in found.foreign_keys
        ],
    }


def _jsonable(cell: object) -> object:
    """Dates, Decimals and UUIDs arrive as driver objects JSON cannot encode."""
    if cell is None or isinstance(cell, bool | int | float | str):
        return cell
    return str(cell)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Aperture MCP server.")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable HTTP instead of stdio (for a remote client).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=settings().log_level)

    # Warm before serving, so the first tool call does not pay for schema
    # reflection — a client that times out on its first call concludes the
    # server is broken.
    get_agent()

    server.run(transport="streamable-http" if args.http else "stdio")


if __name__ == "__main__":
    main()
