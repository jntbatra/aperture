"""Candidates written from different schema renderings, chosen by their rows."""

from __future__ import annotations

import networkx as nx
import pytest
from tests.test_toggles import (
    ScriptedClient,
    build,
    database,  # noqa: F401 - pytest fixture
)

from sqlagent.prompts import RENDER_STYLES, render_schema
from sqlagent.schema.docs import ColumnDoc
from sqlagent.schema.introspect import Column, SchemaSnapshot, Table, compute_version
from sqlagent.voting import result_key, vote_on_results


def _snapshot():
    tables = {
        "orders": Table(
            name="orders",
            columns=(
                Column(name="id", type="INTEGER", nullable=False, primary_key=True),
                Column(name="status", type="TEXT", nullable=True, primary_key=False),
            ),
            foreign_keys=(),
        )
    }
    return SchemaSnapshot(tables=tables, version=compute_version(tables))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("style", RENDER_STYLES)
def test_every_style_carries_every_column_type_key_and_description(style):
    """Only the layout may change. A candidate written from a rendering that
    dropped a fact would lose for the wrong reason."""
    text = render_schema(
        _snapshot(),
        nx.DiGraph(),
        ["orders"],
        docs={("orders", "status"): ColumnDoc(values="'new', 'shipped'")},
        dialect="sqlite",
        style=style,
    )

    assert "id" in text and "status" in text
    assert "INTEGER" in text and "TEXT" in text
    assert "PRIMARY KEY".casefold() in text.casefold()
    assert "'new', 'shipped'" in text


def test_the_three_styles_are_actually_different():
    renderings = {
        render_schema(_snapshot(), nx.DiGraph(), ["orders"], dialect="sqlite", style=s)
        for s in RENDER_STYLES
    }
    assert len(renderings) == 3


def test_the_default_style_is_unchanged():
    """Turning the toggle off must leave every existing prompt byte-identical."""
    snapshot = _snapshot()
    assert render_schema(snapshot, nx.DiGraph(), ["orders"], dialect="sqlite") == (
        render_schema(snapshot, nx.DiGraph(), ["orders"], dialect="sqlite", style="compact")
    )


def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError):
        render_schema(_snapshot(), nx.DiGraph(), ["orders"], style="yaml")


# --------------------------------------------------------------------------
# Voting on results
# --------------------------------------------------------------------------


def test_row_order_and_float_noise_do_not_split_a_result():
    assert result_key(((1, 2.0), (3, 4.0))) == result_key(((3, 4.0000000001), (1, 2.0)))


def test_a_majority_of_matching_results_wins_outright():
    a, b = result_key(((1,),)), result_key(((2,),))
    vote = vote_on_results([b, a, a])
    assert (vote.index, vote.agreement, vote.contenders) == (1, 2, ())


def test_a_failed_candidate_cannot_win():
    a = result_key(((1,),))
    vote = vote_on_results([None, a, None])
    assert (vote.index, vote.contenders) == (1, ())


def test_three_different_results_go_to_a_tie_break():
    keys = [result_key(((n,),)) for n in (1, 2, 3)]
    vote = vote_on_results(keys)
    assert vote.contenders == (0, 1, 2)


def test_nothing_ran_falls_back_to_the_first_candidate():
    vote = vote_on_results([None, None, None])
    assert (vote.index, vote.agreement, vote.contenders) == (0, 0, ())


# --------------------------------------------------------------------------
# End to end through the graph
# --------------------------------------------------------------------------


def test_three_renderings_three_calls_at_temperature_zero_and_the_majority_wins(database):  # noqa: F811
    client = ScriptedClient(
        "SELECT count(*) FROM orders",
        "SELECT count(*) FROM customers",
        "SELECT COUNT(id) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, candidate_renderings=3)

    result = agent.ask("How many customers?")

    # Both customer counts return 2 — different SQL, same rows — so they win
    # without a tie-break. The orders count also returns 2 here, so make the
    # assertion about the call pattern, which is what the toggle controls.
    assert client.temperatures[:3] == [None, None, None]
    assert "Tables:" in client.prompts[0] and "CREATE TABLE" in client.prompts[1]
    assert "Table " in client.prompts[2] and "  - " in client.prompts[2]
    assert result.trace.vote_samples == 3
    assert result.ok


def test_a_split_costs_exactly_one_tie_break_call(database):  # noqa: F811
    client = ScriptedClient(
        "SELECT name FROM customers WHERE id = 1",
        "SELECT name FROM customers WHERE id = 2",
        "SELECT total FROM orders WHERE id = 1",
        "2",
        "Bo.",
    )
    agent = build(database, client, candidate_renderings=3)

    result = agent.ask("Who is customer two?")

    assert "Candidate 1:" in client.prompts[3]
    assert "id = 2" in result.sql
    assert client.calls == 5


def test_repairs_do_not_fan_out(database):  # noqa: F811
    """Only the first attempt pays for three renderings."""
    client = ScriptedClient(
        "SELECT nope FROM customers",
        "SELECT nope FROM customers",
        "SELECT nope FROM customers",
        "SELECT count(*) FROM customers",
        "There are 2 customers.",
    )
    agent = build(database, client, candidate_renderings=3)

    result = agent.ask("How many customers?")

    assert result.ok
    assert client.calls == 5


def test_off_by_default_is_one_call(database):  # noqa: F811
    client = ScriptedClient("SELECT count(*) FROM customers", "There are 2 customers.")
    agent = build(database, client)

    agent.ask("How many customers?")

    assert client.calls == 2
