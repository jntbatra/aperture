"""Graph wiring.

The shape is a loop, not a chain: validation, cost estimation and execution can
each send the query back to be rewritten, carrying the database's own error
with it. That cycle is the product.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ..config import settings
from .nodes import AnalystContext, make_nodes
from .state import AnalystState

# Statuses that end a run. "answered" is deliberately absent: it is set by the
# final nodes, and treating it as a stop condition mid-run lets a previous
# turn's status terminate the current one.
TERMINAL_STATUSES = {"refused", "over_budget", "timed_out", "unavailable"}


def checkpoint_path() -> Path:
    home = Path(os.path.expanduser(settings().home_dir))
    home.mkdir(parents=True, exist_ok=True)
    return home / "state.db"


def sync_checkpointer():
    """SQLite checkpointer owning its own connection.

    `from_conn_string` is a context manager that closes the connection on exit,
    which is wrong for anything longer-lived than a single `with` block.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(checkpoint_path()), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


async def async_checkpointer():
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(str(checkpoint_path()))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver


def _after_route(state: AnalystState) -> str:
    return "link_schema" if state.get("intent") == "query" else "small_talk"


def _after_generate(state: AnalystState) -> str:
    if state.get("status") in TERMINAL_STATUSES:
        return END
    if state.get("last_error"):
        return "diagnose"
    return "validate" if state.get("sql") else "diagnose"


def _after_validate(state: AnalystState) -> str:
    if state.get("status") in TERMINAL_STATUSES:
        return END
    return "diagnose" if state.get("last_error") else "cost_guard"


def _after_cost_guard(state: AnalystState) -> str:
    if state.get("status") in TERMINAL_STATUSES:
        return END
    return "diagnose" if state.get("last_error") else "execute"


def _is_zero_scalar(state: AnalystState) -> bool:
    """A single row holding a single zero is an empty answer wearing a number."""
    rows = state.get("rows") or []
    if len(rows) != 1 or len(rows[0]) != 1:
        return False
    value = rows[0][0]
    return value in (0, None) or str(value) in {"0", "0.0", "None"}


def _after_execute(state: AnalystState) -> str:
    if state.get("status") in TERMINAL_STATUSES:
        return END
    if state.get("last_error"):
        return "diagnose"
    if state.get("row_count", 0) == 0 or _is_zero_scalar(state):
        return "diagnose_empty"
    return "verify"


def _after_diagnose(state: AnalystState) -> str:
    if state.get("attempts", 0) >= settings().max_repair_attempts:
        return "exhausted"
    return "generate_sql"


def _after_diagnose_empty(state: AnalystState) -> str:
    return "generate_sql" if state.get("last_error_kind") == "empty_result" else "narrate"


def build_analyst(ctx: AnalystContext | None = None, *, checkpointer=None):
    """Compile the analyst graph."""
    ctx = ctx or AnalystContext.create()
    nodes = make_nodes(ctx)

    builder = StateGraph(AnalystState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)

    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", _after_route, ["link_schema", "small_talk"])
    builder.add_edge("small_talk", END)
    builder.add_edge("link_schema", "generate_sql")
    builder.add_conditional_edges("generate_sql", _after_generate, ["validate", "diagnose", END])
    builder.add_conditional_edges("validate", _after_validate, ["cost_guard", "diagnose", END])
    builder.add_conditional_edges("cost_guard", _after_cost_guard, ["execute", "diagnose", END])
    builder.add_conditional_edges(
        "execute", _after_execute, ["verify", "diagnose", "diagnose_empty", END]
    )
    builder.add_edge("verify", "narrate")
    builder.add_conditional_edges("diagnose", _after_diagnose, ["generate_sql", "exhausted"])
    builder.add_conditional_edges(
        "diagnose_empty", _after_diagnose_empty, ["generate_sql", "narrate"]
    )
    builder.add_edge("narrate", "chart")
    builder.add_edge("chart", END)
    builder.add_edge("exhausted", END)

    return builder.compile(checkpointer=checkpointer), ctx
