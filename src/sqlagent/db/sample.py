"""Fetch a couple of example rows per table, to show the model real values.

Why sampling earns its cost
---------------------------
The schema tells the model that ``orders.status`` is ``VARCHAR(20)``. It does
not say whether the values are ``'active'``/``'cancelled'`` or ``'A'``/``'C'``,
whether dates are stored as real ``DATE`` columns or as ``'2025-01-15'``
strings, or whether money is in pounds or pence. Those details decide whether a
``WHERE`` clause matches anything at all.

Two rows are usually enough to answer all of that, and two rows is a very small
amount of context.

The privacy cost, stated plainly
--------------------------------
These are real rows from a real table, and they travel into a model provider's
context and into any log that records the prompt. That is a genuine data
exposure, so:

* Columns can be excluded by name (``exclude_columns``), for anything holding
  personal or sensitive data.
* Values are truncated to a fixed width, which also bounds accidental leakage
  of long free-text fields.
* The whole feature can be switched off, and the pipeline still works — the
  model simply has less to go on.

Sampling is a static, code-generated ``SELECT``; the model never chooses what to
sample. That is deliberate: a model that could pick its own sample query could
be talked into sampling something it should not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from sqlagent.db.dialects import Dialect, for_engine_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TableSample:
    """A few rows from one table."""

    table: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    """Values already rendered as strings and truncated."""

    def render(self) -> str:
        """Format for inclusion in a prompt.

        Deliberately terse — a pipe-separated table costs far fewer tokens than
        JSON and models read it perfectly well.
        """
        if not self.rows:
            return f"{self.table}: (empty table)"

        lines = [f"{self.table} sample rows:", "  " + " | ".join(self.columns)]
        lines.extend("  " + " | ".join(row) for row in self.rows)
        return "\n".join(lines)


def sample_tables(
    engine: Engine,
    tables: list[str],
    *,
    limit: int = 2,
    max_cell_chars: int = 100,
    exclude_columns: set[str] | None = None,
    statement_timeout_ms: int = 5_000,
    dialect: Dialect | None = None,
) -> list[TableSample]:
    """Read a few rows from each table.

    Args:
        engine: Database connection.
        tables: Table names, already narrowed to the ones actually relevant.
            Sampling every table in the schema would defeat the purpose.
        limit: Rows per table.
        max_cell_chars: Truncate any value longer than this.
        exclude_columns: Column names to omit entirely, case-insensitive.
            Applies across all tables — intended for sensitive fields.
        statement_timeout_ms: Short by design. A sample is an optimisation; it
            must never be the reason a request is slow.

    Returns:
        One :class:`TableSample` per table that could be read. Tables that fail
        are skipped with a warning rather than raising — see below.

    Note:
        Failure here is deliberately non-fatal. If a table cannot be sampled
        (permissions, a lock, an exotic column type the driver cannot render),
        the correct behaviour is to continue without that sample. The agent
        still has the schema; it is merely less informed.
    """
    excluded = {name.lower() for name in (exclude_columns or set())}
    dialect = dialect or for_engine_name(engine.dialect.name)
    samples: list[TableSample] = []

    for table in tables:
        try:
            sample = _sample_one(
                engine,
                table,
                limit=limit,
                max_cell_chars=max_cell_chars,
                excluded=excluded,
                statement_timeout_ms=statement_timeout_ms,
                dialect=dialect,
            )
        except SQLAlchemyError as exc:
            logger.warning("could not sample %s: %s", table, exc)
            continue

        if sample is not None:
            samples.append(sample)

    return samples


def _sample_one(
    engine: Engine,
    table: str,
    *,
    limit: int,
    max_cell_chars: int,
    excluded: set[str],
    statement_timeout_ms: int,
    dialect: Dialect,
) -> TableSample | None:
    # The table name is interpolated rather than bound, because an identifier
    # cannot be a bind parameter in SQL. That is only safe because `table` comes
    # from schema reflection — it is a name the database itself reported, never
    # user or model input. Quoting defends against unusual but legal identifiers
    # (mixed case, spaces) rather than against injection.
    quoted = '"' + table.replace('"', '""') + '"'

    with engine.begin() as connection:
        for statement in dialect.session_setup(statement_timeout_ms=statement_timeout_ms):
            connection.execute(text(statement))
        cursor = connection.execute(text(f"SELECT * FROM {quoted} LIMIT {int(limit)}"))

        all_columns = list(cursor.keys())
        keep = [
            index
            for index, name in enumerate(all_columns)
            if name.lower() not in excluded
        ]
        if not keep:
            return None

        columns = tuple(all_columns[index] for index in keep)
        rows = tuple(
            tuple(_render(row[index], max_cell_chars) for index in keep)
            for row in cursor.fetchall()
        )

    return TableSample(table=table, columns=columns, rows=rows)


def _render(value: object, max_chars: int) -> str:
    """Render one value as a short, unambiguous string.

    ``NULL`` is spelled out: an empty cell would be indistinguishable from an
    empty string, and the difference changes which ``WHERE`` clause is correct.
    """
    if value is None:
        return "NULL"

    rendered = str(value)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "…"
    return rendered
