"""Persistent storage for conversations and uploaded datasets.

Why a store at all
------------------
Two things want persistence:

* **Conversations.** A thread of questions where each may refer back to the ones
  before it. It is stored server-side rather than held by the browser for two
  reasons: a reload must not lose the thread, and the turns fed to the model
  then come from the same record the UI shows, so the two cannot disagree about
  what was asked.
* **Datasets.** An uploaded file must still be there after a restart, or the
  upload was pointless.

There was once a third: a flat, searchable list of every question ever asked,
separate from the conversations. It was removed. Half the entries in it were
fragments — "and for April?", "they should be veg" — which mean nothing outside
the thread that gave them a subject, so the list was mostly unreadable rows. The
one thing it did well, finding a query you wrote last week, survives as
``search_turns``, which returns the turn *and* the conversation it belongs to.

The physical table is still called ``history``. Renaming it would need a data
migration to buy nothing but a nicer name.

Why SQLite
----------
A single file, no server, and the agent already depends on SQLAlchemy. The data
is small — one row per question — and the access pattern is "append, then read
the most recent few". Reaching for anything larger would be choosing
infrastructure over a requirement.

The store is deliberately separate from the database under analysis. That
database is read-only and belongs to someone else; writing application state
into it would be both rude and, given the read-only role, impossible.

What is *not* stored
--------------------
Result rows, with one tightly bounded exception.

A question like "list every customer" can return thousands of rows containing
personal data, and keeping them would turn a query log into a shadow copy of
the database. The SQL is kept — it is reproducible and carries no data — along
with row counts and the answer text.

The exception: when a result is small enough to be a lookup (see
``sqlagent.conversation.carryable_result`` — a few cells), its values are kept
so the *next* question in the same conversation can refer to them. Without it,
"what is the name of the item with that ID" has no id to refer to, and the
model invents one — observed on a real database, producing a query that
executed perfectly against a fabricated UUID.

The bound is what keeps this from being a data copy: anything larger than a
handful of cells is stored as a row count and nothing else.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    Engine,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    case,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)

from sqlagent.clarify import CLARIFICATION_ERROR

logger = logging.getLogger(__name__)

_state = MetaData()

# Conversations and their turns. Defined with SQLAlchemy Core so the same code
# runs on SQLite (local, tests) and on Postgres (Neon, where it survives a
# restart). `user_id` scopes every read: one person never sees another's chats.
history = Table(
    "history",
    _state,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("asked_at", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("answer", Text),
    Column("sql", Text),
    Column("dataset_id", Text),
    Column("conversation_id", Text),
    Column("result_preview", Text),
    Column("ok", Integer, nullable=False),
    Column("error", Text),
    Column("row_count", Integer),
    Column("seconds", Float),
    Column("model_calls", Integer),
    Column("tokens", Integer),
    Column("repairs", Integer),
    Column("user_id", Text),
)
Index("history_asked_at", history.c.asked_at)
Index("history_conversation", history.c.conversation_id, history.c.id)
Index("history_user", history.c.user_id)

conversations = Table(
    "conversations",
    _state,
    Column("id", Text, primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("dataset_id", Text),
    Column("user_id", Text),
)
Index("conversations_user", conversations.c.user_id)

# Dataset records stay in the local SQLite file, next to the SQLite files they
# point at: both live on the server's disk, so they come and go together.
DATASETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    database_url TEXT NOT NULL,
    tables       TEXT NOT NULL,
    row_counts   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT,
    user_id      TEXT
)
"""


@dataclass
class HistoryEntry:
    """One turn: a question, what it produced, and what it cost."""

    id: int
    asked_at: str
    question: str
    answer: str | None
    sql: str | None
    dataset_id: str | None
    conversation_id: str | None
    result_preview: dict | None
    ok: bool
    error: str | None
    row_count: int | None
    seconds: float | None
    model_calls: int | None
    tokens: int | None
    repairs: int | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "asked_at": self.asked_at,
            "question": self.question,
            "answer": self.answer,
            "sql": self.sql,
            "dataset_id": self.dataset_id,
            "conversation_id": self.conversation_id,
            "result_preview": self.result_preview,
            "ok": self.ok,
            "error": self.error,
            "row_count": self.row_count,
            "seconds": self.seconds,
            "model_calls": self.model_calls,
            "tokens": self.tokens,
            "repairs": self.repairs,
        }


