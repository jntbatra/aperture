"""Profile a table's columns to show the model what values actually look like.

Why this exists
---------------
Two sample rows tell the model that ``element`` is a ``TEXT`` column containing
``'c'``. They do not tell it that ``element`` only ever holds one of about ten
values, or which ten. So when a question asks "what percentage of atoms are
carbon", the model has to guess whether carbon is ``'c'``, ``'C'``, ``'carbon'``
or ``6`` — and a wrong guess produces a query that runs perfectly and returns
zero.

Measured on BIRD, the databases the agent does worst on are exactly the ones
with opaque coded values: ``toxicology`` (30%), ``california_schools`` (42%),
``thrombosis_prediction`` (42%). Their columns are things like ``element``,
``SEX``, ``Charter Funding Type`` — short codes whose meaning is unguessable
from the column name and type alone.

This module profiles each column from a single sample query and reports:

* **Low-cardinality columns** — every distinct value observed. This is the
  valuable part: a filter can now be written against real values.
* **Everything else** — a couple of examples, which is what sampling already
  gave us, to convey format (date layout, identifier shape).

How distinctness is determined — and a mistake worth recording
--------------------------------------------------------------
The first implementation read ``SELECT * FROM t LIMIT 200`` once per table and
computed distinct values in Python. That is cheap, and it is wrong.

``LIMIT`` with no ``ORDER BY`` returns the *first* rows in physical order, and
real tables are clustered. Measured against BIRD, **77% of the "this is the
complete value list" claims produced that way were false** —
``california_schools.frpm."County Code"`` appeared to hold one value because the
first 200 rows were all the same county; it actually holds 58. A model told a
partial list is exhaustive writes ``IN (...)`` filters that silently drop rows,
and accuracy fell 2.6 points.

So distinct values are now asked of the database directly:

    SELECT DISTINCT col FROM t LIMIT max_distinct + 1

Fetching one more than the threshold makes the answer exact rather than
inferred: at most ``max_distinct`` rows come back and the list genuinely is
complete; more come back and the column is not categorical, so only a couple of
examples are shown. No claim is made that cannot be backed.

Cost is one query per column rather than one per table. ``LIMIT`` lets the
engine stop early once enough distinct values are found, so this is far cheaper
than a full ``COUNT(DISTINCT ...)``, but it is not free — which is why the
whole feature is switchable.

Privacy
-------
More rows are *read* than before, but no more data is *sent*: only a bounded
set of short values per column is rendered. Long values are truncated, and a
column excluded by name is never read into the output at all. As with plain
sampling, the whole feature can be switched off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from sqlagent.db.dialects import Dialect, for_engine_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """What one column's values look like."""

    name: str
    values: tuple[str, ...]
    """Observed values, already rendered and truncated."""

    exhaustive: bool
    """True when ``values`` is provably every distinct value in the column.

    Established by asking for one more value than the threshold: if fewer come
    back, the list is complete. Drives the wording in the prompt, and getting
    it wrong is costly — a model told a partial list is complete writes an
    ``IN (...)`` filter that silently excludes real rows.
    """

    def render(self) -> str:
        if not self.values:
            return f"{self.name}: (no values)"
        joined = ", ".join(self.values)
        return f"{self.name}: {joined}" if self.exhaustive else f"{self.name}: e.g. {joined}"


@dataclass(frozen=True, slots=True)
class TableProfile:
    """Column profiles for one table."""

    table: str
    columns: tuple[ColumnProfile, ...]
    sampled_rows: int

    def render(self) -> str:
        if not self.columns:
            return f"{self.table}: (empty table)"

        categorical = [c for c in self.columns if c.exhaustive]
        examples = [c for c in self.columns if not c.exhaustive]

        lines = [f"{self.table}:"]
        if categorical:
            lines.append("    columns with a small fixed set of values:")
            lines.extend(f"      {c.render()}" for c in categorical)
        if examples:
            lines.append("    example values:")
            lines.extend(f"      {c.render()}" for c in examples)
        return "\n".join(lines)


