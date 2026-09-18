"""Prompt construction.

The prompts encode what the database has already told us -- real column
spellings, real enum values, real row counts -- because that is the difference
between a model guessing `status = 'completed'` and it knowing the value is
`DELIVERED`.
"""

from __future__ import annotations

GENERATE_SYSTEM = """You are a precise SQL analyst. You write one read-only \
{dialect} query that answers the user's question.

Rules:
- Output ONLY a SQL statement in a ```sql fenced block. No explanation.
- One statement. SELECT or WITH ... SELECT only. Never modify data.
- Use exactly the identifier spellings shown in the schema. This database uses \
camelCase column names, so they must be double-quoted: "createdAt", not created_at.
- Use only the literal values shown under VALUE HINTS or in a column's observed \
values. Do not invent status strings or category names.
- Prefer explicit joins using the conditions listed under JOINS.
- When aggregating money, check the CAUTION section: joining a larger child \
table multiplies rows and inflates SUM.
"""

ASSUMPTIONS_INSTRUCTION = """After the SQL block, add one line starting with \
ASSUMPTIONS: stating any business definition you chose (for example which \
status counts as delivered, or which date range 'last month' means). Keep it to \
one sentence."""

CLARIFY_INSTRUCTION = """If answering would require inventing a business \
definition that changes the result -- which of two measures to rank by, which \
of several dates defines a period, which of two similar tables is meant -- do \
not guess. Reply with exactly one line:

CLARIFY: <your single question>

Only do this when the choice genuinely changes the answer. If the schema or the \
metric definitions settle it, write the SQL."""


def history_section(history: list[dict] | None) -> str:
    """Earlier turns, so a follow-up has something to refer to.

    "Now show it by city" is unanswerable without the query it is amending, and
    a model asked to guess will invent columns. Only the question and the SQL
    are included -- results would dominate the prompt and add nothing.
    """
    if not history:
        return ""
    lines = []
    for turn in history[-3:]:
        question = (turn.get("question") or "").strip()
        sql = " ".join((turn.get("sql") or "").split())
        if not question:
            continue
        lines.append(f"  Q: {question}")
        if sql:
            lines.append(f"  SQL: {sql}")
    if not lines:
        return ""
    return (
        "EARLIER IN THIS CONVERSATION (a follow-up usually amends the last query)\n"
        + "\n".join(lines)
    )


def generate_prompt(
    question: str,
    schema_section: str,
    dialect: str,
    history: list[dict] | None = None,
    clarify: bool = False,
) -> list[tuple[str, str]]:
    earlier = history_section(history)
    context = f"{schema_section}\n\n{earlier}" if earlier else schema_section
    instructions = ASSUMPTIONS_INSTRUCTION
    if clarify:
        instructions = f"{CLARIFY_INSTRUCTION}\n\n{instructions}"
    return [
        ("system", GENERATE_SYSTEM.format(dialect=dialect)),
        ("human", f"{context}\n\nQUESTION: {question}\n\n{instructions}"),
    ]


def repair_prompt(
    question: str,
    schema_section: str,
    failed_sql: str,
    error_text: str,
    dialect: str,
    note: str = "",
) -> list[tuple[str, str]]:
    """Build the repair turn.

    The error goes last and unadorned. Buried above a schema dump it gets
    ignored, and the model returns the same query it just ran.
    """
    guidance = f"\n{note}\n" if note else ""
    return [
        ("system", GENERATE_SYSTEM.format(dialect=dialect)),
        (
            "human",
            f"{schema_section}\n\nQUESTION: {question}\n\n"
            f"Your previous query failed:\n\n```sql\n{failed_sql}\n```\n"
            f"{guidance}\n"
            f"The database reported:\n\n{error_text}\n\n"
            "Return a corrected query. Fix the specific problem reported above; "
            "do not repeat the same query.\n\n" + ASSUMPTIONS_INSTRUCTION,
        ),
    ]


NARRATE_SYSTEM = """You summarise query results for a business user. Two or \
three sentences, specific, no preamble.

Quote numbers exactly as they appear in the rows. Do NOT compute anything new -- \
no totals, differences, percentages or averages that are not already in the \
result. If a total was not returned, describe the rows without one. Arithmetic \
you perform is unverified and will contradict the table shown beside it.

Never guess a currency symbol: if a column holds money and the unit is not \
stated, write the bare number. If the result is empty or surprising, say so \
plainly rather than inventing an explanation."""


def narrate_prompt(
    question: str,
    sql: str,
    columns: list[str],
    rows: list,
    row_count: int,
    conventions: list[str] | None = None,
):
    preview = "\n".join(str(tuple(r)) for r in rows[:15])
    # Conventions carry the unit and currency. Without them the summary invents
    # a currency symbol, which is a small error that reads as a large one.
    rules = ""
    if conventions:
        rules = "REPORTING RULES\n" + "\n".join(f"  - {c}" for c in conventions) + "\n\n"
    return [
        ("system", NARRATE_SYSTEM),
        (
            "human",
            f"{rules}QUESTION: {question}\n\nSQL:\n{sql}\n\n"
            f"COLUMNS: {columns}\nROWS RETURNED: {row_count}\n"
            f"FIRST ROWS:\n{preview}",
        ),
    ]
