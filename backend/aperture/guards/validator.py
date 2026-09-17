"""SQL validation over a parsed AST.

Regex guards on generated SQL are bypassable with comments, CTEs, or a second
statement; this parses instead. Three jobs:

1. reject anything that is not a single read-only statement,
2. reject calls to filesystem / network / sleep functions,
3. inject a row limit when the query does not have one.

`validate_sql` never raises on *user* error -- it returns a result carrying the
reason, because the repair loop feeds that reason back to the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Statement types that mutate data or schema. Anything in this set anywhere in
# the tree fails validation, including inside a CTE.
WRITE_NODES: tuple[type[exp.Expression], ...] = (
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

# Functions that read files, sleep, or reach the network. Names are matched
# case-insensitively against `exp.Anonymous` function names.
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

READ_ONLY_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
    exp.Subquery,
    exp.Values,
    exp.With,
)


class ValidationError(Exception):
    """Raised only for programmer error, not for a rejected query."""


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None
    reasons: list[str] = field(default_factory=list)
    limit_injected: bool = False
    is_write: bool = False

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _has_limit(tree: exp.Expression) -> bool:
    node = tree
    if isinstance(node, exp.With):
        node = node.this
    return bool(node.args.get("limit")) if isinstance(node, exp.Expression) else False


def validate_sql(sql: str, *, dialect: str = "postgres", row_limit: int = 1000) -> ValidationResult:
    """Parse `sql` and return whether it is safe to execute read-only."""
    if not sql or not sql.strip():
        return ValidationResult(ok=False, reasons=["empty statement"])

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except sqlglot.ParseError as err:
        return ValidationResult(ok=False, reasons=[f"parse error: {err}"])

    if len(statements) == 0:
        return ValidationResult(ok=False, reasons=["no statement found"])
    if len(statements) > 1:
        return ValidationResult(
            ok=False,
            reasons=[f"expected 1 statement, got {len(statements)}"],
        )

    tree = statements[0]
    reasons: list[str] = []

    write_hits = [n for n in tree.find_all(*WRITE_NODES)]
    if write_hits:
        kinds = sorted({type(n).__name__.upper() for n in write_hits})
        return ValidationResult(
            ok=False,
            reasons=[f"write statement not permitted: {', '.join(kinds)}"],
            is_write=True,
        )

    if not isinstance(tree, READ_ONLY_ROOTS):
        return ValidationResult(
            ok=False,
            reasons=[f"statement type {type(tree).__name__.upper()} is not a read query"],
        )

    for func in tree.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name in BANNED_FUNCTIONS:
            reasons.append(f"function not permitted: {name}")
    for func in tree.find_all(exp.Func):
        name = (func.sql_name() or "").lower()
        if name in BANNED_FUNCTIONS:
            reasons.append(f"function not permitted: {name}")

    if reasons:
        return ValidationResult(ok=False, reasons=reasons)

    limit_injected = False
    if not _has_limit(tree):
        target = tree.this if isinstance(tree, exp.With) else tree
        if isinstance(target, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            tree = tree.limit(row_limit)
            limit_injected = True

    return ValidationResult(
        ok=True,
        sql=tree.sql(dialect=dialect, pretty=True),
        limit_injected=limit_injected,
    )
