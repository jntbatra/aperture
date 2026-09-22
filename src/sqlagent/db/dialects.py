"""Per-dialect differences, isolated in one place.

The agent is meant to work against more than one kind of database. Almost all
of the code is dialect-agnostic — reflection, the schema graph, retrieval, the
prompts — because SQLAlchemy and sqlglot absorb the differences. Two things do
not absorb cleanly, and they live here:

1. **How you make a session safe.** Postgres has ``SET LOCAL
   transaction_read_only`` and ``SET LOCAL statement_timeout``. SQLite has
   neither; it enforces read-only by how the file is opened.
2. **How you read an error.** Postgres returns a five-character SQLSTATE, which
   is precise and stable. SQLite returns an English sentence, which is neither,
   so it has to be matched on text.

Keeping these apart means adding a dialect later is a change in this file
rather than a hunt through the codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Error kinds used across the codebase. `missing_relation` and `missing_column`
# are the two that matter most: they tell the pipeline the schema context was
# too narrow, rather than the SQL being wrong. See sqlagent.pipeline.
KIND_MISSING_RELATION = "missing_relation"
KIND_MISSING_COLUMN = "missing_column"
KIND_BAD_FUNCTION = "bad_function"
KIND_SYNTAX = "syntax"
KIND_TIMEOUT = "timeout"
KIND_PERMISSION = "permission"
KIND_WRITE_ATTEMPT = "write_attempt"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Dialect:
    """What the agent needs to know about one database engine."""

    name: str
    """SQLAlchemy's dialect name: ``postgresql``, ``sqlite``, ``mysql``."""

    sqlglot_name: str
    """What sqlglot calls the same dialect, for parsing and validation."""

    prompt_name: str
    """What to call it in a prompt. Models respond better to 'PostgreSQL' than
    to 'postgresql'."""

    supports_statement_timeout: bool
    """Whether the server can cancel a long query on its own."""

    supports_explain: bool = False
    """Whether ``EXPLAIN`` returns a plan the cost gate can read.

    Not "whether EXPLAIN exists" — SQLite has one, but it emits bytecode with no
    cost estimate at all, so there is nothing to gate on. False means the gate
    abstains and the statement timeout does the work alone.
    """

    sqlstate_kinds: dict[str, str] = field(default_factory=dict)
    """SQLSTATE code -> error kind, for engines that report them."""

    message_patterns: tuple[tuple[str, str], ...] = ()
    """(regex, error kind) pairs, for engines that only return prose."""

    prompt_rules: tuple[str, ...] = ()
    """Dialect-specific rules placed in the SQL-generation prompt.

    "Write PostgreSQL" is not enough. The traps that actually cost accuracy are
    specific: integer division silently truncating a percentage to 0, a date
    function that exists in one dialect and not another, the wrong string
    concatenation operator.

    Measured on BIRD (SQLite): a missing ``CAST(... AS REAL)`` appeared in 6% of
    failures and a wrong date function in 5% — about a ninth of all failures
    between them, all avoidable by stating the rule.

    Kept per dialect rather than as one generic list so the model is never told
    about syntax its target database does not have."""

    def session_setup(self, *, statement_timeout_ms: int) -> list[str]:
        """Statements to run before the query, to bound what it may do."""
        return []


@dataclass(frozen=True)
class PostgresDialect(Dialect):
    def session_setup(self, *, statement_timeout_ms: int) -> list[str]:
        return [
            # Server-enforced wall clock. The planner's cost estimate can be
            # wrong; this cannot.
            f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}",
            # Refuses writes at the transaction level, independently of what
            # the connected role is permitted to do.
            "SET LOCAL transaction_read_only = on",
        ]


POSTGRES = PostgresDialect(
    name="postgresql",
    sqlglot_name="postgres",
    prompt_name="PostgreSQL",
    supports_statement_timeout=True,
    # EXPLAIN reports a cost estimate the gate can read.
    supports_explain=True,
    prompt_rules=(
        "Integer division truncates: 5/2 is 2. For a ratio or percentage cast "
        "first, e.g. CAST(x AS NUMERIC) / y or x::numeric / y.",
        "Extract date parts with EXTRACT(YEAR FROM col) or DATE_TRUNC('month', col). "
        "STRFTIME and YEAR() do not exist.",
        "Concatenate strings with || or CONCAT().",
        "Use ILIKE for case-insensitive matching.",
        "Booleans are TRUE and FALSE, not 1 and 0.",
        "Every non-aggregated column in the SELECT must appear in GROUP BY.",
        'Unquoted identifiers are folded to lower case, so a column named '
        '"itemId" must be written with double quotes. Copy the exact spelling '
        "shown in the schema above, including its quotes.",
    ),
    # https://www.postgresql.org/docs/current/errcodes-appendix.html
    sqlstate_kinds={
        "42P01": KIND_MISSING_RELATION,
        "42703": KIND_MISSING_COLUMN,
        "42883": KIND_BAD_FUNCTION,
        "42601": KIND_SYNTAX,
        "57014": KIND_TIMEOUT,
        "42501": KIND_PERMISSION,
        "25006": KIND_WRITE_ATTEMPT,
    },
)

