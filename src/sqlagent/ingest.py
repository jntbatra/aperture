"""Turn an uploaded file into a database the agent can query.

Three kinds of upload, three quite different problems:

**CSV** — one file, one table. Column types have to be inferred, because a CSV
has none.

**Excel** — one file, one table *per sheet*. Otherwise as CSV.

**PostgreSQL dump** — already a database. It has real types, real primary keys
and, crucially, real foreign keys, so the schema graph works properly on it.

The foreign-key problem, stated plainly
---------------------------------------
The agent's schema retrieval walks foreign keys. A CSV has none, and neither
does a workbook. So an upload of five CSVs produces five islands: the agent can
answer questions about any one of them, and cannot join them, because nothing
records that ``orders.customer_id`` refers to ``customers.id``.

Rather than guess — a wrong inferred join silently returns wrong rows, which is
the worst failure mode this system has — relationships can be declared
explicitly after upload (:func:`add_relationship`). Nothing is inferred from
column names.

A pg dump has none of this problem, which is why it is the best thing to upload.

Where the data goes
-------------------
Each upload becomes its own SQLite file under the data directory. That keeps
uploads isolated from each other and from the configured production database,
makes deletion a single file removal, and means a malformed upload cannot
affect anything else.

pg dumps are the exception: they need a PostgreSQL server, and are restored into
a dedicated database on the one configured for the purpose.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
DUMP_SUFFIXES = {".sql", ".dump", ".backup"}

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
"""Refuse anything larger. A bound has to exist somewhere, and failing at the
boundary with a clear message beats filling the disk."""


class IngestError(Exception):
    """An upload could not be turned into a queryable database."""


@dataclass
class Dataset:
    """One uploaded file, now queryable."""

    id: str
    name: str
    kind: str
    """``csv``, ``excel`` or ``pg_dump``."""

    database_url: str
    tables: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    created_at: str = ""
    note: str = ""
    """Anything the user should know — inferred types, missing relationships."""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "tables": self.tables,
            "row_counts": self.row_counts,
            "created_at": self.created_at,
            "note": self.note,
        }


def safe_table_name(raw: str) -> str:
    """Turn a filename or sheet name into a legal, predictable table name.

    ``Q1 Sales (final).csv`` becomes ``q1_sales_final``. Lowercased because
    mixed-case identifiers in SQL require quoting, and a model that forgets the
    quotes writes a query that fails.
    """
    stem = Path(raw).stem.lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)

    if not cleaned:
        cleaned = "data"
    if cleaned[0].isdigit():
        # An identifier cannot start with a digit.
        cleaned = f"t_{cleaned}"
    return cleaned[:60]


def _read_csv(path: Path) -> pd.DataFrame:
    separator = "\t" if path.suffix.lower() == ".tsv" else None
    # sep=None asks pandas to sniff the delimiter, which handles semicolon-
    # separated exports from European spreadsheet software. It requires the
    # Python engine.
    return pd.read_csv(path, sep=separator, engine="python" if separator is None else "c")


def _normalise_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to legal identifiers, keeping them unique.

    A spreadsheet column called ``Total (£)`` is unusable in SQL without
    quoting; ``total`` is not.
    """
    seen: dict[str, int] = {}
    names: list[str] = []

    for column in frame.columns:
        base = safe_table_name(str(column)) or "column"
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        names.append(base)

    frame.columns = names
    return frame


def ingest_file(
    source: Path,
    *,
    data_dir: Path,
    original_name: str | None = None,
    postgres_admin_url: str | None = None,
) -> Dataset:
    """Turn an uploaded file into a queryable dataset.

    Args:
        source: The uploaded file on disk.
        data_dir: Where per-dataset SQLite files are kept.
        original_name: The name the user uploaded it under, used for display
            and to choose the loader.
        postgres_admin_url: A PostgreSQL URL with rights to create databases.
            Required only for dumps.

    Raises:
        IngestError: Unsupported type, oversized, or the file could not be read.
    """
    name = original_name or source.name
    suffix = Path(name).suffix.lower()

    size = source.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise IngestError(
            f"File is {size / 1e6:.0f} MB; the limit is {MAX_UPLOAD_BYTES / 1e6:.0f} MB."
        )
    if size == 0:
        raise IngestError("File is empty.")

    data_dir.mkdir(parents=True, exist_ok=True)
    dataset_id = uuid.uuid4().hex[:12]

    if suffix in CSV_SUFFIXES:
        return _ingest_tabular(source, name, dataset_id, data_dir, kind="csv")
    if suffix in EXCEL_SUFFIXES:
        return _ingest_tabular(source, name, dataset_id, data_dir, kind="excel")
    if suffix in DUMP_SUFFIXES:
        if not postgres_admin_url:
            raise IngestError(
                "A PostgreSQL dump needs a server to restore into. "
                "Set SQLAGENT_POSTGRES_ADMIN_URL."
            )
        return _ingest_dump(source, name, dataset_id, postgres_admin_url)

    raise IngestError(
        f"Unsupported file type '{suffix}'. "
        f"Supported: {', '.join(sorted(CSV_SUFFIXES | EXCEL_SUFFIXES | DUMP_SUFFIXES))}."
    )


