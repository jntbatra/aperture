"""Tests for conversation turns — the context that makes a follow-up answerable.

What these are really checking
------------------------------
That a question which is meaningless on its own ("and for April?") arrives at
the model accompanied by the turn that gives it meaning, and that a *first*
question is unaffected. The second half matters as much as the first: history
that leaks into a standalone question would change every benchmark number for a
feature that question does not use.
"""

from __future__ import annotations

from sqlagent.conversation import (
    DEFAULT_WINDOW,
    Turn,
    carryable_result,
    mentioned_tables,
    render_conversation,
)
from sqlagent.prompts import (
    build_answer_prompt,
    build_generation_prompt,
    build_repair_prompt,
)

# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_no_turns_renders_nothing():
    """A first question must produce the prompt it produced before this existed.

    Not "no previous questions" — an empty string. A header announcing the
    absence of history is noise, and any text at all would make first-question
    prompts differ from the ones the benchmark was measured on.
    """
    assert render_conversation([]) == ""


def test_a_turn_carries_its_question_and_sql():
    text = render_conversation([Turn("How many orders?", "SELECT count(*) FROM orders")])

    assert "How many orders?" in text
    assert "SELECT count(*) FROM orders" in text


def test_turns_are_rendered_oldest_first():
    text = render_conversation(
        [Turn("first", "SELECT 1"), Turn("second", "SELECT 2")]
    )

    assert text.index("first") < text.index("second")


def test_only_the_last_few_turns_are_kept():
    """History is windowed so it cannot crowd the schema out of the prompt."""
    turns = [Turn(f"question {i}", f"SELECT {i}") for i in range(DEFAULT_WINDOW + 3)]

    text = render_conversation(turns)

    assert "question 0" not in text
    assert f"question {DEFAULT_WINDOW + 2}" in text


def test_sql_is_flattened_to_one_line():
    """Multi-line SQL inside a history block obscures where turns end."""
    text = render_conversation([Turn("q", "SELECT a,\n  b\nFROM t")])

    assert "SELECT a, b FROM t" in text


def test_a_failed_turn_is_marked_as_failed():
    """A model shown a clean history repeats the mistake it cannot see."""
    text = render_conversation([Turn("q", "SELECT nope FROM t", ok=False)])

    assert "failed" in text.lower()


def test_a_turn_with_no_sql_still_appears():
    """The question was still asked; losing it would misalign the sequence."""
    text = render_conversation([Turn("something unanswerable", None, ok=False)])

    assert "something unanswerable" in text


# --------------------------------------------------------------------------
# Seeding retrieval from previous SQL
# --------------------------------------------------------------------------


def test_tables_are_recovered_from_previous_sql():
    """The mechanism behind "break that down by city", which names no table."""
    turns = [Turn("How many orders?", "SELECT count(*) FROM orders")]

    assert mentioned_tables(turns, {"orders", "customers"}) == ["orders"]


def test_a_column_that_contains_a_table_name_is_not_a_table():
    """Whole-word matching, so `orders_total` does not resolve to `orders`."""
    turns = [Turn("q", "SELECT orders_total FROM summary")]

    assert mentioned_tables(turns, {"orders"}) == []


def test_matching_ignores_case_because_postgres_folds_identifiers():
    turns = [Turn("q", "SELECT * FROM Orders")]

    assert mentioned_tables(turns, {"orders"}) == ["orders"]


def test_quoted_identifiers_are_matched():
    """Quotes are stripped by the tokeniser, so camelCase tables still match."""
    turns = [Turn("q", 'SELECT * FROM "orderItems"')]

    assert mentioned_tables(turns, {"orderItems"}) == ["orderItems"]


def test_tables_are_returned_in_order_of_first_appearance_without_duplicates():
    turns = [
        Turn("q1", "SELECT * FROM orders"),
        Turn("q2", "SELECT * FROM orders JOIN customers ON true"),
    ]

    assert mentioned_tables(turns, {"orders", "customers"}) == ["orders", "customers"]


def test_a_turn_without_sql_contributes_nothing():
    assert mentioned_tables([Turn("q", None, ok=False)], {"orders"}) == []


# --------------------------------------------------------------------------
# Where the history lands in each prompt
# --------------------------------------------------------------------------


def test_generation_prompt_without_history_is_unchanged():
    """The regression guard for every benchmark number already recorded."""
    with_empty = build_generation_prompt("q", "Tables:\n  t(a INT)", conversation="")
    without = build_generation_prompt("q", "Tables:\n  t(a INT)")

    assert with_empty == without


