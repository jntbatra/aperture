"""Check that an answer says what the results actually say.

Why this exists
---------------
The validator proves a query is safe and the database proves it runs. Neither
says anything about the *sentence* built from the rows, and that sentence is the
only part most users read. Two failures observed on a real database, both from
queries that executed perfectly:

1. The query returned one row — the item name ``Grilled Chicken Bowl
   (250gm)`` — and the answer said the item was ``Chicken Noodle Bowl with
   Choice of Sides``. A name that appears nowhere in the result, stated as
   fact.
2. A query with no vegetarian filter returned a mixed list, and the answer
   described it as "the vegetarian items", adding a qualifier the SQL never
   applied.

Both are worse than an error. An error is visibly an error; a confident wrong
answer gets acted on.

What is checked, and what deliberately is not
----------------------------------------------
Two mechanical checks, both conservative — a false alarm costs a wasted model
call, so the bar is "this cannot be a legitimate paraphrase":

* **Invented figures.** A number of three or more digits that appears in the
  answer but in neither the results nor the question. Small numbers are skipped
  because they are ranks and list positions ("the top 5"), and years are
  skipped because they are frequently restated from a date the results contain
  in another format.
* **Dropped values.** When the result is small enough that the answer should
  quote it outright — a handful of cells — every text cell must appear. This is
  what catches a substituted name: the row said one thing and the sentence said
  another.

Not checked: whether a qualifier like "vegetarian" is justified by the WHERE
clause. That needs to understand the SQL's semantics, not just its text, and a
regex approximation would fire on every legitimate summary. It is handled in the
prompt instead — see ``ANSWER_SYSTEM_PROMPT``.
"""

from __future__ import annotations

import re

from sqlagent.db.execute import QueryResult

MIN_DIGITS = 3
"""Below this, a number in an answer is a rank or a count of listed items."""

SMALL_RESULT_CELLS = 6
"""At most this many cells for the "every value must be quoted" rule.

A single-row lookup or a two-by-two comparison should be reproduced exactly.
Beyond that an answer is legitimately a summary, and demanding every cell would
flag every correct summary of a twenty-row table.
"""

MIN_TEXT_LENGTH = 4
"""Shorter cells ('N/A', 'INR', a one-letter code) match too easily by accident
to be worth checking."""

_NUMERIC_TEXT = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
"""A cell that is a number, whatever Python type it arrived as.

PostgreSQL serialises ``NUMERIC`` as a *string*, so a revenue total reaches this
module as ``'237220.180000000000'``. Checking the Python type alone therefore
subjects it to the "quote every text cell verbatim" rule, and a correct answer
saying "Rs 237220.18" gets flagged for not reproducing twelve trailing zeroes.

Figures are the figure check's job, and that one understands formatting and
precision. This is how they are handed over to it.
"""

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digits(text: str) -> str:
    """Strip formatting so 1,234,500 and 1234500 compare equal."""
    return re.sub(r"[,\s]", "", text)


def _bare(text: str) -> str:
    """Digits only, for comparing magnitudes across differing precision.

    ``237220.18`` and ``237220.180000000000`` are the same figure written by a
    person and by PostgreSQL's NUMERIC. Prefix-matching on digits alone relates
    them; prefix-matching with the decimal point left in on one side and
    stripped from the other relates nothing, which is what made the guard flag a
    correct answer as unsupported.
    """
    return re.sub(r"[^\d]", "", text)


def _as_float(text: str) -> float | None:
    try:
        return float(_digits(text))
    except ValueError:  # pragma: no cover - the regex only matches numerals
        return None


def _decimals(text: str) -> int:
    _, _, fraction = text.partition(".")
    return len(fraction)


def _numbers_in(text: str) -> set[str]:
    return {_digits(match.group()) for match in _NUMBER.finditer(text)}


def invented_figures(answer: str, result: QueryResult, question: str) -> list[str]:
    """Figures stated in the answer that are in neither the results nor the question.

    Rounded and derived numbers are the main source of false positives: an
    answer may legitimately say "about 2.1 thousand" for 2147, or state a
    percentage it computed. Those are accepted — this only reports a figure
    with no relationship to anything available, which is the shape of a
    fabrication rather than a paraphrase.
    """
    available = _numbers_in(question)
    for row in result.rows:
        for cell in row:
            available |= _numbers_in(str(cell))

    # Kept in three forms because three different comparisons are needed:
    # exact text, numeric value, and digits-only for prefix matching.
    available_floats = [
        value for value in (_as_float(v) for v in available) if value is not None
    ]
    available_bare = {_bare(value) for value in available if _bare(value)}

    invented = []
    for candidate in _numbers_in(answer):
        bare = _bare(candidate)
        if len(bare) < MIN_DIGITS:
            continue
        # Years restate a date the result holds in another format.
        if candidate.isdigit() and 1900 <= int(candidate) <= 2100:
            continue
        if candidate in available:
            continue

        # Equal once both are read as numbers and rounded to the precision the
        # answer actually quoted. This is what relates "237220.18" to the
        # NUMERIC column's "237220.180000000000".
        value = _as_float(candidate)
        if value is not None:
            places = _decimals(candidate)
            if any(round(other, places) == round(value, places) for other in available_floats):
                continue

        # Digits-only prefix, which accepts rounding: "12,340" against 12345,
        # or "214 dozen" against 2147.
        if any(
            other.startswith(bare) or bare.startswith(other)
            for other in available_bare
        ):
            continue

        invented.append(candidate)

    return sorted(invented)


def dropped_values(answer: str, result: QueryResult) -> list[str]:
    """Text values from a small result that the answer failed to quote.

    Only applies to results small enough that quoting them all is the expected
    behaviour. This is the check that catches a substituted name — the case
    where the query was right, the row was right, and the sentence named
    something else entirely.
    """
    cells = [cell for row in result.rows for cell in row]
    if not cells or len(cells) > SMALL_RESULT_CELLS:
        return []

    folded = answer.casefold()
    missing = []
    for cell in cells:
        if not isinstance(cell, str) or len(cell.strip()) < MIN_TEXT_LENGTH:
            continue
        # Numbers belong to `invented_figures`, which compares them as numbers.
        # Demanding one verbatim would require an answer to write a NUMERIC's
        # full stored precision.
        if _NUMERIC_TEXT.match(cell.strip()):
            continue
        if cell.casefold() not in folded:
            missing.append(cell)
    return missing


def check(answer: str, result: QueryResult, question: str) -> str | None:
    """Return a correction instruction, or ``None`` if the answer is supported.

    The return value is written as an instruction rather than an error because
    its only consumer feeds it straight back to the model. Naming the specific
    offending value matters: told "that is wrong" a model rewrites the sentence
    and keeps the invention, told "you wrote X, the results contain Y" it fixes
    the value.
    """
    missing = dropped_values(answer, result)
    if missing:
        return (
            "The answer did not use the values the query returned. "
            f"The results contain: {', '.join(repr(value) for value in missing)}. "
            "Rewrite the answer using exactly those values. Do not substitute a "
            "different name."
        )

    invented = invented_figures(answer, result, question)
    if invented:
        return (
            f"The answer states figures that are not in the results: "
            f"{', '.join(invented)}. Every number must come from the result rows. "
            "Rewrite the answer using only figures that appear there."
        )

    return None
