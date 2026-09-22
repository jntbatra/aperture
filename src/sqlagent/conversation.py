"""Conversation turns — what makes a follow-up question answerable.

The problem
-----------
Until now every question was answered in isolation. That works for
"How many orders were placed in March?" and fails completely for what a person
naturally says next:

    "and for April?"
    "break that down by city"
    "just the top 10"

None of those contain a table name, a metric, or a filter. On their own they are
not questions about a database at all. They only mean something *relative to the
previous turn*, which is exactly what a stateless agent does not have.

What gets carried forward, and what does not
--------------------------------------------
A turn keeps the **question**, the **SQL**, and — only when it is very small —
the **result**. Deliberately not the answer prose.

* The SQL is the precise, compact record of what was measured. "and for April?"
  becomes answerable the moment the model can see
  ``SELECT count(*) FROM orders WHERE date >= '2024-03-01' ...`` — it needs to
  change one literal.
* The answer prose says the same thing less precisely, in more tokens.
* Large results are excluded. They could be thousands of rows of personal data,
  and they would crowd the schema out of the prompt. The schema is what makes
  the next query correct.

Why small results *are* carried
-------------------------------
Because a follow-up frequently refers to a value that only ever existed in a
result cell. Observed on a real database:

    Q: I need an item that has been repeatedly ordered
    A: ... "Grilled Chicken Bowl (250gm)", ID a1b2c3d4-0000-4000-...
    Q: what is the name of the item with that ID
    -> SELECT name FROM items WHERE id = 'e5f6a7b8-1111-4111-8111-111111111111'

The model **invented a UUID**. It had to: the previous turn's SQL contains no
id — the id was in the row the query returned — so "that ID" had no referent in
anything the model could see, and a plausible-looking UUID is what filled the
gap. The resulting query then executed perfectly and returned a real row for
the wrong item, which is the hardest kind of wrong answer to notice.

The bound is deliberately tight: a result is carried only if it fits in a few
cells. ``RESULT_MAX_ROWS`` × ``RESULT_MAX_COLUMNS`` is the size of a lookup or a
small comparison — exactly the results that follow-ups point at — and far below
anything that would amount to copying data out of the database. "List every
customer" carries nothing; the turn says the result was large instead.

Why a bounded window
--------------------
Only the last few turns are rendered. A conversation is not a transcript to be
preserved in full: turn 30 rarely constrains turn 31, and carrying everything
means the prompt grows without limit until the schema — the part that actually
matters — is squeezed out. Old turns remain in the store and are still shown in
the UI; they just stop being fed to the model.

Failed turns are carried too, marked as failed. A model that can see its last
query errored will not reproduce it; a model shown a clean history repeats the
same mistake, because as far as it can tell nothing went wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENTIFIER_SPLIT = re.compile(r"[^A-Za-z0-9_]+")
"""Splits SQL into identifier-shaped tokens.

Quoted identifiers lose their quotes, which is intended: the comparison below is
against bare table names.
"""

DEFAULT_WINDOW = 4
"""Turns of context passed to the model.

Four covers the realistic depth of a drill-down — ask, narrow, group, narrow
again — at a cost of a few hundred tokens. Deeper chains almost always restate
the subject anyway ("okay, now for refunds"), which re-anchors the context
without needing history.
"""


RESULT_MAX_ROWS = 3
RESULT_MAX_COLUMNS = 5
RESULT_MAX_CELL_CHARS = 120
"""Bounds on the result carried into the next turn.

Small enough to be a lookup or a short comparison — the results a follow-up
actually refers back to — and far too small to amount to extracting data. A
query returning more than this carries nothing.
"""


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange, reduced to what the next turn needs to know."""

    question: str
    sql: str | None
    ok: bool = True

    result_columns: tuple[str, ...] = ()
    result_rows: tuple[tuple[object, ...], ...] = ()
    """The result, if it was small enough to carry. See the module docstring:
    this is what gives "that ID" something to refer to."""

    row_count: int | None = None
    """Rows the query actually returned, whether or not they were carried.

    Kept even when the rows are not, so a turn can say "returned 284 rows"
    rather than silently looking like it returned nothing.
    """

    clarification: str | None = None
    """The clarifying question put back to the user, if this turn asked one.

    Without this a clarification is indistinguishable from a failure: it has no
    SQL, so the history rendered it as "that question could not be answered",
    which is the opposite of what happened. The question was fine — it was
    *waiting for a reply*, and the very next message is that reply.

    Observed: asked to clarify "best customers", the user answered "Yes". With
    the turn rendered as a failure there was nothing to attach "Yes" to, no
    table name anywhere in context, and the run died at seed selection with "I
    could not match that question to any table".
    """

    def render(self) -> str:
        """One turn as prompt text."""
        lines = [f"Q: {self.question}"]

        if self.clarification:
            # Not a failure — a turn that asked something and is owed an answer.
            # Spelled out explicitly, because the next message is almost never a
            # self-contained question: it is "by revenue", or "the second one",
            # or "yes".
            lines.append(f"I asked back: {self.clarification}")
            lines.append(
                "The next question is the user's reply to that. Combine it with "
                "the question above and answer the combined question."
            )
            return "\n".join(lines)

        if self.sql:
            # The SQL is flattened to a single line. Its internal formatting
            # carries no meaning here, and multi-line blocks inside a history
            # section make it harder for the model to see where one turn ends
            # and the next begins.
            flat = " ".join(self.sql.split())
            lines.append(f"{'SQL' if self.ok else 'SQL (failed)'}: {flat}")
        elif not self.ok:
            lines.append("SQL: (none - that question could not be answered)")

        if self.result_rows:
            header = " | ".join(self.result_columns)
            body = " ; ".join(
                " | ".join(_cell(value) for value in row) for row in self.result_rows
            )
            lines.append(f"Result: {header} -> {body}")
        elif self.row_count:
            # The values are not carried, but their absence must not read as
            # "the query found nothing" — that would invite the model to
            # conclude the data is missing and rewrite a working query.
            lines.append(f"Result: {self.row_count} rows (values not carried forward)")

        return "\n".join(lines)