def profile_tables(
    engine: Engine,
    tables: list[str],
    *,
    sample_rows: int = 200,
    max_distinct: int = 12,
    example_values: int = 2,
    max_cell_chars: int = 60,
    exclude_columns: set[str] | None = None,
    statement_timeout_ms: int = 5_000,
    dialect: Dialect | None = None,
) -> list[TableProfile]:
    """Profile each table's columns from one sample query per table.

    Args:
        engine: Database connection.
        tables: Tables to profile — already narrowed to the relevant ones.
        sample_rows: How many rows to read per table. Larger gives a more
            reliable cardinality estimate; 200 is enough to separate a status
            column from a name column.
        max_distinct: A column with at most this many distinct values in the
            sample is treated as categorical and every value is shown.
        example_values: How many examples to show for everything else.
        max_cell_chars: Truncate any rendered value longer than this.
        exclude_columns: Column names never to read, case-insensitive.
        statement_timeout_ms: Short by design — profiling is an optimisation and
            must never be why a request is slow.
        dialect: Session-hardening statements differ per engine.

    Returns:
        One profile per table that could be read. Tables that fail are skipped
        with a warning: a missing profile degrades the prompt, it does not
        break the request.
    """
    excluded = {name.lower() for name in (exclude_columns or set())}
    dialect = dialect or for_engine_name(engine.dialect.name)
    profiles: list[TableProfile] = []

    for table in tables:
        try:
            profile = _profile_one(
                engine,
                table,
                sample_rows=sample_rows,
                max_distinct=max_distinct,
                example_values=example_values,
                max_cell_chars=max_cell_chars,
                excluded=excluded,
                statement_timeout_ms=statement_timeout_ms,
                dialect=dialect,
            )
        except SQLAlchemyError as exc:
            logger.warning("could not profile %s: %s", table, exc)
            continue

        if profile is not None:
            profiles.append(profile)

    return profiles


def _profile_one(
    engine: Engine,
    table: str,
    *,
    sample_rows: int,
    max_distinct: int,
    example_values: int,
    max_cell_chars: int,
    excluded: set[str],
    statement_timeout_ms: int,
    dialect: Dialect,
) -> TableProfile | None:
    # Identifiers are interpolated because a name cannot be a bind parameter.
    # Safe only because both come from schema reflection — names the database
    # itself reported, never user or model input. The quoting handles unusual
    # but legal identifiers, not injection.
    quoted = _quote(table)

    with engine.begin() as connection:
        for statement in dialect.session_setup(statement_timeout_ms=statement_timeout_ms):
            connection.execute(text(statement))

        cursor = connection.execute(text(f"SELECT * FROM {quoted} LIMIT 1"))
        column_names = [name for name in list(cursor.keys()) if name.lower() not in excluded]
        has_rows = cursor.fetchone() is not None

        if not has_rows:
            return TableProfile(table=table, columns=(), sampled_rows=0)

        profiles: list[ColumnProfile] = []
        for name in column_names:
            # One more than the threshold: if fewer come back, this really is
            # every value the column holds.
            rows = connection.execute(
                text(
                    f"SELECT DISTINCT {_quote(name)} FROM {quoted} "
                    f"LIMIT {int(max_distinct) + 1}"
                )
            ).fetchall()

            values = [_render(row[0], max_cell_chars) for row in rows]
            exhaustive = len(values) <= max_distinct

            # A column with many distinct values is an identifier or free text.
            # Listing them teaches nothing about what to filter on, so show a
            # couple purely as a format hint.
            if not exhaustive:
                values = values[:example_values]

            profiles.append(
                ColumnProfile(name=name, values=tuple(values), exhaustive=exhaustive)
            )

    return TableProfile(table=table, columns=tuple(profiles), sampled_rows=1)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _render(value: object, max_chars: int) -> str:
    """Render one value unambiguously.

    ``NULL`` is spelled out, and strings are quoted. Both matter for a filter:
    an unquoted ``2024`` and a quoted ``'2024'`` need different SQL, and a blank
    cell is indistinguishable from an empty string.
    """
    if value is None:
        return "NULL"

    if isinstance(value, str):
        rendered = value if len(value) <= max_chars else value[:max_chars] + "…"
        return f"'{rendered}'"

    rendered = str(value)
    return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "…"
