"""Load a plain-text SQL dump into SQLite.

Direct connections are better when the database is reachable, which in a demo
or a locked-down network it often is not. A `pg_dump --inserts` or default
plain-text dump is then the fastest path to real data: the schema and rows are
right there in a file.

Only the plain-text format is supported. `pg_dump -Fc` is a compressed archive
that requires `pg_restore` and a live PostgreSQL to unpack, so it is rejected
with that explanation rather than half-parsed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from .ingest import IngestResult, datasets_dir, safe_identifier

log = logging.getLogger(__name__)

# Statements that are meaningful to a server but meaningless in SQLite.
SKIP_PREFIXES = (
    "set ",
    "select pg_catalog",
    "alter ",
    "create sequence",
    "create extension",
    "create index",
    "create unique index",
    "create trigger",
    "create function",
    "create type",
    "comment on",
    "grant ",
    "revoke ",
    "create schema",
    "drop ",
)

_COPY_START = re.compile(r"^COPY\s+(?P<table>[^\s(]+)\s*(\((?P<columns>[^)]*)\))?\s+FROM\s+stdin", re.I)


@dataclass
class DumpReport:
    tables: list[str] = field(default_factory=list)
    rows: int = 0
    skipped: int = 0


def _plain_name(identifier: str) -> str:
    return identifier.replace('"', "").split(".")[-1].strip()


def _unescape(value: str) -> str | None:
    """COPY encodes NULL as \\N and escapes tabs and newlines."""
    if value == r"\N":
        return None
    return (
        value.replace(r"\t", "\t").replace(r"\n", "\n").replace(r"\r", "\r").replace("\\\\", "\\")
    )


def _simplify_user_types(tree: exp.Expression) -> None:
    """Replace user-defined column types with TEXT.

    A dump of a database using enums declares columns as public."OrderStatus"
    and defaults them with CAST('CREATED' AS public."OrderStatus"). SQLite has
    neither, and both are only a labelling of text, so the column becomes TEXT
    and the cast is unwrapped to its literal.
    """
    for column in tree.find_all(exp.ColumnDef):
        kind = column.args.get("kind")
        if kind is None:
            continue
        rendered = kind.sql()
        if "." in rendered or kind.this == exp.DataType.Type.USERDEFINED:
            column.set("kind", exp.DataType.build("TEXT"))

    for cast in list(tree.find_all(exp.Cast)):
        target = cast.args.get("to")
        if target is None:
            continue
        rendered = target.sql()
        if "." in rendered or target.this == exp.DataType.Type.USERDEFINED:
            cast.replace(cast.this)


def _translate_create(statement: str) -> str | None:
    """Rewrite a PostgreSQL CREATE TABLE for SQLite, dropping what will not port."""
    try:
        tree = sqlglot.parse_one(statement, dialect="postgres")
    except Exception as err:
        log.debug("could not parse CREATE TABLE: %s", err)
        return None
    if not isinstance(tree, exp.Create):
        return None

    table = tree.find(exp.Table)
    if table is not None:
        # Dumps qualify tables as public.orders; SQLite has no schemas.
        table.set("db", None)
        table.set("catalog", None)

    _simplify_user_types(tree)
    try:
        return tree.sql(dialect="sqlite")
    except Exception as err:
        log.debug("could not render CREATE TABLE for sqlite: %s", err)
        return None


def load_sql_dump(source: str | Path, *, dataset: str | None = None) -> IngestResult:
    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(source)

    head = source.open("rb").read(8)
    if head.startswith(b"PGDMP"):
        raise ValueError(
            "this is a pg_dump custom-format archive; re-export with "
            "`pg_dump --format=plain` (optionally --inserts), or connect to the database directly"
        )

    dataset = safe_identifier(dataset or source.stem, fallback="dump")
    target = datasets_dir() / f"{dataset}.db"
    if target.exists():
        target.unlink()

    connection = sqlite3.connect(str(target))
    report = DumpReport()
    buffer: list[str] = []
    copy_table: str | None = None
    copy_columns: list[str] = []
    copy_batch: list[tuple] = []

    def flush_copy() -> None:
        nonlocal copy_batch
        if copy_table and copy_batch:
            placeholders = ", ".join("?" for _ in copy_columns)
            columns = ", ".join(f'"{c}"' for c in copy_columns)
            connection.executemany(
                f'INSERT INTO "{copy_table}" ({columns}) VALUES ({placeholders})', copy_batch
            )
            report.rows += len(copy_batch)
        copy_batch = []

    try:
        with source.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.rstrip("\n")

                if copy_table is not None:
                    if stripped == r"\.":
                        flush_copy()
                        copy_table, copy_columns = None, []
                        continue
                    values = [_unescape(v) for v in stripped.split("\t")]
                    if len(values) == len(copy_columns):
                        copy_batch.append(tuple(values))
                        if len(copy_batch) >= 1000:
                            flush_copy()
                    continue

                start = _COPY_START.match(stripped)
                if start:
                    copy_table = _plain_name(start.group("table"))
                    raw_columns = start.group("columns") or ""
                    copy_columns = [_plain_name(c) for c in raw_columns.split(",") if c.strip()]
                    if not copy_columns:
                        info = connection.execute(f'PRAGMA table_info("{copy_table}")').fetchall()
                        copy_columns = [row[1] for row in info]
                    continue

                bare = stripped.strip()
                # psql meta-commands (\restrict, \connect) and comments are not
                # SQL. Newer pg_dump emits \restrict on the first line, which
                # otherwise glues itself onto the first real statement.
                if not bare or bare.startswith("\\") or bare.startswith("--"):
                    continue

                buffer.append(stripped)
                if not stripped.rstrip().endswith(";"):
                    continue

                statement = "\n".join(buffer).strip()
                buffer = []
                # Skipping is decided per statement, not per line: dropping the
                # first line of a multi-line ALTER TABLE would leave its
                # continuation behind as an orphan fragment.
                if statement.lower().startswith(SKIP_PREFIXES):
                    report.skipped += 1
                    continue
                head_word = statement.split(None, 1)[0].lower() if statement else ""

                if head_word == "create":
                    translated = _translate_create(statement)
                    if not translated:
                        report.skipped += 1
                        continue
                    try:
                        connection.execute(translated)
                        match = re.search(r'CREATE TABLE\s+"?([A-Za-z_][\w]*)"?', translated, re.I)
                        if match:
                            report.tables.append(match.group(1))
                    except sqlite3.Error as err:
                        log.debug("skipped CREATE: %s -- %s", err, translated[:160])
                        report.skipped += 1
                elif head_word == "insert":
                    try:
                        rendered = sqlglot.transpile(
                            statement, read="postgres", write="sqlite"
                        )[0]
                        connection.execute(rendered)
                        report.rows += 1
                    except Exception as err:
                        log.debug("skipped INSERT: %s", err)
                        report.skipped += 1
                else:
                    report.skipped += 1

        flush_copy()
        connection.commit()
    finally:
        connection.close()

    if not report.tables:
        raise ValueError("no tables could be read from that dump")

    log.info("loaded %d tables, %d rows, skipped %d statements", len(report.tables), report.rows, report.skipped)
    return IngestResult(
        database_url=f"sqlite:///{target}",
        path=target,
        table=report.tables[0],
        rows=report.rows,
        columns=[],
    )
