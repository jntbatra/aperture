"""Prompt construction.

Prompts are source code
-----------------------
They live here, in version control, not inline in the call sites and not in a
database. A prompt change is a behaviour change: it should show up in a diff,
be reviewable, and be measurable by re-running the benchmark. Burying prompt
text inside a function that also does HTTP makes all three impossible.

What goes into the SQL-generation prompt, and why
-------------------------------------------------
1. **The dialect**, because ``EXTRACT(YEAR FROM d)`` and ``strftime('%Y', d)``
   are both correct, just not on the same database.
2. **Only the relevant tables**, with columns and types. Not the whole schema.
3. **Explicit join predicates**, taken from real foreign-key constraints. This
   is the single highest-value thing in the prompt: it removes the need for the
   model to guess which columns link two tables, which is where naive
   text-to-SQL systems most often go wrong.
4. **Sample rows**, so value formats are known rather than assumed.
5. **Rules**, phrased as constraints the validator will independently enforce.
   Telling the model "SELECT only" does not make it true — the validator makes
   it true — but a model told the rule breaks it far less often, which saves a
   repair round trip.
"""

from __future__ import annotations

import re

import networkx as nx

from sqlagent.db.profile import TableProfile
from sqlagent.db.sample import TableSample
from sqlagent.schema.graph import describe_edges
from sqlagent.schema.introspect import SchemaSnapshot

SQL_SYSTEM_PROMPT = """\
You are a precise SQL analyst. You translate questions into a single SQL SELECT \
statement for the stated dialect.

Rules:
- Output exactly one SELECT statement. No prose, no explanation, no markdown fences.
- Never write INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, GRANT or TRUNCATE.
- Use only the tables and columns given. Never invent a table or column name.
- Join tables only on the relationships listed. Do not guess a join condition.
- Prefer explicit column lists over SELECT *.
- When a question is about "how many", return a count rather than raw rows.
- If the question asks to list or name things and your query joins a table that
  can match several rows per result, add DISTINCT. Asking for "the elements of
  molecule X" wants each element once, not once per bond it appears in.
- Do not add DISTINCT when the query already groups, or when duplicates are
  part of the answer (counting occurrences, listing per-row detail).
"""


# A bare identifier PostgreSQL will not fold: lowercase letters, digits and
# underscores, not starting with a digit.
SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote_identifier(name: str, *, dialect: str = "postgres") -> str:
    """Quote an identifier if writing it bare would change what it refers to.

    PostgreSQL folds unquoted identifiers to lower case, so a Prisma-generated
    column called ``itemId`` must be written ``"itemId"``. Written bare it
    becomes ``itemid``, which does not exist — and the failure arrives at
    execution time, not generation time.

    This is not an edge case. Prisma, TypeORM and Rails all produce camelCase
    columns by default, so an entire class of real application database is
    unusable without it. Measured on a 56-table Prisma schema, it was the
    single largest cause of failure.

    MySQL uses backticks and is case-sensitive only on some platforms; SQLite
    accepts double quotes and folds nothing.
    """
    if SAFE_IDENTIFIER.match(name):
        return name
    if dialect.lower().startswith("mysql"):
        return f"`{name}`"
    return '"' + name.replace('"', '""') + '"'


def render_schema(
    snapshot: SchemaSnapshot,
    graph: nx.DiGraph,
    tables: list[str],
    *,
    samples: list[TableSample] | None = None,
    profiles: list[TableProfile] | None = None,
    dialect: str = "postgres",
) -> str:
    """Describe a subset of the schema in a compact, model-readable form.

    Args:
        snapshot: Full schema, used to look up column detail.
        graph: Schema graph, used to find the joins between the chosen tables.
        tables: The tables to describe, already ordered by relevance (nearest
            first). Order is preserved because models weight earlier context
            more heavily.
        samples: Optional sample rows, appended per table.

    Returns:
        Plain text. Not JSON: it is roughly a third of the tokens for the same
        information, and models parse it just as reliably.
    """
    sections: list[str] = ["Tables:"]

    for table_name in tables:
        table = snapshot.tables.get(table_name)
        if table is None:
            continue

        # Identifiers are rendered exactly as they must be written in SQL —
        # quoted when bare use would fold to something that does not exist.
        # The model copies what it is shown, so showing the correct form is
        # far more reliable than describing the rule and hoping.
        columns = ", ".join(
            f"{quote_identifier(column.name, dialect=dialect)} {column.type}"
            + (" PRIMARY KEY" if column.primary_key else "")
            + ("" if column.nullable else " NOT NULL")
            for column in table.columns
        )
        sections.append(f"  {quote_identifier(table.name, dialect=dialect)}({columns})")

    joins = describe_edges(graph, set(tables))
    if joins:
        sections.append("")
        sections.append("Relationships (use these exact join conditions):")
        sections.extend(f"  {predicate}" for predicate in joins)
    else:
        sections.append("")
        sections.append("Relationships: none between these tables.")

    # Value profiles are preferred over raw sample rows: they say what a column
    # can contain, which is what a WHERE clause needs, rather than what one
    # arbitrary row happened to hold.
    if profiles:
        sections.append("")
        sections.append(
            "Column values (use these exact values in filters; lists marked "
            "'e.g.' are examples, not the full set):"
        )
        sections.extend(f"  {profile.render()}" for profile in profiles)
    elif samples:
        sections.append("")
        sections.append("Example rows:")
        sections.extend(f"  {sample.render()}" for sample in samples)

    return "\n".join(sections)


