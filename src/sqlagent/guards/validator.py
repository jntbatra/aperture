"""Prove a generated statement is safe to run, before it reaches the database.

Why this exists when the database role is already read-only
-----------------------------------------------------------
The read-only role is the boundary that genuinely cannot be talked around: no
prompt, however cleverly worded, grants write permission the role does not
have. So why check here too?

1. **Configuration drift.** The role is granted once and trusted forever. If
   someone grants it write access during an incident and forgets to revoke,
   the only protection left is this layer. Two independent controls fail
   independently; one does not.
2. **Speed and message quality.** Parsing takes under a millisecond. Sending a
   bad statement to Postgres costs a round trip and returns
   ``permission denied for table orders``, which is a worse thing to feed back
   into a repair loop than "generated a DELETE, expected SELECT".
3. **Rules a database role cannot express.** A role is binary: read or write.
   It cannot say "every query must have a LIMIT", "do not touch
   ``customers.ssn``", or "only these eight tables". Those live here.

Parsing, not pattern matching
-----------------------------
A regex for ``DROP`` fails in both directions: it flags
``SELECT 'DROP' AS action`` and misses ``SELECT 1; drop table t``. sqlglot
builds a real syntax tree, so we inspect what the statement *is* rather than
what it looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DEFAULT_DIALECT = "postgres"

# Statement types that read data. Anything not on this list is refused, which
# is the safe default: a new sqlglot expression type appearing in a future
# version gets blocked rather than silently allowed.
ALLOWED_STATEMENTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

# Functions that read or write outside the queried tables, or change state
# that is not data. All are legitimate Postgres features; none belong in a
# generated analytics query.
#
# **This list is the weakest of the three layers, and it cannot be complete.**
# It is a denylist of names, so it protects against what someone thought of.
# Every entry below was added because it was found, several of them by
# attacking this validator rather than by reading it — and the honest
# conclusion is that the read-only role is what actually holds. The measured
# evidence for that is in docs/07-decisions.md: three of the functions here got
# past this check and were stopped by the database anyway.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        # Filesystem access
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        # Reaching another database
        "dblink",
        "dblink_exec",
        # Executing SQL passed as a string.
        #
        # The dangerous class, and the one a reader is most likely to miss:
        # `query_to_xml('DELETE FROM orders', ...)` is a SELECT containing a
        # function call containing a DELETE. Every structural check above
        # passes — the statement really is a SELECT, the CTEs really do only
        # read — and the argument is a string, so nothing that inspects the
        # AST can see the statement inside it.
        #
        # Found by attacking this file. The read-only transaction stopped it
        # ("DELETE is not allowed in a non-volatile function"), which is
        # exactly why that layer exists, but a guard that relies on the next
        # guard is not a guard.
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "table_to_xml",
        # Sequence state. Not table data, and still a persistent change: a
        # question must not be able to renumber anything. `currval` is absent
        # deliberately — it only reads.
        "nextval",
        "setval",
        # Server and session state
        "pg_reload_conf",
        "pg_rotate_logfile",
        "set_config",
        # Locks a read has no business taking, and that outlive the statement
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_try_advisory_lock",
        # Denial of service
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_terminate_backend",
        "pg_cancel_backend",
    }
)


class ValidationError(Exception):
    """A statement was refused. The message is written to be fed back to a model.

    Phrasing matters: this text goes into the repair prompt, so it says what was
    wrong and what was expected, not just that something failed.
    """

    def __init__(self, message: str, *, kind: str = "invalid_sql") -> None:
        super().__init__(message)
        self.kind = kind
        """Why it was refused, which decides what happens next.

        ``table_not_allowed`` is special: it means the query named a table that
        exists in the database but was not included in the context we built. The
        statement may well be correct — we simply did not offer enough schema.
        The remedy is to widen the graph walk and regenerate, not to tell the
        model to try harder with the same information.

        Every other kind means the statement itself was wrong.
        """


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of checking one statement."""

    sql: str
    """The statement to actually execute — possibly rewritten (e.g. a LIMIT added)."""

    tables: frozenset[str] = field(default_factory=frozenset)
    """Every table the statement reads, lowercased.

    Used to confirm the query stayed within the tables the model was shown, and
    recorded in the request trace for debugging.
    """

    limit_added: bool = False
    """True when this layer injected a LIMIT the model omitted."""


def validate(
    sql: str,
    *,
    dialect: str = DEFAULT_DIALECT,
    row_limit: int | None = None,
    allowed_tables: set[str] | None = None,
) -> ValidationResult:
    """Check a statement and return the version that should be executed.

    Args:
        sql: The raw statement, already stripped of markdown fences.
        dialect: SQL dialect for parsing.
        row_limit: If set, ensure the statement returns no more than this many
            rows — either by trusting a smaller existing LIMIT, or by adding
            one.
        allowed_tables: If set, every table read must appear here. Catches a
            model inventing a plausible-sounding table it was never shown.

    Returns:
        A :class:`ValidationResult` whose ``sql`` is safe to execute.

    Raises:
        ValidationError: The statement is empty, unparseable, not a single
            read-only statement, or touches something it should not.
    """
    if not sql or not sql.strip():
        raise ValidationError("Empty statement. Expected a single SELECT query.")

    statements = _parse(sql, dialect)

    if len(statements) > 1:
        raise ValidationError(
            f"Expected exactly one statement, found {len(statements)}. "
            "Multiple statements are refused because only the first would be checked."
        )

    statement = statements[0]

    _reject_non_select(statement)
    _reject_forbidden_functions(statement)
    _reject_locking_clauses(statement)
    _reject_data_modifying_ctes(statement)

    tables = _referenced_tables(statement)

    if allowed_tables is not None:
        # Compare case-insensitively. SQL identifiers are case-insensitive
        # unless quoted, so a model writing `FROM player` against a table
        # declared as `Player` is correct, and the database will resolve it
        # happily. Comparing raw strings here rejected every such query —
        # which silently broke entire databases whose tables are capitalised,
        # a very common convention.
        permitted = {name.lower() for name in allowed_tables}
        unexpected = {table for table in tables if table not in permitted}
        if unexpected:
            raise ValidationError(
                f"Query references table(s) not provided in the schema: "
                f"{', '.join(sorted(unexpected))}. "
                f"Available tables: {', '.join(sorted(allowed_tables))}.",
                kind="table_not_allowed",
            )

    limit_added = False
    if row_limit is not None:
        statement, limit_added = _apply_row_limit(statement, row_limit)

    return ValidationResult(
        sql=statement.sql(dialect=dialect),
        tables=frozenset(tables),
        limit_added=limit_added,
    )