def _to_entry(row) -> HistoryEntry:
    """Map one ``history`` row onto a :class:`HistoryEntry`, by column name."""
    r = row._mapping
    return HistoryEntry(
        id=r["id"],
        asked_at=r["asked_at"],
        question=r["question"],
        answer=r["answer"],
        sql=r["sql"],
        dataset_id=r["dataset_id"],
        conversation_id=r["conversation_id"],
        result_preview=json.loads(r["result_preview"]) if r["result_preview"] else None,
        ok=bool(r["ok"]),
        error=r["error"],
        row_count=r["row_count"],
        seconds=r["seconds"],
        model_calls=r["model_calls"],
        tokens=r["tokens"],
        repairs=r["repairs"],
    )


def _normalise(url: str) -> str:
    # Neon hands out a plain postgresql:// URL; name the driver we ship.
    return (
        "postgresql+psycopg://" + url.removeprefix("postgresql://")
        if url.startswith("postgresql://")
        else url
    )


class Store:
    """Conversations, their turns, and dataset records.

    Conversations and turns go to ``state_url`` when given (Neon Postgres, so
    they survive a restart), otherwise into the same local SQLite file as the
    dataset records. Every read takes an optional ``user_id``: when given, only
    that person's rows are visible. ``None`` means unscoped, which is what the
    CLI and tests want; the web API always passes one.
    """

    def __init__(self, path: Path | str, *, state_url: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(f"sqlite:///{self.path}")
        self._state_engine: Engine = (
            create_engine(_normalise(state_url), pool_pre_ping=True, pool_recycle=300)
            if state_url
            else self._engine
        )
        self._create_schema()

    def _create_schema(self) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(DATASETS_SCHEMA))
        # Add columns older stores lack *before* creating anything that
        # references them: CREATE TABLE IF NOT EXISTS leaves an old table as it was.
        self._add_missing_columns(self._engine, "datasets", {"user_id"})
        self._add_missing_columns(
            self._state_engine, "history", {"conversation_id", "result_preview", "user_id"}
        )
        self._add_missing_columns(self._state_engine, "conversations", {"user_id"})
        _state.create_all(self._state_engine)
        for table in (history, conversations):
            for index in table.indexes:
                index.create(self._state_engine, checkfirst=True)

    @staticmethod
    def _add_missing_columns(engine: Engine, table: str, wanted: set[str]) -> None:
        """Bring an older table up to the current shape.

        Column checks rather than a version number: every change so far has been
        an added nullable column, for which "is it there?" is both the test and
        the precondition of the fix. That stops being true the first time a
        column needs a backfill or a type change.
        """
        inspector = inspect(engine)
        if not inspector.has_table(table):
            return
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as connection:
            for column in sorted(wanted - existing):
                logger.info("migrating %s: adding %s", table, column)
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))

    @staticmethod
    def _owned(table: Table, user_id: str | None):
        return table.c.user_id == user_id if user_id is not None else text("1 = 1")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def record_question(
        self,
        *,
        question: str,
        answer: str | None,
        sql: str | None,
        ok: bool,
        error: str | None = None,
        dataset_id: str | None = None,
        conversation_id: str | None = None,
        result_preview: dict | None = None,
        row_count: int | None = None,
        seconds: float | None = None,
        model_calls: int | None = None,
        tokens: int | None = None,
        repairs: int | None = None,
        user_id: str | None = None,
    ) -> int:
        """Append one question. Returns its id.

        Failures are recorded too. A question that could not be answered is
        often the most useful thing in the log — it is what tells you the schema
        is confusing or the phrasing was ambiguous.
        """
        with self._state_engine.begin() as connection:
            result = connection.execute(
                insert(history).values(
                    asked_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    question=question,
                    answer=answer,
                    sql=sql,
                    dataset_id=dataset_id,
                    conversation_id=conversation_id,
                    result_preview=json.dumps(result_preview) if result_preview else None,
                    ok=1 if ok else 0,
                    error=error,
                    row_count=row_count,
                    seconds=seconds,
                    model_calls=model_calls,
                    tokens=tokens,
                    repairs=repairs,
                    user_id=user_id,
                )
            )
            return int(result.inserted_primary_key[0])

    def search_turns(
        self, term: str, limit: int = 50, *, user_id: str | None = None
    ) -> list[HistoryEntry]:
        """Find past questions containing a term, newest first.

        Substring matching over the question text. Not full-text search: the
        corpus is one person's questions, where scanning a few thousand short
        strings is instant and an FTS index would be more machinery than the
        problem needs. It returns turns so the caller can jump to the
        conversation they belong to, which is the context that makes them readable.
        """
        stmt = (
            select(history)
            .where(history.c.question.ilike(f"%{term}%"), self._owned(history, user_id))
            .order_by(history.c.id.desc())
            .limit(limit)
        )
        with self._state_engine.connect() as connection:
            return [_to_entry(row) for row in connection.execute(stmt)]

    def recent_turns(self, limit: int = 400, *, user_id: str | None = None) -> list[HistoryEntry]:
        """The most recent questions, newest first.

        Used for drift detection, which needs the raw per-question record
        rather than the aggregate :meth:`stats` returns.

        Ordered by id, not by ``asked_at``: timestamps are stored to the second
        and several questions in one second would tie, which puts them in
        arbitrary order and quietly mixes the two periods being compared.
        """
        stmt = (
            select(history)
            .where(self._owned(history, user_id))
            .order_by(history.c.id.desc())
            .limit(limit)
        )
        with self._state_engine.connect() as connection:
            return [_to_entry(row) for row in connection.execute(stmt)]

    def stats(self, *, user_id: str | None = None) -> dict:
        """Aggregate numbers across every question ever asked.

        A turn where the agent asked a clarifying question is stored with
        ``ok = 0`` — no answer was produced — and is **not** counted against the
        success rate. Clarifications are reported separately, because the rate
        the agent asks at is worth knowing and is not the rate it breaks at.
        """
        stmt = select(
            func.count(),
            func.sum(history.c.ok),
            func.avg(history.c.seconds),
            func.sum(history.c.tokens),
            func.sum(case((history.c.error == CLARIFICATION_ERROR, 1), else_=0)),
        ).where(self._owned(history, user_id))
        with self._state_engine.connect() as connection:
            row = connection.execute(stmt).one()

        total = row[0] or 0
        successful = int(row[1] or 0)
        clarified = int(row[4] or 0)
        # Turns where an answer was actually attempted. A clarification was not
        # an attempt — it is the agent declining to guess.
        attempted = total - clarified

        return {
            "total": total,
            "successful": successful,
            "clarified": clarified,
            "success_rate": successful / attempted if attempted else 0.0,
            "mean_seconds": round(float(row[2]), 2) if row[2] else 0.0,
            "total_tokens": int(row[3] or 0),
        }

    # ------------------------------------------------------------------
    # Conversations
    #
    # A conversation is a thread of questions where each may refer back to the
    # ones before it. It is stored server-side rather than held by the browser
    # for two reasons: a reload must not lose the thread, and the turns fed to
    # the model then come from the same record the UI shows, so the two cannot
    # disagree about what was asked.
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        """Timestamp for conversations, to microsecond precision.

        History rows are stored to the second — they are ordered by their id, so
        the timestamp is only ever displayed. Conversations are ordered *by*
        their timestamp, and two threads started in the same second then tie and
        come back in arbitrary order.
        """
        return datetime.now(UTC).isoformat()

    def create_conversation(
        self, *, dataset_id: str | None = None, title: str = "", user_id: str | None = None
    ) -> str:
        """Start a thread and return its id."""
        conversation_id = uuid.uuid4().hex[:16]
        now = self._now()
        with self._state_engine.begin() as connection:
            connection.execute(
                insert(conversations).values(
                    id=conversation_id,
                    started_at=now,
                    updated_at=now,
                    title=title or "New conversation",
                    dataset_id=dataset_id,
                    user_id=user_id,
                )
            )
        return conversation_id

    def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None:
        """Mark a thread as just used, and name it if it has no name yet.

        The title is the first question asked, which is both the cheapest
        summary available and the one a person recognises. Later questions do
        not rename it: a thread whose label moves as it grows cannot be found again.
        """
        with self._state_engine.begin() as connection:
            connection.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id)
                .values(updated_at=self._now())
            )
            if title:
                connection.execute(
                    update(conversations)
                    .where(
                        conversations.c.id == conversation_id,
                        conversations.c.title.in_(("", "New conversation")),
                    )
                    .values(title=title[:120])
                )

    def get_conversation(self, conversation_id: str, *, user_id: str | None = None) -> dict | None:
        stmt = select(conversations).where(
            conversations.c.id == conversation_id, self._owned(conversations, user_id)
        )
        with self._state_engine.connect() as connection:
            row = connection.execute(stmt).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "started_at": row.started_at,
            "updated_at": row.updated_at,
            "title": row.title,
            "dataset_id": row.dataset_id,
        }

    def list_conversations(self, limit: int = 50, *, user_id: str | None = None) -> list[dict]:
        """Threads, most recently used first, each with its question count."""
        c = conversations
        stmt = (
            select(
                c.c.id,
                c.c.started_at,
                c.c.updated_at,
                c.c.title,
                c.c.dataset_id,
                func.count(history.c.id),
            )
            .select_from(c.outerjoin(history, history.c.conversation_id == c.c.id))
            .where(self._owned(c, user_id))
            .group_by(c.c.id, c.c.started_at, c.c.updated_at, c.c.title, c.c.dataset_id)
            .order_by(c.c.updated_at.desc(), c.c.started_at.desc(), c.c.id.desc())
            .limit(limit)
        )
        with self._state_engine.connect() as connection:
            rows = connection.execute(stmt).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "updated_at": r[2],
                "title": r[3],
                "dataset_id": r[4],
                "turns": r[5],
            }
            for r in rows
        ]

    def conversation_entries(
        self, conversation_id: str, limit: int = 200, *, user_id: str | None = None
    ) -> list[HistoryEntry]:
        """Every turn in one thread, **oldest first**.

        Ascending order, unlike the search results: a transcript reads forwards,
        and the model needs the turns in the order they happened for "the
        previous one" to mean anything.
        """
        stmt = (
            select(history)
            .where(history.c.conversation_id == conversation_id, self._owned(history, user_id))
            .order_by(history.c.id.asc())
            .limit(limit)
        )
        with self._state_engine.connect() as connection:
            return [_to_entry(row) for row in connection.execute(stmt)]

    def delete_conversation(self, conversation_id: str, *, user_id: str | None = None) -> bool:
        """Delete a thread and every question in it.

        The questions go too. A history entry whose thread is gone has lost the
        context that made it readable — half of them are fragments like "and for
        April?" that mean nothing alone.
        """
        if self.get_conversation(conversation_id, user_id=user_id) is None:
            return False
        with self._state_engine.begin() as connection:
            connection.execute(delete(history).where(history.c.conversation_id == conversation_id))
            result = connection.execute(
                delete(conversations).where(conversations.c.id == conversation_id)
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def save_dataset(self, dataset, *, user_id: str | None = None) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO datasets "
                    "(id, name, kind, database_url, tables, row_counts, created_at, note, user_id) "
                    "VALUES (:id, :name, :kind, :url, :tables, :counts, :created, :note, :user)"
                ),
                {
                    "id": dataset.id,
                    "name": dataset.name,
                    "kind": dataset.kind,
                    "url": dataset.database_url,
                    "tables": json.dumps(dataset.tables),
                    "counts": json.dumps(dataset.row_counts),
                    "created": dataset.created_at,
                    "note": dataset.note,
                    "user": user_id,
                },
            )

    def list_datasets(self, *, user_id: str | None = None) -> list[dict]:
        sql = (
            "SELECT id, name, kind, database_url, tables, row_counts, created_at, note "
            "FROM datasets "
        )
        params: dict = {}
        if user_id is not None:
            sql += "WHERE user_id = :user "
            params["user"] = user_id
        with self._engine.connect() as connection:
            rows = connection.execute(text(sql + "ORDER BY created_at DESC"), params).fetchall()

        return [
            {
                "id": r[0],
                "name": r[1],
                "kind": r[2],
                "database_url": r[3],
                "tables": json.loads(r[4]),
                "row_counts": json.loads(r[5]),
                "created_at": r[6],
                "note": r[7],
            }
            for r in rows
        ]

    def get_dataset(self, dataset_id: str, *, user_id: str | None = None) -> dict | None:
        for dataset in self.list_datasets(user_id=user_id):
            if dataset["id"] == dataset_id:
                return dataset
        return None

    def delete_dataset(self, dataset_id: str, *, user_id: str | None = None) -> bool:
        """Forget a dataset, and delete its SQLite file if it has one.

        A PostgreSQL-backed dataset leaves its database in place: dropping
        someone's restored database as a side effect of a UI click is not a
        decision this code should make on its own.
        """
        dataset = self.get_dataset(dataset_id, user_id=user_id)
        if dataset is None:
            return False

        url = dataset["database_url"]
        if url.startswith("sqlite:///"):
            file = Path(url.removeprefix("sqlite:///"))
            try:
                file.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.warning("could not delete %s: %s", file, exc)

        with self._engine.begin() as connection:
            connection.execute(text("DELETE FROM datasets WHERE id = :id"), {"id": dataset_id})
        return True

    def close(self) -> None:
        self._engine.dispose()
        if self._state_engine is not self._engine:
            self._state_engine.dispose()