def render_dialect_rules(dialect_name: str, rules: tuple[str, ...]) -> str:
    """State the target dialect's traps explicitly.

    Naming the dialect alone leaves the model to recall which functions exist
    where, and it recalls wrongly in predictable ways — integer division
    silently truncating a percentage, STRFTIME used against PostgreSQL. These
    rules are per dialect so the model is never shown syntax its database
    does not have.
    """
    if not rules:
        return f"Dialect: {dialect_name}"

    lines = [f"Dialect: {dialect_name}. Rules for this dialect:"]
    lines.extend(f"- {rule}" for rule in rules)
    return "\n".join(lines)


def build_generation_prompt(
    question: str,
    schema_text: str,
    *,
    dialect: str = "postgres",
    dialect_rules: tuple[str, ...] = (),
    glossary: str = "",
    conversation: str = "",
) -> str:
    """The prompt that asks for SQL.

    ``conversation`` is the rendered history from
    :func:`sqlagent.conversation.render_conversation`, empty for a first
    question. It sits *between* the schema and the question: the schema is the
    ground truth and belongs first, and the history is what the question is
    read against, so it belongs immediately before it.

    With an empty history the output is byte-identical to what this function
    produced before conversations existed — which is what keeps the benchmark
    numbers comparable across the change.
    """
    parts = [render_dialect_rules(dialect, dialect_rules), schema_text]
    # Schema says what exists; glossary says what it means; history says what we
    # were talking about. A model reading in that order has the definitions in
    # hand before it reaches the question.
    if glossary:
        parts.append(glossary)
    if conversation:
        parts.append(conversation)
    parts.append(f"Question: {question}")
    parts.append(
        "Write one SQL SELECT statement that answers the question. "
        "Output only the SQL."
    )
    return "\n\n".join(parts)


def build_repair_prompt(
    question: str,
    schema_text: str,
    failed_sql: str,
    error: str,
    *,
    dialect: str = "postgres",
    dialect_rules: tuple[str, ...] = (),
    glossary: str = "",
    conversation: str = "",
) -> str:
    """The prompt that asks for a corrected query after a failure.

    Includes the failed statement and the specific error. Both matter: without
    the statement the model cannot see what it did, and without the error it
    will often reproduce the same mistake.

    The error text is deliberately short — one line from the database, not a
    traceback. A long error crowds out the schema, which is the part that
    actually enables the fix.

    History is included here too. A repair prompt still has to *understand* the
    question, and "just the top 10" is no more self-contained on the second
    attempt than it was on the first.
    """
    parts = [render_dialect_rules(dialect, dialect_rules), schema_text]
    if glossary:
        parts.append(glossary)
    if conversation:
        parts.append(conversation)
    parts.append(f"Question: {question}")
    parts.append(f"This query failed:\n{failed_sql}")
    parts.append(f"Error: {error}")
    parts.append("Write a corrected SQL SELECT statement. Output only the SQL.")
    return "\n\n".join(parts)


ANSWER_SYSTEM_PROMPT = """\
You explain query results to someone who asked a question in plain language.

Rules:
- Answer the question directly in one or two sentences.
- Quote the actual numbers from the results. Never invent a figure.
- Use only names and values that appear in the results. If the results contain a
  name, write that name exactly. Never substitute a different one.
- Describe the results using only the filters the query actually applied. If the
  query has no WHERE clause restricting a property, do not call the results
  "vegetarian", "delivered", "recent" or anything else the SQL did not enforce —
  even if the user asked for it. Say what was measured, not what was wanted.
- If the query answered only part of the question, say which part is missing
  rather than presenting a partial result as complete.
- If the result set is empty, say so plainly and suggest why it might be.
- Do not describe the SQL or the database structure unless asked.
- This may be one turn in a conversation. If earlier turns are shown, answer as
  a continuation of them: say what changed rather than restating the whole
  setup, and never repeat a figure the user was already given as though it were
  new.
"""


def build_answer_prompt(
    question: str,
    sql: str,
    result_preview: str,
    *,
    glossary: str = "",
    conversation: str = "",
) -> str:
    """The prompt that turns rows into a sentence.

    The SQL is included so the model can describe what was measured accurately
    — "total spend per country" rather than a vague gesture at the numbers.

    History is included because the answer to a follow-up has to read like one.
    Asked "and for April?", a model with no history writes a fresh, context-free
    paragraph restating the metric and the filters; given the previous turn it
    writes "412, up from 389 in March" — which is the reply the person asked
    for.
    """
    parts = []
    # The answer needs the units as much as the query did: a figure the query
    # converted correctly can still be reported in the wrong currency, and a
    # raw stored value can still be presented as though it were rupees.
    if glossary:
        parts.append(glossary)
    if conversation:
        parts.append(conversation)
    parts.append(f"Question: {question}")
    parts.append(f"Query executed:\n{sql}")
    parts.append(f"Results:\n{result_preview}")
    parts.append("Answer the question based on these results.")
    return "\n\n".join(parts)