SQLITE = Dialect(
    name="sqlite",
    sqlglot_name="sqlite",
    prompt_name="SQLite",
    # SQLite has no server-side statement timeout. Read-only is enforced by
    # opening the database file in read-only mode instead; see
    # `read_only_url` below.
    supports_statement_timeout=False,
    # SQLite's EXPLAIN emits virtual-machine bytecode with no cost estimate, so
    # there is nothing for the gate to read.
    supports_explain=False,
    prompt_rules=(
        "Integer division truncates: 5/2 is 2, and COUNT(...)/COUNT(...) is "
        "almost always 0 or 1. For any ratio or percentage wrap the numerator: "
        "CAST(COUNT(...) AS REAL) * 100 / COUNT(...).",
        "Extract date parts with STRFTIME('%Y', col) or SUBSTR(col, 1, 4). "
        "EXTRACT, DATE_TRUNC and YEAR() do not exist.",
        "Concatenate strings with ||. CONCAT() does not exist.",
        "IIF(condition, a, b) is available, as is CASE WHEN.",
        "LIKE is case-insensitive for ASCII by default; there is no ILIKE.",
        "There is no FULL OUTER JOIN. Use LEFT JOIN, or a UNION of two joins.",
        "Booleans are stored as 1 and 0.",
    ),
    message_patterns=(
        (r"no such table", KIND_MISSING_RELATION),
        (r"no such column", KIND_MISSING_COLUMN),
        (r"no such function", KIND_BAD_FUNCTION),
        (r"(syntax error|incomplete input)", KIND_SYNTAX),
        (r"(readonly database|attempt to write)", KIND_WRITE_ATTEMPT),
        (r"interrupted", KIND_TIMEOUT),
    ),
)

MYSQL = Dialect(
    name="mysql",
    sqlglot_name="mysql",
    prompt_name="MySQL",
    supports_statement_timeout=False,
    prompt_rules=(
        "Integer division truncates: use CAST(x AS DECIMAL) or x * 1.0 before "
        "dividing for a ratio or percentage.",
        "Extract date parts with YEAR(col), MONTH(col) or DATE_FORMAT(col, '%Y'). "
        "STRFTIME and DATE_TRUNC do not exist.",
        "Concatenate strings with CONCAT(). The || operator is logical OR, not "
        "concatenation.",
        "There is no FULL OUTER JOIN. Use LEFT JOIN, or a UNION of two joins.",
        "Quote identifiers with backticks, not double quotes.",
    ),
    message_patterns=(
        (r"doesn't exist", KIND_MISSING_RELATION),
        (r"unknown column", KIND_MISSING_COLUMN),
        (r"does not exist", KIND_BAD_FUNCTION),
        (r"you have an error in your sql syntax", KIND_SYNTAX),
        (r"read.only", KIND_WRITE_ATTEMPT),
    ),
)

_BY_NAME = {d.name: d for d in (POSTGRES, SQLITE, MYSQL)}


def for_engine_name(name: str) -> Dialect:
    """Look up a dialect by SQLAlchemy's name for it.

    Falls back to Postgres rather than raising: an unrecognised engine still
    works, it simply gets no dialect-specific session hardening. The validator
    and the read-only role remain in force either way, so the fallback is
    degraded, not unsafe.
    """
    return _BY_NAME.get(name, POSTGRES)


def classify_error(dialect: Dialect, message: str, sqlstate: str | None) -> str:
    """Work out what kind of failure this was.

    Prefers SQLSTATE when available, because it is exact. Falls back to
    matching the message, which is what SQLite and MySQL force on us.
    """
    if sqlstate and sqlstate in dialect.sqlstate_kinds:
        return dialect.sqlstate_kinds[sqlstate]

    lowered = message.lower()
    for pattern, kind in dialect.message_patterns:
        if re.search(pattern, lowered):
            return kind

    return KIND_UNKNOWN


def read_only_url(url: str) -> str:
    """Rewrite a connection URL so the database cannot be written to.

    Only meaningful for SQLite, where read-only is a property of how the file
    is opened rather than of a role or a transaction. Used by the benchmark
    harness, which runs against database files it must not modify.

    Other dialects are returned unchanged — they enforce read-only through the
    connected role and the read-only transaction instead.
    """
    if not url.startswith("sqlite"):
        return url
    if "mode=ro" in url:
        return url

    _, _, path = url.partition("sqlite:///")
    if not path or path.startswith("file:"):
        return url

    return f"sqlite:///file:{path}?mode=ro&uri=true"
