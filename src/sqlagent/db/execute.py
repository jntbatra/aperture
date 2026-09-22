"""Run a validated query against the database, under enforced limits.

Everything here is a *runtime* control, deliberately separate from the
validator's *static* ones. The validator reasons about the text of a statement;
this module bounds what happens once it is actually running. Both are needed:
a query can pass every static check and still scan a billion rows.

Three limits, and why each exists
---------------------------------
* **Statement timeout** — set on the connection, enforced by Postgres itself.
  A cost estimate can be badly wrong (stale statistics, a correlated subquery
  the planner misjudges); a wall-clock timeout cannot be argued with.
* **Row cap** — already injected into the SQL by the validator. Here we also
  stop reading after that many rows, so a query that somehow returns more
  cannot exhaust memory.
* **Read-only transaction** — every statement runs inside ``BEGIN READ ONLY``.
  Postgres then refuses writes at the transaction level, independently of
  whatever the role is permitted to do.

That last point is worth dwelling on: it means even if someone accidentally
grants write privileges to the agent's database role, an ``UPDATE`` still fails.
The validator would have caught it, and the role should have prevented it, and
this catches it too. Three independent layers, each sufficient alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from sqlagent.db.dialects import Dialect, classify_error, for_engine_name

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """A query failed to run.

    Carries a message shaped for a repair prompt rather than a raw traceback.
    """

    def __init__(self, message: str, *, kind: str = "unknown", sqlstate: str | None = None):
        super().__init__(message)
        self.kind = kind
        """Coarse classification driving what happens next.

        ``missing_relation`` and ``missing_column`` mean the schema context was
        insufficient — the fix is to widen the retrieved neighbourhood and try
        again. Everything else means the SQL itself was wrong, and the fix is to
        regenerate with the error text. See ``sqlagent.pipeline`` for where this
        decision is made.
        """

        self.sqlstate = sqlstate
        """The five-character SQLSTATE code Postgres returned, when available."""


# Error kinds and their per-dialect detection live in sqlagent.db.dialects.


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows returned by a successful query, plus how it went."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    seconds: float
    truncated: bool
    """True when the row cap stopped us short of the full result set.

    Surfaced to the user: "showing the first 1000 rows" is honest, silently
    dropping rows is not.
    """

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Rows as dictionaries, for JSON responses and prompt rendering."""
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def preview(self, limit: int = 20, max_cell_chars: int = 100) -> str:
        """A compact text table, for putting results into a prompt.

        Long values are clipped: a single 50KB text column would otherwise
        dominate the context window and push out the schema the model needs.
        """
        if not self.rows:
            return "(no rows)"

        shown = self.rows[:limit]
        header = " | ".join(self.columns)

        lines = [header, "-" * len(header)]
        for row in shown:
            cells = []
            for value in row:
                rendered = "NULL" if value is None else str(value)
                if len(rendered) > max_cell_chars:
                    rendered = rendered[:max_cell_chars] + "…"
                cells.append(rendered)
            lines.append(" | ".join(cells))

        if self.row_count > limit:
            lines.append(f"... {self.row_count - limit} more rows")

        return "\n".join(lines)


def execute(
    engine: Engine,
    sql: str,
    *,
    row_limit: int = 1000,
    statement_timeout_ms: int = 30_000,
    dialect: Dialect | None = None,
) -> QueryResult:
    """Run a query read-only, bounded in both time and rows.

    Args:
        engine: SQLAlchemy engine pointing at the database under analysis.
        sql: A statement that has already passed
            :func:`sqlagent.guards.validator.validate`. This function does not
            re-validate — it enforces runtime limits, which is a different job.
        row_limit: Stop reading after this many rows.
        statement_timeout_ms: Postgres-enforced wall-clock limit.

    Returns:
        A :class:`QueryResult`.

    Raises:
        ExecutionError: Classified by ``kind`` so the caller knows whether to
            widen the schema context or regenerate the SQL.
    """
    dialect = dialect or for_engine_name(engine.dialect.name)
    started = time.monotonic()

    try:
        # A single transaction with whatever hardening this engine supports.
        # `begin()` commits or rolls back on exit; for a read-only transaction
        # either is harmless.
        with engine.begin() as connection:
            for statement in dialect.session_setup(
                statement_timeout_ms=statement_timeout_ms
            ):
                connection.execute(text(statement))

            cursor = connection.execute(text(sql))

            columns = tuple(cursor.keys())

            # Read one row more than the cap. If it exists, the result was
            # genuinely larger and we report truncation honestly rather than
            # implying the query returned exactly `row_limit` rows.
            fetched = cursor.fetchmany(row_limit + 1)
            truncated = len(fetched) > row_limit
            rows = tuple(tuple(row) for row in fetched[:row_limit])

    except DBAPIError as exc:
        raise _classify(exc, dialect) from exc
    except SQLAlchemyError as exc:
        raise ExecutionError(str(exc), kind="unknown") from exc

    elapsed = round(time.monotonic() - started, 3)
    logger.debug("query returned %d rows in %.3fs", len(rows), elapsed)

    return QueryResult(columns=columns, rows=rows, seconds=elapsed, truncated=truncated)


def _classify(exc: DBAPIError, dialect: Dialect) -> ExecutionError:
    """Turn a database exception into a compact, actionable error.

    Raw driver exceptions are verbose and include the full statement, connection
    details and a traceback. Feeding that to a model wastes context and buries
    the one sentence that matters. This extracts the SQLSTATE, the primary
    message, and Postgres's own hint when it offers one — Postgres is
    frequently good at suggesting the column you meant.
    """
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)

    diagnostics = getattr(original, "diag", None)
    primary = getattr(diagnostics, "message_primary", None)
    hint = getattr(diagnostics, "message_hint", None)

    message = primary or str(original or exc).strip().splitlines()[0]
    if hint:
        message = f"{message} (hint: {hint})"

    kind = classify_error(dialect, message, sqlstate)

    if kind == "timeout":
        message = (
            "Query exceeded the time limit. It likely scans too much data — "
            "add a filter, or aggregate rather than returning raw rows."
        )

    return ExecutionError(message, kind=kind, sqlstate=sqlstate)
