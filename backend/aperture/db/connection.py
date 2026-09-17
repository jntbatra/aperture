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
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings

SUPPORTED_DIALECTS = {"postgresql", "sqlite", "mysql"}


# SQLSTATE codes the repair loop branches on. Each one implies a different
# fix, and treating them all as "try again" wastes the attempt budget.
SQLSTATE_UNDEFINED_COLUMN = "42703"
SQLSTATE_UNDEFINED_TABLE = "42P01"
SQLSTATE_UNDEFINED_FUNCTION = "42883"
SQLSTATE_INVALID_TEXT_REPRESENTATION = "22P02"
SQLSTATE_QUERY_CANCELED = "57014"
SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"
SQLSTATE_READ_ONLY_TRANSACTION = "25006"


@dataclass
class DbError:
    """A database failure in the form a repair prompt can actually use.

    `str(SQLAlchemyError)` appends a documentation URL and a parameter dump --
    prompt noise that pushes the useful part out of view. Postgres already
    provides the useful part in structured form, including the HINT that names
    the column the model should have used.
    """

    sqlstate: str = ""
    primary: str = ""
    hint: str = ""
    detail: str = ""
    position: str = ""
    raw: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Identity of a failure, for detecting a loop repeating itself."""
        return (self.sqlstate, self.primary)

    @property
    def is_timeout(self) -> bool:
        return self.sqlstate == SQLSTATE_QUERY_CANCELED

    @property
    def is_schema_error(self) -> bool:
        return self.sqlstate in {SQLSTATE_UNDEFINED_COLUMN, SQLSTATE_UNDEFINED_TABLE}

    @property
    def is_bad_literal(self) -> bool:
        return self.sqlstate == SQLSTATE_INVALID_TEXT_REPRESENTATION

    def for_prompt(self) -> str:
        lines = [f"ERROR: {self.primary or self.raw}"]
        if self.detail:
            lines.append(f"DETAIL: {self.detail}")
        if self.hint:
            lines.append(f"HINT: {self.hint}")
        if self.position:
            lines.append(f"POSITION: {self.position}")
        if self.sqlstate:
            lines.append(f"SQLSTATE: {self.sqlstate}")
        return "\n".join(lines)


class QueryFailed(Exception):
    """Raised by `Database.run` carrying a structured `DbError`."""

    def __init__(self, error: DbError):
        super().__init__(error.primary or error.raw)
        self.error = error


def describe_error(exc: Exception) -> DbError:
    """Normalise a driver exception into a `DbError`."""
    orig = getattr(exc, "orig", None) or exc
    diag = getattr(orig, "diag", None)
    if diag is not None:
        return DbError(
            sqlstate=str(getattr(diag, "sqlstate", "") or getattr(orig, "sqlstate", "") or ""),
            primary=str(getattr(diag, "message_primary", "") or "").strip(),
            hint=str(getattr(diag, "message_hint", "") or "").strip(),
            detail=str(getattr(diag, "message_detail", "") or "").strip(),
            position=str(getattr(diag, "statement_position", "") or "").strip(),
            raw=str(orig).strip(),
        )
    # MySQL and SQLite: no structured diagnostics, so the driver message is all
    # there is -- but taking it from `orig` still drops SQLAlchemy's trailing
    # documentation URL.
    return DbError(primary=str(orig).strip(), raw=str(orig).strip())


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
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]


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
    def from_settings(cls) -> Database:
        return cls(settings().database_url)

    @classmethod
    def active(cls) -> Database:
        """The dataset selected by `aperture load`, else the configured database."""
        from ..ingest import active_database_url

        return cls(active_database_url())

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
        try:
            with self.engine.connect() as conn:
                self._apply_session_guards(conn)
                cursor = conn.execute(text(sql))
                columns = list(cursor.keys())
                rows = cursor.fetchmany(limit + 1)
        except SQLAlchemyError as err:
            raise QueryFailed(describe_error(err)) from err
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