def test_generation_prompt_places_history_between_schema_and_question():
    """Schema is ground truth and comes first; the question is read against
    the history, so the history sits immediately before it."""
    prompt = build_generation_prompt(
        "and for April?",
        "Tables:\n  orders(id INT)",
        conversation=render_conversation([Turn("March?", "SELECT 1")]),
    )

    assert prompt.index("Tables:") < prompt.index("SELECT 1")
    assert prompt.index("SELECT 1") < prompt.index("Question: and for April?")


def test_repair_prompt_carries_history_too():
    """"Just the top 10" is no more self-contained on the second attempt."""
    prompt = build_repair_prompt(
        "just the top 10",
        "Tables:\n  orders(id INT)",
        "SELECT bad FROM orders",
        "column does not exist",
        conversation=render_conversation([Turn("biggest orders?", "SELECT 1")]),
    )

    assert "biggest orders?" in prompt
    assert "column does not exist" in prompt


def test_answer_prompt_carries_history_so_the_reply_reads_as_a_reply():
    prompt = build_answer_prompt(
        "and for April?",
        "SELECT count(*) FROM orders",
        "count\n412",
        conversation=render_conversation([Turn("March?", "SELECT 2")]),
    )

    assert "March?" in prompt
    assert prompt.index("March?") < prompt.index("Question: and for April?")


# --------------------------------------------------------------------------
# Carrying a small result forward
#
# The failure this fixes: asked "what is the name of the item with that ID",
# the model wrote `WHERE id = 'e5f6a7b8-...'` — a UUID it invented, because the
# previous turn's SQL contained no id (the id was in the row the query
# returned) and "that ID" had nothing to refer to.
# --------------------------------------------------------------------------


def test_a_small_result_is_carried():
    columns, rows = carryable_result(("id", "name"), [("abc-123", "Grilled Chicken")])

    assert columns == ("id", "name")
    assert rows == (("abc-123", "Grilled Chicken"),)


def test_a_carried_result_appears_in_the_prompt():
    """This is what gives "that ID" a referent."""
    turn = Turn(
        "which item is most ordered?",
        "SELECT i.id, i.name FROM items i ...",
        result_columns=("id", "name"),
        result_rows=(("a1b2c3d4-0000-4000", "Grilled Chicken Bowl"),),
    )

    text = render_conversation([turn])

    assert "a1b2c3d4-0000-4000" in text
    assert "Grilled Chicken Bowl" in text


def test_a_large_result_is_not_carried():
    """A hundred rows of customer data must not enter the prompt or the store."""
    columns, rows = carryable_result(("name",), [(f"customer {i}",) for i in range(100)])

    assert rows == ()


def test_a_wide_result_is_not_carried():
    columns, rows = carryable_result(tuple(f"c{i}" for i in range(20)), [tuple(range(20))])

    assert rows == ()


def test_carrying_is_all_or_nothing():
    """Carrying the first three rows of a hundred would let the model treat a
    truncated sample as the complete answer — worse than carrying nothing."""
    _, rows = carryable_result(("name",), [("a",), ("b",), ("c",), ("d",)])

    assert rows == ()


def test_an_uncarried_result_still_reports_its_size():
    """Otherwise the turn reads as "the query found nothing", which invites the
    model to conclude the data is missing and rewrite a working query."""
    text = render_conversation([Turn("list them", "SELECT ...", row_count=284)])

    assert "284 rows" in text


def test_an_empty_result_is_not_carried():
    assert carryable_result(("name",), []) == ((), ())


def test_a_long_cell_is_truncated():
    turn = Turn("q", "SELECT ...", result_columns=("note",), result_rows=(("x" * 500,),))

    assert len(turn.render()) < 400


def test_null_cells_render_readably():
    turn = Turn("q", "SELECT ...", result_columns=("name",), result_rows=((None,),))

    assert "NULL" in turn.render()


def test_the_model_is_told_to_copy_ids_rather_than_write_them():
    text = render_conversation([Turn("q", "SELECT 1")])

    assert "Never write an id that does not appear there" in text


def test_the_model_is_told_to_replace_contradicted_constraints():
    """"I told you it should be veg" returned zero rows, because the veg filter
    was added to a query still pinned to one specific item id."""
    text = render_conversation([Turn("q", "SELECT 1")])

    assert "REPLACE" in text
