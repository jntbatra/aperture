"""Per-column documentation, from wherever the deployment keeps it.

Why this exists
---------------
A column named ``EdOpsCode`` holding ``'TRAD'`` is opaque. The name does not
say what it means and a sample value does not either. Both systems above us on
the corrected BIRD leaderboard feed the model a written description per column
and fall back to sampled values only where none exists — ReViSQL uses BIRD's
own ``database_description`` CSVs, OpenSearch-SQL builds its entire column
rendering from them.

BIRD ships that documentation for every one of its databases and this project
referenced none of it. Measured across all 11 mini-dev databases: 799 columns
have a description row, 565 (71%) carry a description that says more than the
column name repeated, and 278 (35%) carry a ``value_description`` — often the
enumerated legal values, which is the single most useful thing a filter can be
told.

This is not benchmark-specific machinery. It is the same information a real
warehouse keeps in ``COMMENT ON COLUMN``, and :func:`from_comments` reads that
form. The CSV loader exists because SQLite has nowhere to put a comment.

Deliberately not a model call
-----------------------------
Asking a model to describe a column costs a call per database and invents
confident nonsense for the columns that most need explaining. Documentation
that a human wrote is worth more than documentation a model guessed, and the
absence of it is itself informative — a column nobody documented is usually
one nobody uses.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 180
"""Longer than this and it is prose, not a label.

BIRD's longest value descriptions run to several sentences of commentary. The
first clause carries the meaning; the rest costs tokens on every question that
touches the table.
"""


@dataclass(frozen=True, slots=True)
class ColumnDoc:
    """What is known about one column beyond its name and type."""

    description: str = ""
    """What the column means."""

    values: str = ""
    """What its values mean, or which ones are legal.

    Kept apart from ``description`` because it is the more useful half for
    writing a WHERE clause and should survive truncation independently.
    """

    def render(self) -> str:
        parts = [p for p in (self.description, self.values) if p]
        return " — ".join(parts)


def _clean(text: str | None, *, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    # BIRD writes this prefix on roughly a third of its value descriptions. It
    # tells the model nothing it cannot see from the position of the text.
    for noise in ("commonsense evidence:", "commonsense reasoning:"):
        if collapsed.lower().startswith(noise):
            collapsed = collapsed[len(noise):].strip()
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip(" ,;.") + "…"
    return collapsed


def _is_redundant(description: str, column: str) -> bool:
    """A description that restates the column name carries nothing.

    BIRD has many of these — ``CDSCode`` described as "CDSCode". Keeping them
    doubles the token cost of the schema for no information, which is worse
    than having no description at all.
    """
    squash = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())  # noqa: E731
    return squash(description) == squash(column)


def from_csv_directory(root: Path | str) -> dict[tuple[str, str], ColumnDoc]:
    """Load BIRD-style ``database_description/<table>.csv`` files.

    One CSV per table, named after the table. Columns used:
    ``original_column_name``, ``column_description``, ``value_description``.

    Never raises. A malformed or missing description file must not stop a
    question from being answered — the schema renders without it.
    """
    root = Path(root)
    docs: dict[tuple[str, str], ColumnDoc] = {}
    if not root.is_dir():
        return docs

    for path in sorted(root.glob("*.csv")):
        table = path.stem
        try:
            # utf-8-sig: BIRD's files carry a BOM, which otherwise becomes part
            # of the first header name and silently breaks every lookup in the
            # first column.
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the rest
            logger.debug("column docs: cannot read %s: %s", path, exc)
            continue

        for row in csv.DictReader(io.StringIO(raw)):
            column = (row.get("original_column_name") or "").strip()
            if not column:
                continue
            description = _clean(row.get("column_description"))
            if _is_redundant(description, column):
                description = ""
            doc = ColumnDoc(description=description, values=_clean(row.get("value_description")))
            if doc.render():
                docs[(table, column)] = doc

    return docs


def from_comments(engine, tables: list[str]) -> dict[tuple[str, str], ColumnDoc]:
    """Read native column comments — the production path.

    PostgreSQL's ``COMMENT ON COLUMN`` is the same information in the place a
    real deployment already keeps it. SQLite has no equivalent and returns
    nothing, which is why the CSV loader exists alongside this.
    """
    from sqlalchemy import inspect

    docs: dict[tuple[str, str], ColumnDoc] = {}
    try:
        inspector = inspect(engine)
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        logger.debug("column docs: cannot inspect: %s", exc)
        return docs

    for table in tables:
        try:
            columns = inspector.get_columns(table)
        except Exception:  # noqa: BLE001, PERF203
            continue
        for column in columns:
            comment = _clean(column.get("comment"))
            if comment and not _is_redundant(comment, column["name"]):
                docs[(table, column["name"])] = ColumnDoc(description=comment)

    return docs
