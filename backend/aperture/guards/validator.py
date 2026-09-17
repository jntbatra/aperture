"""SQL validation over a parsed AST.

Regex guards on generated SQL are bypassable with comments, CTEs, or a second
statement; this parses instead. Four jobs:

1. reject anything that is not a single read-only statement,
2. reject calls to filesystem / network / sleep functions,
3. inject a row limit when the query does not have one,
4. report *why* it failed in a form the repair loop can branch on, and report
   which tables and columns the query touched so callers can check the model
   stayed inside the schema it was given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from sqlglot import exp

FailureKind = Literal[
    "ok",
    "empty",
    "parse",
    "write",
    "multi_statement",
    "banned_function",
    "not_a_query",
    "no_projection",
    "locking",
]

# Statement types that mutate data or schema. Anything in this set anywhere in
# the tree fails validation, including inside a CTE.
WRITE_NODES: tuple[type, ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Merge,
)

# Functions that read files, sleep, or reach the network.
BANNED_FUNCTIONS = {
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_sleep",
    "pg_sleep_for",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "copy_from_program",
    "load_extension",
    "readfile",
    "load_file",
    "sys_exec",
}

READ_ONLY_ROOTS: tuple[type, ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
    exp.Subquery,
    exp.Values,
)


@dataclass
class ValidationResult:
    ok: bool
    kind: FailureKind = "ok"
    sql: str | None = None
    # The statement as parsed, kept even on failure so a repair prompt can show
    # the model what it actually produced.
    original_sql: str = ""
    reasons: list[str] = field(default_factory=list)
    limit_injected: bool = False
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    dialect: str = ""

    @property
    def is_write(self) -> bool:
        return self.kind == "write"

    @property
    def repairable(self) -> bool:
        """Banned functions and writes are refusals, not things to retry."""
        return self.kind in {"parse", "not_a_query", "no_projection", "multi_statement"}

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _referenced(tree: exp.Expr) -> tuple[list[str], list[str]]:
    tables = {t.name for t in tree.find_all(exp.Table) if t.name}
    columns = {c.name for c in tree.find_all(exp.Column) if c.name}
    return sorted(tables), sorted(columns)


def _has_limit(tree: exp.Expr) -> bool:
    return bool(tree.args.get("limit"))


def _has_projection(tree: exp.Expr) -> bool:
    """A bare `SELECT` parses cleanly and returns one empty row. Reject it."""
    if isinstance(tree, exp.Select):
        return bool(tree.expressions)
    return True


def validate_sql(sql: str, *, dialect: str, row_limit: int = 1000) -> ValidationResult:
    """Parse `sql` and decide whether it is safe to execute read-only.

    `dialect` is required: defaulting it silently mis-parses every non-Postgres
    database, which is exactly the kind of bug that survives a demo and ruins a
    benchmark run.
    """
    if not sql or not sql.strip():
        return ValidationResult(ok=False, kind="empty", reasons=["empty statement"], dialect=dialect)

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except sqlglot.ParseError as err:
        return ValidationResult(
            ok=False, kind="parse", original_sql=sql, reasons=[f"parse error: {err}"], dialect=dialect
        )

    if not statements:
        return ValidationResult(
            ok=False, kind="parse", original_sql=sql, reasons=["no statement found"], dialect=dialect
        )
    if len(statements) > 1:
        return ValidationResult(
            ok=False,
            kind="multi_statement",
            original_sql=sql,
            reasons=[f"expected 1 statement, got {len(statements)}"],
            dialect=dialect,
        )

    tree = statements[0]
    tables, columns = _referenced(tree)
    base = {"original_sql": sql, "tables": tables, "columns": columns, "dialect": dialect}

    write_hits = list(tree.find_all(*WRITE_NODES))
    if write_hits:
        kinds = sorted({type(n).__name__.upper() for n in write_hits})
        return ValidationResult(
            ok=False, kind="write", reasons=[f"write statement not permitted: {', '.join(kinds)}"], **base
        )

    if not isinstance(tree, READ_ONLY_ROOTS):
        return ValidationResult(
            ok=False,
            kind="not_a_query",
            reasons=[f"statement type {type(tree).__name__.upper()} is not a read query"],
            **base,
        )

    # SELECT ... INTO creates a table; the read-only role would refuse it, but
    # the validator should not be the layer that lets it through.
    if tree.args.get("into"):
        return ValidationResult(
            ok=False, kind="write", reasons=["SELECT ... INTO creates a table"], **base
        )

    if tree.args.get("locks"):
        return ValidationResult(
            ok=False, kind="locking", reasons=["row locking (FOR UPDATE/SHARE) not permitted"], **base
        )

    if not _has_projection(tree):
        return ValidationResult(
            ok=False, kind="no_projection", reasons=["query selects no columns"], **base
        )

    banned = []
    for func in tree.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else ""
        if not name:
            try:
                name = func.sql_name()
            except NotImplementedError:
                name = ""
        if name.lower() in BANNED_FUNCTIONS:
            banned.append(f"function not permitted: {name.lower()}")
    if banned:
        return ValidationResult(ok=False, kind="banned_function", reasons=sorted(set(banned)), **base)

    limit_injected = False
    if isinstance(tree, exp.Query) and not _has_limit(tree):
        tree = tree.limit(row_limit)
        limit_injected = True

    return ValidationResult(
        ok=True, kind="ok", sql=tree.sql(dialect=dialect, pretty=True), limit_injected=limit_injected, **base
    )
