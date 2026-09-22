"""Tests for the MCP server.

The property worth protecting hardest: there is no tool that takes raw SQL.
Offering one would reduce this to a database proxy and hand the calling model
exactly the freedom the validator exists to remove.
"""

from __future__ import annotations

import asyncio

import pytest

import sqlagent.mcp_server as mcp
from sqlagent.db.execute import QueryResult
from sqlagent.pipeline import AgentResult, Trace
from sqlagent.schema.introspect import Column, ForeignKey, SchemaSnapshot, Table

SNAPSHOT = SchemaSnapshot(
    tables={
        "customers": Table(
            "customers",
            (Column("id", "INTEGER", False, True), Column("name", "TEXT", True, False)),
            (),
        ),
        "orders": Table(
            "orders",
            (Column("id", "INTEGER", False, True), Column("customer_id", "INTEGER", True, False)),
            (ForeignKey("orders", ("customer_id",), "customers", ("id",)),),
        ),
    },
    version="v1",
)


class StubAgent:
    def __init__(self, *, ok: bool = True, rows=None, warnings=()):
        self.snapshot = SNAPSHOT
        self._ok = ok
        self._rows = rows if rows is not None else ((3,),)
        self._warnings = list(warnings)
        self.asked: list[str] = []

    def ask(self, question, **kwargs):
        self.asked.append(question)
        trace = Trace(question=question)
        trace.inflation_warnings = self._warnings
        return AgentResult(
            question=question,
            answer="There are 3 customers." if self._ok else "I could not answer that.",
            sql="SELECT count(*) FROM customers" if self._ok else None,
            result=QueryResult(
                columns=("count",), rows=self._rows, seconds=0.01, truncated=False
            )
            if self._ok
            else None,
            trace=trace,
            error=None if self._ok else "syntax error",
        )


@pytest.fixture
def agent(monkeypatch):
    stub = StubAgent()
    monkeypatch.setattr(mcp, "get_agent", lambda: stub)
    return stub


def tools():
    return {tool.name: tool for tool in asyncio.run(mcp.server.list_tools())}


# --------------------------------------------------------------------------
# The tool surface
# --------------------------------------------------------------------------


def test_no_tool_accepts_raw_sql():
    """The whole point. A `run_sql` tool would discard every guard here."""
    for tool in tools().values():
        properties = tool.input_schema.get("properties", {})
        assert "sql" not in properties
        assert "query" not in properties


def test_the_surface_is_small():
    """A large tool surface makes a model choose badly."""
    assert set(tools()) == {"ask_database", "list_tables", "describe_table"}


def test_every_tool_describes_itself():
    """The description is how the calling model decides what to call."""
    for tool in tools().values():
        assert tool.description and len(tool.description) > 30


def test_the_server_states_that_the_database_is_read_only():
    assert "read-only" in (mcp.server.instructions or "")


# --------------------------------------------------------------------------
# ask_database
# --------------------------------------------------------------------------


def test_asking_returns_the_answer_and_the_sql(agent):
    """A caller that can see the query can tell "the data says 400" from
    "a query about the wrong column says 400"."""
    payload = mcp.ask_database("How many customers?")

    assert payload["answer"] == "There are 3 customers."
    assert payload["sql"] == "SELECT count(*) FROM customers"
    assert payload["ok"] is True
    assert payload["rows"] == [[3]]


def test_a_failure_comes_back_as_data_not_an_exception(agent, monkeypatch):
    monkeypatch.setattr(mcp, "get_agent", lambda: StubAgent(ok=False))

    payload = mcp.ask_database("something impossible")

    assert payload["ok"] is False
    assert payload["error"] == "syntax error"


def test_rows_are_capped_so_they_cannot_flood_the_caller(monkeypatch):
    """A thousand rows is a reasonable answer on screen and an unreasonable
    thing to push into another model's context."""
    monkeypatch.setattr(
        mcp, "get_agent", lambda: StubAgent(rows=tuple((i,) for i in range(500)))
    )

    payload = mcp.ask_database("list everything")

    assert payload["rows_shown"] == 50
    assert payload["truncated"] is True


def test_warnings_are_surfaced_so_a_caller_can_weigh_the_answer(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "get_agent",
        lambda: StubAgent(warnings=["SUM(o.total) may be inflated"]),
    )

    payload = mcp.ask_database("total revenue per customer")

    assert "inflated" in payload["warnings"][0]


# --------------------------------------------------------------------------
# list_tables / describe_table
# --------------------------------------------------------------------------


def test_listing_tables_reports_what_can_be_asked_about(agent):
    payload = mcp.list_tables()

    assert payload["table_count"] == 2
    assert [table["name"] for table in payload["tables"]] == ["customers", "orders"]


def test_listing_reports_relationships(agent):
    payload = mcp.list_tables()
    orders = next(t for t in payload["tables"] if t["name"] == "orders")

    assert orders["references"] == ["customers"]


def test_describing_a_table_gives_real_join_conditions(agent):
    """From actual foreign keys, not column-name guessing — the most valuable
    thing this server can tell a caller."""
    payload = mcp.describe_table("orders")

    assert payload["primary_key"] == ["id"]
    assert payload["foreign_keys"][0]["join_condition"] == (
        "orders.customer_id = customers.id"
    )


def test_an_unknown_table_suggests_a_near_miss(agent):
    """The usual cause is a singular/plural slip, and a suggestion ends the
    exchange in one turn instead of three."""
    payload = mcp.describe_table("order")

    assert "error" in payload
    assert "orders" in payload["did_you_mean"]


def test_an_unknown_table_with_no_near_miss_still_answers(agent):
    payload = mcp.describe_table("zzzz")

    assert payload["did_you_mean"] == []