def _cell(value: object) -> str:
    text = "NULL" if value is None else str(value)
    if len(text) > RESULT_MAX_CELL_CHARS:
        return text[: RESULT_MAX_CELL_CHARS - 1] + "…"
    return text


def carryable_result(
    columns: tuple[str, ...] | list[str], rows: tuple | list
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    """Reduce a result to what may be carried forward, or nothing.

    All-or-nothing by design. Carrying the first three rows of a hundred would
    let the model treat a truncated sample as the complete answer, which is a
    worse failure than carrying nothing at all — the turn already says how many
    rows there were.
    """
    if not rows or len(rows) > RESULT_MAX_ROWS or len(columns) > RESULT_MAX_COLUMNS:
        return (), ()
    return tuple(columns), tuple(tuple(row) for row in rows)


def evicted_turns(
    turns: list[Turn] | tuple[Turn, ...], *, window: int = DEFAULT_WINDOW
) -> list[Turn]:
    """Turns that fall outside the window, oldest first.

    The complement of what :func:`render_conversation` shows in full. Split out
    so the summariser and the renderer cannot disagree about where the boundary
    is — a turn appearing in both the summary and the window is duplicated
    context, and a turn in neither is silently forgotten.
    """
    if window <= 0:
        return list(turns)
    return list(turns)[:-window] if len(turns) > window else []


def render_conversation(
    turns: list[Turn] | tuple[Turn, ...],
    *,
    window: int = DEFAULT_WINDOW,
    summary: str = "",
) -> str:
    """Render recent turns as a prompt section, or an empty string if there are none.

    Empty rather than "no previous questions": a header announcing the absence
    of history is pure noise, and a first question should produce exactly the
    prompt it produced before this feature existed. That keeps the benchmark
    numbers comparable.

    ``summary`` is the standing context distilled from turns that fell out of
    the window (see :mod:`sqlagent.summarise`). It is rendered *above* the
    turns, and labelled as still-in-force rather than as history, because that
    is the whole point of keeping it: "delivered orders only" said nine turns
    ago still constrains this question.
    """
    recent = list(turns)[-window:] if window > 0 else []
    summary = summary.strip()
    if not recent and not summary:
        return ""

    sections: list[str] = []

    if summary:
        sections.append(
            "Standing context from earlier in this conversation. These are "
            "constraints and subjects the user established and has not "
            "withdrawn; they still apply to the new question unless it "
            "contradicts them.\n\n" + summary
        )

    if recent:
        sections.append(
            "Earlier in this conversation (most recent last). The new question "
            "may refer back to these — resolve pronouns, 'that', and implied "
            "filters against them.\n\n"
            + "\n\n".join(turn.render() for turn in recent)
        )

    sections.append(CARRY_FORWARD_RULES)
    return "\n\n".join(sections)


CARRY_FORWARD_RULES = """\
How to use the history above:
- Carry forward the subject and the filters that are still wanted.
- If the new question corrects or contradicts an earlier constraint, REPLACE \
that constraint. Do not add the new one on top of the old one. "It should be \
vegetarian" after a query about one specific item means "search the vegetarian \
items", not "check whether that one item is vegetarian".
- Never carry forward a specific id or literal value from an earlier query \
unless the new question actually refers to that row. An id was an answer to the \
previous question, not a filter for this one.
- When the question does refer to an earlier value ("that ID", "that customer"), \
use the value shown on the Result line above, copied exactly. Never write an id \
that does not appear there.
- If the new question narrows a previous analysis ("only the delivered ones"), \
keep what was being measured and add the restriction. Do not replace the \
analysis with a bare count."""
"""Rules appended after the turns.

Every line here corresponds to an observed failure on a real database:

* A user who said "I told you it should be veg" got zero rows, because the model
  added a vegetarian filter to a query that was still pinned to one specific
  item id from two turns earlier. The correction was treated as an addition.
* "For the orders that were actually delivered", asked during a revenue
  analysis, produced a bare count of delivered orders — the analysis was
  dropped and only the new restriction survived.

The two failures are opposites, which is why both rules are needed: the model
has no default sense of which parts of a previous turn are the *subject* and
which were incidental, and left to itself it guesses inconsistently.
"""


def mentioned_tables(turns: list[Turn] | tuple[Turn, ...], known: set[str]) -> list[str]:
    """Tables named in recent SQL, for seeding retrieval on a follow-up.

    "break that down by city" contains no table name, so seed selection has
    nothing to match on and the graph walk starts from nowhere. The previous
    turn's SQL names the tables literally, which makes it a better starting
    point than anything derivable from the question text.

    Matching is done on whole words against the set of tables that actually
    exist, so a column called ``orders_total`` cannot be mistaken for the
    ``orders`` table, and a literal string in a WHERE clause cannot invent one.
    """
    # Case-insensitive, because PostgreSQL folds unquoted identifiers: a query
    # written `FROM Orders` refers to the table stored as `orders`.
    by_lower = {name.lower(): name for name in known}

    found: list[str] = []
    for turn in turns:
        if not turn.sql:
            continue
        for token in _IDENTIFIER_SPLIT.split(turn.sql):
            name = by_lower.get(token.lower())
            if name is not None and name not in found:
                found.append(name)
    return found
