"""Convert a plain-SQL ``pg_dump`` into a SQLite file, with no PostgreSQL server.

Only what a question-answering agent needs survives: tables, columns, row data,
primary keys and foreign keys. Views, functions, sequences, indexes, enums and
triggers are skipped, and column types are mapped to the nearest SQLite type
(arrays, JSON and UUIDs become TEXT). That is a loss, and the caller says so.

The dump is read twice, as a stream. Constraints come *after* the data in a
``pg_dump`` and SQLite cannot add a foreign key to an existing table, so the
first pass collects the table definitions and constraints and the second
builds the tables and loads the rows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

_COPY = re.compile(r"^COPY\s+(?P<table>\S+?)\s*\((?P<cols>.*?)\)\s+FROM\s+stdin;?\s*$", re.I | re.S)
_CONSTRAINT = re.compile(
    r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?P<table>\S+)\s+ADD\s+CONSTRAINT\s+\S+\s+"
    r"(?P<kind>PRIMARY\s+KEY|FOREIGN\s+KEY)\s*\((?P<cols>[^)]*)\)"
    r"(?:\s+REFERENCES\s+(?P<ref>\S+?)\s*\((?P<refcols>[^)]*)\))?",
    re.I | re.S,
)
_DOLLAR = re.compile(r"\$[A-Za-z_]*\$")
_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


class DumpError(Exception):
    """The dump could not be converted."""


def _name(raw: str) -> str:
    """``public."Order Items"`` -> ``Order Items``: drop schema and quoting."""
    last = raw.strip().rsplit(".", 1)[-1] if '"' not in raw else raw.strip().split('"')[-2]
    return last.strip('"')


def _names(raw: str) -> list[str]:
    return [_name(part) for part in raw.split(",") if part.strip()]


def _sqlite_type(pg_type: str) -> str:
    t = pg_type.lower()
    if "[" in t:
        return "TEXT"
    if re.match(r"(small|big)?(int|serial)|int[248]$", t):
        return "INTEGER"
    if t.startswith(("numeric", "decimal", "money")):
        return "NUMERIC"
    if t.startswith(("real", "double", "float")):
        return "REAL"
    if t.startswith("bool"):
        return "BOOLEAN"
    if t.startswith("timestamp"):
        return "TIMESTAMP"
    if t.startswith("date"):
        return "DATE"
    if t.startswith("time"):
        return "TIME"
    if t.startswith("bytea"):
        return "BLOB"
    return "TEXT"


def _statements(path: Path, *, rows: bool) -> Iterator[tuple[str, object]]:
    """Yield ``("stmt", sql)`` or ``("copy", (table, columns, rows))``.

    A ``copy`` item's row iterator must be exhausted before the next item is
    requested. With ``rows=False`` the data lines are skipped unparsed.
    """
    with path.open(encoding="utf-8", errors="replace") as handle:
        buffer: list[str] = []
        in_dollar = False
        in_quote = False
        for line in handle:
            if not buffer and (not line.strip() or line.startswith("--")):
                continue
            buffer.append(line)
            if len(_DOLLAR.findall(line)) % 2:
                in_dollar = not in_dollar
            if line.count("'") % 2:
                in_quote = not in_quote
            if in_dollar or in_quote or not line.rstrip().endswith(";"):
                continue
            statement = "".join(buffer).strip()
            buffer = []
            match = _COPY.match(statement)
            if not match:
                yield "stmt", statement
                continue

            def body() -> Iterator[list[str | None]]:
                for data in handle:
                    if data.rstrip("\n") == "\\.":
                        return
                    if rows:
                        yield [_unescape(field) for field in data.rstrip("\n").split("\t")]

            yield "copy", (_name(match["table"]), _names(match["cols"]), body())
        if buffer:
            yield "stmt", "".join(buffer).strip()


def _unescape(field: str) -> str | None:
    if field == "\\N":
        return None
    if "\\" not in field:
        return field
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 1 < len(field):
            out.append(_ESCAPES.get(field[i + 1], field[i + 1]))
            i += 2
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


class _Table:
    def __init__(self, name: str, columns: list[tuple[str, str]]):
        self.name = name
        self.columns = columns
        self.primary_key: list[str] = []
        self.foreign_keys: list[tuple[list[str], str, list[str]]] = []

    def ddl(self) -> str:
        parts = [f'"{c}" {t}' for c, t in self.columns]
        if self.primary_key:
            parts.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in self.primary_key) + ")")
        for cols, target, target_cols in self.foreign_keys:
            parts.append(
                "FOREIGN KEY ("
                + ", ".join(f'"{c}"' for c in cols)
                + f') REFERENCES "{target}" ('
                + ", ".join(f'"{c}"' for c in target_cols)
                + ")"
            )
        return f'CREATE TABLE "{self.name}" ({", ".join(parts)})'


def _parse_create(statement: str) -> _Table | None:
    try:
        tree = sqlglot.parse_one(statement, read="postgres")
    except sqlglot.errors.SqlglotError:
        logger.warning("skipped a CREATE TABLE it could not parse: %.80s", statement)
        return None
    if not isinstance(tree, exp.Create) or not isinstance(tree.this, exp.Schema):
        return None
    schema = tree.this
    table = _Table(schema.this.name, [])
    for item in schema.expressions:
        if isinstance(item, exp.ColumnDef):
            kind = item.args.get("kind")
            table.columns.append(
                (item.name, _sqlite_type(kind.sql("postgres")) if kind else "TEXT")
            )
            if any(isinstance(c.kind, exp.PrimaryKeyColumnConstraint) for c in item.constraints):
                table.primary_key.append(item.name)
    return table


def _convert(value: str | None, sqlite_type: str) -> object:
    if value is None:
        return None
    if sqlite_type == "BOOLEAN":
        return 1 if value in ("t", "true") else 0
    if sqlite_type == "BLOB" and value.startswith("\\x"):
        return bytes.fromhex(value[2:])
    return value


def convert(source: Path, target: Path) -> dict[str, int]:
    """Write ``source`` (plain SQL dump) as a SQLite database at ``target``.

    Returns the row count of every table. Raises :class:`DumpError` when the
    file holds no tables.
    """
    tables: dict[str, _Table] = {}
    saw_query = False
    for kind, item in _statements(source, rows=False):
        if kind == "copy":
            for _ in item[2]:  # type: ignore[index]
                pass
            continue
        statement = str(item)
        head = statement[:30].upper()
        saw_query = saw_query or head.startswith(("SELECT", "WITH"))
        if head.startswith("CREATE TABLE"):
            if (table := _parse_create(statement)) is not None:
                tables[table.name] = table
        elif head.startswith("ALTER TABLE") and (m := _CONSTRAINT.match(statement)):
            table = tables.get(_name(m["table"]))
            if table is None:
                continue
            if m["kind"].upper().startswith("PRIMARY"):
                table.primary_key = _names(m["cols"])
            else:
                table.foreign_keys.append(
                    (_names(m["cols"]), _name(m["ref"]), _names(m["refcols"]))
                )

    if not tables and saw_query:
        raise DumpError(
            "This file holds a SQL query, not a database dump: it has no tables or rows. "
            "Upload the data itself (a CSV, an Excel file, or a pg_dump in plain SQL)."
        )
    if not tables:
        raise DumpError(
            "No CREATE TABLE statements found. Upload a plain-SQL dump "
            "(pg_dump --format=plain), not a custom or directory archive."
        )

    target.unlink(missing_ok=True)
    db = sqlite3.connect(target)
    counts = dict.fromkeys(tables, 0)
    try:
        for table in tables.values():
            db.execute(table.ddl())
        for kind, item in _statements(source, rows=True):
            if kind == "copy":
                name, columns, data = item  # type: ignore[misc]
                table = tables.get(name)
                if table is None:
                    for _ in data:
                        pass
                    continue
                types = dict(table.columns)
                marks = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{c}"' for c in columns)
                sql = f'INSERT INTO "{name}" ({quoted}) VALUES ({marks})'
                batch = []
                for row in data:
                    batch.append(
                        [
                            _convert(v, types.get(c, "TEXT"))
                            for v, c in zip(row, columns, strict=False)
                        ]
                    )
                    if len(batch) >= 5000:
                        db.executemany(sql, batch)
                        counts[name] += len(batch)
                        batch = []
                if batch:
                    db.executemany(sql, batch)
                    counts[name] += len(batch)
            elif str(item)[:11].upper() == "INSERT INTO":
                statement = re.sub(r'(?i)\b"?public"?\.', "", str(item), count=1)
                try:
                    db.execute(statement)
                except sqlite3.Error as exc:
                    logger.warning("skipped an INSERT: %s", exc)
        db.commit()
        counts = {n: db.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in tables}
    finally:
        db.close()
    return counts