def _parse(sql: str, dialect: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except sqlglot.ParseError as exc:
        raise ValidationError(f"Could not parse SQL: {exc}") from exc

    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise ValidationError("Statement parsed to nothing. Expected a single SELECT query.")
    return statements


def _reject_non_select(statement: exp.Expression) -> None:
    """Allow only statements that read.

    ``INSERT``/``UPDATE``/``DELETE``/``DROP``/``CREATE``/``GRANT`` and friends
    all land here. The error names what was found so a repair attempt has
    something to work with.
    """
    if isinstance(statement, ALLOWED_STATEMENTS):
        return

    found = type(statement).__name__.upper()
    raise ValidationError(
        f"Only read-only SELECT statements are permitted; found a {found} statement. "
        "Rewrite the answer as a SELECT."
    )


def _reject_forbidden_functions(statement: exp.Expression) -> None:
    """Block functions that reach outside the queried data."""
    for node in statement.find_all(exp.Anonymous, exp.Func):
        name = _function_name(node)
        if name and name.lower() in FORBIDDEN_FUNCTIONS:
            raise ValidationError(
                f"Function {name}() is not permitted in generated queries."
            )


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        value = node.args.get("this")
        return value if isinstance(value, str) else None
    return type(node).__name__ if isinstance(node, exp.Func) else None


def _reject_locking_clauses(statement: exp.Expression) -> None:
    """Refuse ``FOR UPDATE`` / ``FOR SHARE``.

    Structural rather than a function name, because this is syntax. A locking
    read is still a read — it returns rows and changes none — but it takes row
    locks that block writers for the length of the transaction, which is a way
    for an analytics question to stall an application. Nothing an analyst asks
    needs it.

    The read-only transaction rejects these too. This exists so the refusal is
    a sentence the model can repair from rather than a driver error surfaced
    three layers later.
    """
    for node in statement.find_all(exp.Lock):
        raise ValidationError(
            "Locking reads (FOR UPDATE / FOR SHARE) are not permitted; "
            "they block writers for the length of the transaction. "
            f"Remove the {node.sql().strip() or 'locking'} clause."
        )


def _reject_data_modifying_ctes(statement: exp.Expression) -> None:
    """Catch writes smuggled inside a WITH clause.

    Postgres allows ``WITH deleted AS (DELETE FROM t RETURNING *) SELECT * FROM
    deleted``. The outer statement is a SELECT, so the top-level type check
    above passes — but the query deletes rows. Every CTE body must itself be a
    read.
    """
    for cte in statement.find_all(exp.CTE):
        body = cte.this
        if body is not None and not isinstance(body, ALLOWED_STATEMENTS):
            found = type(body).__name__.upper()
            raise ValidationError(
                f"A WITH clause contains a {found} statement. "
                "Common table expressions must only read data."
            )


def _referenced_tables(statement: exp.Expression) -> set[str]:
    """Collect real table names, excluding CTE aliases.

    A CTE name looks exactly like a table reference in the tree, but it is
    defined inside the query itself. Counting it would make the
    ``allowed_tables`` check reject valid queries that use CTEs.
    """
    cte_names = {
        cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }

    tables: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if name and name not in cte_names:
            tables.add(name)

    return tables


def _apply_row_limit(statement: exp.Expression, row_limit: int) -> tuple[exp.Expression, bool]:
    """Ensure the statement cannot return more than ``row_limit`` rows.

    An existing smaller limit is respected — the model may have had a good
    reason for ``LIMIT 10``. A larger one is tightened, because the cap exists
    to protect the service, not to express intent.

    Set operations (``UNION`` and friends) are wrapped rather than modified in
    place: attaching a LIMIT to a UNION's final branch would limit that branch
    only, which is a subtly wrong result rather than an error.
    """
    if isinstance(statement, (exp.Union, exp.Except, exp.Intersect)):
        wrapped = exp.select("*").from_(statement.subquery(alias="bounded")).limit(row_limit)
        return wrapped, True

    existing = statement.args.get("limit")
    if existing is not None:
        expression = existing.expression
        if (
            isinstance(expression, exp.Literal)
            and expression.is_int
            and int(expression.name) <= row_limit
        ):
            return statement, False
        # Non-literal (e.g. a parameter) or too large: replace it.
        return statement.limit(row_limit, copy=True), True

    return statement.limit(row_limit, copy=True), True
