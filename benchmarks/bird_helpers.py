"""The scoring primitives from bird.py, importable without its CLI.

Split out so `rescore.py` can compare result sets without importing a module
that parses arguments and builds a model client at import time.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, text  # noqa: E402

from sqlagent.db.dialects import read_only_url  # noqa: E402


def database_url(data_root: Path, db_id: str) -> str:
    return read_only_url(f"sqlite:///{data_root}/dev_databases/{db_id}/{db_id}.sqlite")


def normalise(value: object) -> object:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        return value.strip()
    return value


def result_signature(rows: list, *, ordered: bool):
    shaped = [tuple(normalise(v) for v in row) for row in rows]
    return shaped if ordered else sorted(shaped, key=repr)


def run_gold(url: str, sql: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(r) for r in connection.execute(text(sql)).fetchall()]
    finally:
        engine.dispose()
