"""Load a CSV into a queryable database.

The point is to remove the prerequisite. Aperture already speaks SQLite, so a
spreadsheet becomes a real table with real types, and every other feature --
profiling, linking, guards, charts -- works unchanged.

Types are inferred from the data rather than assumed to be text, because a
column of dates stored as strings makes "last month" unanswerable and a column
of numbers stored as text makes SUM meaningless.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

# How many rows to inspect before deciding a column's type.
SAMPLE_ROWS = 500

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y")
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")
_IDENT = re.compile(r"[^0-9a-zA-Z_]+")
_NULLS = {"", "na", "n/a", "null", "none", "nan", "-"}


@dataclass
class ColumnPlan:
    source: str
    name: str
    sql_type: str

    @property
    def is_temporal(self) -> bool:
        return self.sql_type in {"DATE", "TIMESTAMP"}


@dataclass
class IngestResult:
    database_url: str
    path: Path
    table: str
    rows: int
    columns: list[ColumnPlan] = field(default_factory=list)

    def summary(self) -> str:
        types = ", ".join(f"{c.name} {c.sql_type}" for c in self.columns[:8])
        more = "" if len(self.columns) <= 8 else f" (+{len(self.columns) - 8} more)"
        return f"{self.table}: {self.rows:,} rows · {types}{more}"


def datasets_dir() -> Path:
    path = Path(os.path.expanduser(settings().home_dir)) / "datasets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_identifier(name: str, *, fallback: str) -> str:
    cleaned = _IDENT.sub("_", (name or "").strip()).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    return cleaned


def _is_null(value: str) -> bool:
    return value.strip().lower() in _NULLS


def _parse_number(value: str) -> tuple[bool, bool]:
    """Return (is_number, is_integer) for a raw cell."""
    text = value.strip().replace(",", "")
    if not text:
        return False, False
    try:
        int(text)
        return True, True
    except ValueError:
        pass
    try:
        float(text)
        return True, False
    except ValueError:
        return False, False


def _matches_any(value: str, formats: tuple[str, ...]) -> str | None:
    for fmt in formats:
        try:
            datetime.strptime(value.strip(), fmt)
            return fmt
        except ValueError:
            continue
    return None


def infer_type(values: list[str]) -> str:
    """Infer a SQLite type from sampled cells, conservatively."""
    present = [v for v in values if not _is_null(v)]
    if not present:
        return "TEXT"

    if all(_matches_any(v, _TIMESTAMP_FORMATS) for v in present):
        return "TIMESTAMP"
    if all(_matches_any(v, _DATE_FORMATS) for v in present):
        return "DATE"

    parsed = [_parse_number(v) for v in present]
    if all(is_number for is_number, _ in parsed):
        return "INTEGER" if all(is_int for _, is_int in parsed) else "REAL"

    lowered = {v.strip().lower() for v in present}
    if lowered <= {"true", "false", "yes", "no", "0", "1", "t", "f"}:
        return "BOOLEAN"
    return "TEXT"


def _coerce(value: str, sql_type: str):
    if _is_null(value):
        return None
    text = value.strip()
    if sql_type == "INTEGER":
        return int(text.replace(",", ""))
    if sql_type == "REAL":
        return float(text.replace(",", ""))
    if sql_type == "BOOLEAN":
        return 1 if text.lower() in {"true", "yes", "1", "t"} else 0
    if sql_type in {"DATE", "TIMESTAMP"}:
        fmt = _matches_any(text, _TIMESTAMP_FORMATS) or _matches_any(text, _DATE_FORMATS)
        if not fmt:
            return text
        parsed = datetime.strptime(text, fmt)
        # Store ISO-8601 so SQLite's date functions and our profiler agree.
        return parsed.date().isoformat() if sql_type == "DATE" else parsed.isoformat(sep=" ")
    return text


def plan_columns(header: list[str], sample: list[list[str]]) -> list[ColumnPlan]:
    plans: list[ColumnPlan] = []
    seen: set[str] = set()
    for index, raw in enumerate(header):
        name = safe_identifier(raw, fallback=f"column_{index + 1}")
        while name.lower() in seen:
            name = f"{name}_{index + 1}"
        seen.add(name.lower())
        values = [row[index] for row in sample if index < len(row)]
        plans.append(ColumnPlan(source=raw, name=name, sql_type=infer_type(values)))
    return plans


def load_csv(
    source: str | Path,
    *,
    dataset: str | None = None,
    table: str | None = None,
    delimiter: str | None = None,
) -> IngestResult:
    """Load `source` into a SQLite database and return how to connect to it."""
    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(source)

    dataset = safe_identifier(dataset or source.stem, fallback="dataset")
    table_name = safe_identifier(table or source.stem, fallback="data")
    target = datasets_dir() / f"{dataset}.db"

    with source.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        head = handle.read(64 * 1024)
        handle.seek(0)
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","

        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration as err:
            raise ValueError(f"{source} is empty") from err

        sample: list[list[str]] = []
        for row in reader:
            sample.append(row)
            if len(sample) >= SAMPLE_ROWS:
                break
        columns = plan_columns(header, sample)

        handle.seek(0)
        reader = csv.reader(handle, delimiter=delimiter)
        next(reader, None)

        if target.exists():
            target.unlink()
        connection = sqlite3.connect(str(target))
        try:
            quoted = ", ".join(f'"{c.name}" {c.sql_type}' for c in columns)
            connection.execute(f'CREATE TABLE "{table_name}" ({quoted})')
            placeholders = ", ".join("?" for _ in columns)
            insert = f'INSERT INTO "{table_name}" VALUES ({placeholders})'

            batch: list[tuple] = []
            rows = 0
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                padded = list(row) + [""] * (len(columns) - len(row))
                batch.append(
                    tuple(_coerce(padded[i], c.sql_type) for i, c in enumerate(columns))
                )
                if len(batch) >= 1000:
                    connection.executemany(insert, batch)
                    rows += len(batch)
                    batch.clear()
            if batch:
                connection.executemany(insert, batch)
                rows += len(batch)
            connection.commit()
        finally:
            connection.close()

    return IngestResult(
        database_url=f"sqlite:///{target}",
        path=target,
        table=table_name,
        rows=rows,
        columns=columns,
    )


def dataset_pointer() -> Path:
    return Path(os.path.expanduser(settings().home_dir)) / "current_dataset"


def remember_dataset(url: str) -> None:
    """Record the active dataset so later commands need no arguments."""
    pointer = dataset_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(url)


def active_database_url() -> str:
    """The dataset selected by `aperture load`, else the configured database."""
    pointer = dataset_pointer()
    if pointer.exists():
        url = pointer.read_text().strip()
        if url:
            return url
    return settings().database_url


def forget_dataset() -> None:
    pointer = dataset_pointer()
    if pointer.exists():
        pointer.unlink()
