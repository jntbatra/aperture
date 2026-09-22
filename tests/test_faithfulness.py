"""Tests for the answer-faithfulness guard.

Every case here is drawn from a real failure or from the false-positive risk
that failure's fix introduces. The second half matters as much as the first:
a check that flags correct answers costs a model call on every question and
teaches everyone to ignore it.
"""

from __future__ import annotations

from sqlagent.db.execute import QueryResult
from sqlagent.guards.faithfulness import check, dropped_values, invented_figures


def rows(columns: tuple[str, ...], *data) -> QueryResult:
    return QueryResult(columns=columns, rows=tuple(data), seconds=0.01, truncated=False)


# --------------------------------------------------------------------------
# The failure this was built for
# --------------------------------------------------------------------------


def test_a_substituted_name_is_caught():
    """Observed on a real database.

    The query returned one row — the item's actual name — and the answer named
    a different dish entirely. The query was correct, the row was correct, and
    the sentence the user read was false.
    """
    result = rows(("name",), ("Grilled Chicken Bowl (250gm)",))

    problem = check(
        "The name of the item with that ID is Chicken Noodle Bowl with "
        "Choice of Sides.",
        result,
        "what is the name of the item with that ID",
    )

    assert problem is not None
    assert "Grilled Chicken Bowl (250gm)" in problem


def test_the_correction_names_the_value_that_should_have_been_used():
    """Told only "that is wrong", a model rewrites the sentence and keeps the
    invention. Told which value to use, it uses it."""
    result = rows(("name",), ("Veg Spring Rolls - 6 pcs",))

    problem = check("The item is Paneer Roll.", result, "which item?")

    assert problem is not None
    assert "Veg Spring Rolls - 6 pcs" in problem
    assert "Do not substitute" in problem


def test_quoting_the_returned_value_passes():
    result = rows(("name",), ("Grilled Chicken Bowl (250gm)",))

    assert check(
        "That item is Grilled Chicken Bowl (250gm).", result, "which item?"
    ) is None


def test_matching_ignores_case():
    result = rows(("name",), ("Veg Spring Rolls",))

    assert check("The item is veg spring rolls.", result, "which?") is None


# --------------------------------------------------------------------------
# Invented figures
# --------------------------------------------------------------------------


def test_a_figure_that_appears_nowhere_is_caught():
    result = rows(("total",), (2147,))

    assert invented_figures("There were 8,421 orders.", result, "how many orders?")


def test_a_figure_from_the_results_is_accepted():
    result = rows(("total",), (2147,))

    assert invented_figures("There were 2,147 orders.", result, "how many?") == []


def test_thousands_separators_do_not_count_as_a_difference():
    """1,234,500 in prose and 1234500 in a cell are the same number."""
    result = rows(("revenue",), (1234500,))

    assert invented_figures("Revenue was 1,234,500.", result, "revenue?") == []


def test_a_figure_from_the_question_is_accepted():
    """Restating the user's own threshold is not an invention."""
    result = rows(("total",), (12,))

    assert invented_figures(
        "12 items were ordered more than 100 times.", result, "ordered over 100 times?"
    ) == []


def test_small_numbers_are_ignored():
    """Ranks and list positions — "the top 5" — are not claims about the data."""
    result = rows(("name",), ("a",), ("b",), ("c",))

    assert invented_figures("Here are the top 3 of 5 results.", result, "top?") == []


def test_years_are_ignored():
    """A result holds a date; the answer restates the year in another format."""
    result = rows(("month",), ("2026-08-01",))

    assert invented_figures("The peak was in August 2026.", result, "when?") == []


def test_a_rounded_figure_is_accepted():
    """"About 2,150" for 2147 is a paraphrase, not a fabrication."""
    result = rows(("total",), (2147,))

    assert invented_figures("Roughly 214 dozen orders.", result, "how many?") == []


# --------------------------------------------------------------------------
# Not flagging legitimate summaries
# --------------------------------------------------------------------------


def test_a_large_result_is_not_required_to_be_quoted_in_full():
    """Past a few cells an answer is legitimately a summary. Demanding every
    value would flag every correct summary of a twenty-row table."""
    result = rows(
        ("name", "n"),
        *[(f"item {i}", i) for i in range(20)],
    )

    assert dropped_values("The top item is item 19.", result) == []


def test_short_cells_are_not_checked():
    """'INR', 'N/A' and one-letter codes match by accident too easily."""
    result = rows(("code",), ("INR",))

    assert dropped_values("The amount is in rupees.", result) == []


def test_an_empty_result_is_not_flagged():
    """Empty results are answered without a model call, but the guard must not
    trip if one arrives here anyway."""
    result = rows(("name",))

    assert check("That query returned no rows.", result, "which item?") is None


def test_a_non_text_cell_is_not_required_verbatim():
    """Numbers are covered by the figure check, which understands formatting;
    requiring the literal string would reject '2,147' for 2147."""
    result = rows(("total",), (2147,))

    assert dropped_values("There were 2,147 orders.", result) == []


# --------------------------------------------------------------------------
# PostgreSQL NUMERIC — a correct answer flagged as unsupported
#
# Reported from the running app: "Rs 237220.18" against a row holding
# 237220.180000000000. The figure was exactly right and the guard cried wolf,
# which is worse than no guard — a warning that fires on correct answers trains
# people to ignore it.
# --------------------------------------------------------------------------


def test_a_numeric_column_does_not_flag_a_correctly_rounded_answer():
    result = rows(("revenue",), ("237220.180000000000",))

    assert check(
        "The revenue from orders placed in the last 30 days was Rs 237220.18.",
        result,
        "what was revenue in the last 30 days?",
    ) is None


def test_numeric_text_is_not_required_verbatim():
    """PostgreSQL serialises NUMERIC as a *string*, so a type check alone
    subjects it to the "quote every text cell" rule and demands an answer
    reproduce twelve trailing zeroes."""
    result = rows(("revenue",), ("237220.180000000000",))

    assert dropped_values("Revenue was Rs 237220.18.", result) == []


def test_precision_differences_do_not_matter_either_way():
    result = rows(("total",), ("1234.5000",))

    assert invented_figures("The total is 1234.50.", result, "total?") == []
    assert invented_figures("The total is 1234.5.", result, "total?") == []


def test_a_thousands_separated_answer_matches_a_numeric_cell():
    result = rows(("revenue",), ("237220.180000000000",))

    assert invented_figures("Revenue was Rs 237,220.18.", result, "revenue?") == []


def test_a_genuinely_invented_figure_still_fails_against_a_numeric_cell():
    """The fix must not be a blanket amnesty for anything decimal-shaped."""
    result = rows(("revenue",), ("237220.180000000000",))

    assert invented_figures("Revenue was Rs 8,421.00.", result, "revenue?")


def test_a_negative_numeric_cell_is_treated_as_a_number():
    result = rows(("delta",), ("-4512.7500",))

    assert dropped_values("The change was -4512.75.", result) == []
