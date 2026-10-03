"""The scoring primitives from bird.py, importable without its CLI.

Split out so `rescore.py` can compare result sets without importing a module
that parses arguments and builds a model client at import time.
"""
from __future__ import annotations

import sys
import time
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


SCORING_TIMEOUT_SECONDS = 30.0
"""The agent's own statement timeout. A query the agent could not have run
in time is not one the scorer should wait on: one LIKE-join prediction
(codebase_community, question 637) ground on for many minutes under
rescore.py and stalled the whole analysis behind it."""


class ScoringTimeout(Exception):
    """A query ran past ``SCORING_TIMEOUT_SECONDS``."""


def run_gold(url: str, sql: str, *, timeout: float = SCORING_TIMEOUT_SECONDS) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            raw = connection.connection.dbapi_connection
            if timeout and hasattr(raw, "set_progress_handler"):
                deadline = time.monotonic() + timeout
                raw.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
            try:
                return [tuple(r) for r in connection.execute(text(sql)).fetchall()]
            except Exception as exc:
                if timeout and "interrupted" in str(exc):
                    raise ScoringTimeout(sql[:80]) from exc
                raise
    finally:
        engine.dispose()