def _ingest_tabular(
    source: Path, name: str, dataset_id: str, data_dir: Path, *, kind: str
) -> Dataset:
    """Load a CSV or workbook into a fresh SQLite file."""
    target = data_dir / f"{dataset_id}.sqlite"
    engine = create_engine(f"sqlite:///{target}")

    frames: dict[str, pd.DataFrame] = {}
    try:
        if kind == "csv":
            frames[safe_table_name(name)] = _read_csv(source)
        else:
            workbook = pd.read_excel(source, sheet_name=None)
            for sheet, frame in workbook.items():
                if not frame.empty:
                    frames[safe_table_name(sheet)] = frame
    except Exception as exc:  # noqa: BLE001 - pandas raises many distinct types
        raise IngestError(f"Could not read the file: {exc}") from exc

    if not frames:
        raise IngestError("The file contained no readable data.")

    tables: list[str] = []
    row_counts: dict[str, int] = {}

    for table, frame in frames.items():
        frame = _normalise_columns(frame)
        frame.to_sql(table, engine, index=False, if_exists="replace")
        tables.append(table)
        row_counts[table] = len(frame)

    engine.dispose()

    note = (
        "Column types were inferred from the data. "
        "This file declares no foreign keys, so tables cannot be joined until "
        "a relationship is declared."
        if len(tables) > 1
        else "Column types were inferred from the data."
    )

    return Dataset(
        id=dataset_id,
        name=name,
        kind=kind,
        database_url=f"sqlite:///{target}",
        tables=sorted(tables),
        row_counts=row_counts,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        note=note,
    )


def _ingest_dump(source: Path, name: str, dataset_id: str, admin_url: str) -> Dataset:
    """Restore a PostgreSQL dump into its own database.

    Both dump formats are handled: plain SQL (``pg_dump`` default) via ``psql``,
    and the custom/archive format via ``pg_restore``. The format is detected
    from the first bytes rather than the file extension, which is frequently
    wrong.
    """
    database = f"upload_{dataset_id}"

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            # The identifier is generated here from a hex UUID, never from user
            # input, so interpolation is safe. CREATE DATABASE cannot take a
            # bind parameter.
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"Could not create a database for the dump: {exc}") from exc
    finally:
        admin.dispose()

    target_url = _swap_database(admin_url, database)
    dsn = _libpq_dsn(target_url)

    header = source.read_bytes()[:5]
    is_archive = header.startswith(b"PGDMP")

    command = (
        ["pg_restore", "--no-owner", "--no-privileges", "--dbname", dsn, str(source)]
        if is_archive
        else ["psql", "--quiet", "--dbname", dsn, "--file", str(source)]
    )

    if shutil.which(command[0]) is None:
        raise IngestError(f"{command[0]} is not installed on the server.")

    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)

    # A dump written by a different server version routinely emits warnings and
    # non-fatal errors (missing roles, unknown extensions) while still restoring
    # the data. So success is judged by whether tables exist, not by exit code.
    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            ).fetchall()
            tables = [row[0] for row in rows]

            counts: dict[str, int] = {}
            for table in tables:
                counts[table] = connection.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar_one()
    finally:
        engine.dispose()

    if not tables:
        detail = (result.stderr or result.stdout or "")[-400:]
        raise IngestError(f"The dump restored no tables. Output: {detail}")

    note = f"Restored {len(tables)} tables."
    if result.returncode != 0:
        note += " Some statements failed; check that the data looks complete."

    return Dataset(
        id=dataset_id,
        name=name,
        kind="pg_dump",
        database_url=target_url,
        tables=tables,
        row_counts=counts,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        note=note,
    )


def add_relationship(
    dataset: Dataset,
    *,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
) -> None:
    """Declare a foreign key on an uploaded dataset.

    CSVs and workbooks carry no relationships, so the schema graph sees isolated
    tables and the agent cannot join them. This records one explicitly.

    Relationships are *declared, never inferred*. A guess based on matching
    column names would sometimes be wrong, and a wrong join returns plausible
    rows rather than an error — the single worst failure mode in this system.

    SQLite cannot add a foreign key to an existing table, so the constraint is
    recorded in a side table that schema loading reads. For PostgreSQL datasets
    a real constraint is added.
    """
    engine = create_engine(dataset.database_url)
    try:
        with engine.begin() as connection:
            if dataset.database_url.startswith("sqlite"):
                connection.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS _sqlagent_relationships ("
                        "source_table TEXT, source_column TEXT, "
                        "target_table TEXT, target_column TEXT)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO _sqlagent_relationships VALUES "
                        "(:st, :sc, :tt, :tc)"
                    ),
                    {
                        "st": source_table,
                        "sc": source_column,
                        "tt": target_table,
                        "tc": target_column,
                    },
                )
            else:
                connection.execute(
                    text(
                        f'ALTER TABLE "{source_table}" ADD FOREIGN KEY ("{source_column}") '
                        f'REFERENCES "{target_table}" ("{target_column}")'
                    )
                )
    finally:
        engine.dispose()


def _swap_database(url: str, database: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


def _libpq_dsn(sqlalchemy_url: str) -> str:
    """Convert a SQLAlchemy URL into one libpq understands.

    ``postgresql+psycopg://`` is SQLAlchemy's way of naming a driver; the
    command-line tools only know ``postgresql://``.
    """
    return re.sub(r"^postgresql\+\w+://", "postgresql://", sqlalchemy_url)
