"""Question cache.

A demo asks the same question repeatedly, and so does anyone exploring a
dataset. Caching the *query* rather than the rows is the important detail: the
expensive part is choosing the SQL, while the data underneath can change
between two identical questions, so the cached query is always re-executed.

The key includes the schema fingerprint, so adding a column invalidates
everything that was written against the old shape.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass

from .config import settings
from .store import store_path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS query_cache (
    key          TEXT PRIMARY KEY,
    dataset      TEXT NOT NULL,
    question     TEXT NOT NULL,
    sql          TEXT NOT NULL,
    assumptions  TEXT,
    created_at   REAL NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS query_cache_dataset ON query_cache (dataset);
"""


@dataclass
class CachedQuery:
    sql: str
    assumptions: str
    age_seconds: float
    hits: int


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(store_path()), check_same_thread=False)
    connection.executescript(SCHEMA)
    return connection


def normalise(question: str) -> str:
    return " ".join(question.lower().split()).strip(" ?.!")


def key_for(question: str, dataset: str, schema_version: str) -> str:
    raw = f"{normalise(question)}|{dataset}|{schema_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def lookup(question: str, dataset: str, schema_version: str) -> CachedQuery | None:
    if not settings().cache_enabled:
        return None
    key = key_for(question, dataset, schema_version)
    connection = _connect()
    row = connection.execute(
        "SELECT sql, assumptions, created_at, hits FROM query_cache WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        connection.close()
        return None

    age = time.time() - row[2]
    if age > settings().cache_ttl_seconds:
        with connection:
            connection.execute("DELETE FROM query_cache WHERE key = ?", (key,))
        connection.close()
        return None

    with connection:
        connection.execute("UPDATE query_cache SET hits = hits + 1 WHERE key = ?", (key,))
    connection.close()
    return CachedQuery(sql=row[0], assumptions=row[1] or "", age_seconds=age, hits=row[3] + 1)


def remember(question: str, dataset: str, schema_version: str, sql: str, assumptions: str = "") -> None:
    if not settings().cache_enabled or not sql:
        return
    connection = _connect()
    with connection:
        connection.execute(
            """
            INSERT INTO query_cache (key, dataset, question, sql, assumptions, created_at, hits)
            VALUES (?,?,?,?,?,?,0)
            ON CONFLICT (key) DO UPDATE SET sql = excluded.sql, created_at = excluded.created_at
            """,
            (
                key_for(question, dataset, schema_version),
                dataset,
                question,
                sql,
                assumptions,
                time.time(),
            ),
        )
    connection.close()


def clear(dataset: str = "") -> int:
    connection = _connect()
    with connection:
        cursor = (
            connection.execute("DELETE FROM query_cache WHERE dataset = ?", (dataset,))
            if dataset
            else connection.execute("DELETE FROM query_cache")
        )
    connection.close()
    return cursor.rowcount
