"""Tests for the glossary — domain facts the schema cannot carry.

The measured failure behind this: ``order_items.price`` stores paise, nothing in
the schema says so, and revenue was reported 100× too large.
"""

from __future__ import annotations

import json

from sqlagent.glossary import ColumnNote, Glossary, Metric, Term
from sqlagent.prompts import build_answer_prompt, build_generation_prompt

# --------------------------------------------------------------------------
# An absent glossary must cost nothing
# --------------------------------------------------------------------------


def test_an_empty_glossary_renders_to_nothing():
    """Not "no domain notes" — an empty string.

    Any text at all would make prompts for an undeclared database differ from
    the ones every benchmark number was measured on.
    """
    assert Glossary().render() == ""


def test_an_empty_glossary_is_falsy():
    assert not Glossary()
    assert Glossary(terms=(Term("veg", "isVegetarian = TRUE"),))


def test_a_prompt_without_a_glossary_is_unchanged():
    with_empty = build_generation_prompt("q", "Tables:\n  t(a INT)", glossary="")
    without = build_generation_prompt("q", "Tables:\n  t(a INT)")

    assert with_empty == without


def test_a_missing_file_loads_as_empty():
    """Most databases have no glossary; that is not an error."""
    assert Glossary.load("/nonexistent/glossary.json").render() == ""


def test_no_path_loads_as_empty():
    assert Glossary.load("").render() == ""
    assert Glossary.load(None).render() == ""


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_a_unit_note_reaches_the_prompt():
    """The entry that fixes the 100× revenue error."""
    glossary = Glossary(
        columns=(ColumnNote("order_items", "price", "stored in paise; divide by 100"),)
    )

    text = glossary.render()

    assert "order_items.price" in text
    assert "paise" in text


def test_a_term_disambiguates_two_plausible_tables():
    glossary = Glossary(terms=(Term("ordered", "order_items, never cart_items"),))

    assert '"ordered" means order_items, never cart_items' in glossary.render()


def test_a_metric_carries_its_expression():
    glossary = Glossary(
        metrics=(Metric("revenue", "SUM(price * quantity) / 100.0", "in rupees"),)
    )

    text = glossary.render()

    assert "revenue = SUM(price * quantity) / 100.0" in text
    assert "in rupees" in text


def test_a_metric_without_a_note_renders_cleanly():
    glossary = Glossary(metrics=(Metric("order count", "COUNT(DISTINCT orders.id)"),))

    assert glossary.render().endswith("COUNT(DISTINCT orders.id)")


def test_the_generation_prompt_places_the_glossary_after_the_schema():
    """Schema says what exists, glossary says what it means, then the question."""
    prompt = build_generation_prompt(
        "total revenue?",
        "Tables:\n  order_items(price INT)",
        glossary=Glossary(
            columns=(ColumnNote("order_items", "price", "paise"),)
        ).render(),
    )

    assert prompt.index("Tables:") < prompt.index("paise")
    assert prompt.index("paise") < prompt.index("Question: total revenue?")


def test_the_answer_prompt_gets_the_glossary_too():
    """A correctly converted figure can still be reported in the wrong currency."""
    prompt = build_answer_prompt(
        "revenue?",
        "SELECT sum(price) FROM order_items",
        "sum\n1234500",
        glossary=Glossary(
            columns=(ColumnNote("order_items", "price", "paise, report in rupees"),)
        ).render(),
    )

    assert "rupees" in prompt


# --------------------------------------------------------------------------
# Narrowing to the tables in context
# --------------------------------------------------------------------------


def test_column_notes_are_narrowed_to_the_tables_in_the_prompt():
    """A fifty-column glossary on a two-table question is noise competing with
    the schema for the model's attention."""
    glossary = Glossary(
        columns=(
            ColumnNote("orders", "totalAmount", "paise"),
            ColumnNote("riders", "salary", "paise"),
        )
    )

    narrowed = glossary.for_tables({"orders"})

    assert [note.qualified for note in narrowed.columns] == ["orders.totalAmount"]


def test_terms_and_metrics_survive_narrowing():
    """They are few, and a metric naming a table not in context is itself a
    signal that retrieval may have missed something."""
    glossary = Glossary(
        terms=(Term("ordered", "order_items"),),
        metrics=(Metric("revenue", "SUM(price)"),),
    )

    narrowed = glossary.for_tables({"unrelated"})

    assert narrowed.terms and narrowed.metrics


def test_narrowing_to_nothing_keeps_everything():
    """An empty table set means "no context to narrow against", not "drop it all"."""
    glossary = Glossary(columns=(ColumnNote("orders", "totalAmount", "paise"),))

    assert glossary.for_tables(set()).columns


# --------------------------------------------------------------------------
# Loading, leniently
# --------------------------------------------------------------------------


def test_a_file_round_trips(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps(
            {
                "columns": [{"column": "orders.totalAmount", "note": "paise"}],
                "terms": [{"word": "veg", "meaning": "isVegetarian = TRUE"}],
                "metrics": [{"name": "revenue", "expression": "SUM(x)"}],
            }
        )
    )

    glossary = Glossary.load(path)

    assert glossary.columns[0].table == "orders"
    assert glossary.columns[0].column == "totalAmount"
    assert glossary.terms[0].word == "veg"
    assert glossary.metrics[0].name == "revenue"


def test_a_malformed_entry_is_skipped_not_fatal(tmp_path):
    """A typo in one entry must not take down the agent. The alternative is a
    server that refuses to start because someone misspelled a key."""
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps(
            {
                "columns": [
                    {"column": "no_dot_here", "note": "bad"},
                    {"column": "orders.totalAmount", "note": "good"},
                ]
            }
        )
    )

    glossary = Glossary.load(path)

    assert [note.note for note in glossary.columns] == ["good"]


def test_invalid_json_loads_as_empty(tmp_path):
    path = tmp_path / "g.json"
    path.write_text("{not json")

    assert Glossary.load(path).render() == ""


def test_unknown_keys_are_ignored(tmp_path):
    """The shipped glossary carries a `_comment` block explaining itself."""
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"_comment": ["why this file exists"], "terms": []}))

    assert Glossary.load(path).render() == ""


# --------------------------------------------------------------------------
# The shipped example glossary
# --------------------------------------------------------------------------


def test_the_example_glossary_parses_and_declares_the_paise_columns():
    """Every claim in this file was verified against the live database."""
    glossary = Glossary.load("glossaries/example.json")

    declared = {note.qualified for note in glossary.columns}
    assert "order_items.price" in declared
    assert "orders.totalAmount" in declared
    assert "paise" in glossary.render()
    # The other confirmed failure: a follow-up silently switched from
    # order_items to cart_items.
    assert "cart_items" in glossary.render()


def test_a_configured_glossary_that_is_missing_is_an_error_not_a_whisper(tmp_path, caplog):
    """A renamed file once dropped the paise note without a trace in the logs."""
    import logging

    with caplog.at_level(logging.ERROR, logger="sqlagent.glossary"):
        glossary = Glossary.load(tmp_path / "renamed.json")

    assert glossary == Glossary()
    assert any("does not exist" in r.message for r in caplog.records)
