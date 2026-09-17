"""Database handle: connects, describes its dialect, runs read-only queries.

One class covers Postgres, SQLite and MySQL. Everything dialect-specific is
isolated in small helpers here so the rest of Aperture stays dialect-agnostic.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from ..config import settings

SUPPORTED_DIALECTS = {"postgresql", "sqlite", "mysql"}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    elapsed_ms: float
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class Database:
    url: str
    engine: Engine = field(init=False, repr=False)
    dialect: str = field(init=False)

    def __post_init__(self) -> None:
        url = make_url(self.url)
        backend = url.get_backend_name()
        if backend not in SUPPORTED_DIALECTS:
            raise ValueError(
                f"unsupported dialect {backend!r}; supported: {sorted(SUPPORTED_DIALECTS)}"
            )
        self.dialect = backend
        self.engine = create_engine(self.url, pool_pre_ping=True, future=True)

    @classmethod
    def from_settings(cls) -> "Database":
        return cls(settings().database_url)

    @property
    def sqlglot_dialect(self) -> str:
        """sqlglot spells Postgres differently from SQLAlchemy."""
        return {"postgresql": "postgres", "sqlite": "sqlite", "mysql": "mysql"}[self.dialect]

    @property
    def fingerprint(self) -> str:
        """Stable id for this database, used as the schema cache key."""
        url = make_url(self.url)
        ident = f"{url.get_backend_name()}:{url.host}:{url.port}:{url.database}"
        return hashlib.sha256(ident.encode()).hexdigest()[:16]

    def _apply_session_guards(self, conn) -> None:
        """Belt-and-braces timeouts. The read-only role is the actual guard."""
        timeout = settings().statement_timeout_ms
        if self.dialect == "postgresql":
            conn.execute(text(f"SET statement_timeout = {timeout}"))
            conn.execute(text("SET TRANSACTION READ ONLY"))
        elif self.dialect == "mysql":
            conn.execute(text(f"SET SESSION max_execution_time = {timeout}"))

    def run(self, sql: str, *, max_rows: int | None = None) -> QueryResult:
        """Execute `sql` read-only and fetch at most `max_rows` rows."""
        limit = max_rows or settings().row_limit
        started = time.perf_counter()
        with self.engine.connect() as conn:
            self._apply_session_guards(conn)
            cursor = conn.execute(text(sql))
            columns = list(cursor.keys())
            rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        elapsed_ms = (time.perf_counter() - started) * 1000
        return QueryResult(
            columns=columns,
            rows=[tuple(r) for r in rows[:limit]],
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        )

    def scalar(self, sql: str) -> Any:
        with self.engine.connect() as conn:
            self._apply_session_guards(conn)
            return conn.execute(text(sql)).scalar()

    def dispose(self) -> None:
        self.engine.dispose()
