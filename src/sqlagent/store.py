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

from sqlalchemy import Engine, create_engine, text

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at        TEXT    NOT NULL,
    question        TEXT    NOT NULL,
    answer          TEXT,
    sql             TEXT,
    dataset_id      TEXT,
    conversation_id TEXT,
    result_preview  TEXT,
    ok              INTEGER NOT NULL,
    error           TEXT,
    row_count       INTEGER,
    seconds         REAL,
    model_calls     INTEGER,
    tokens          INTEGER,
    repairs         INTEGER
);

CREATE INDEX IF NOT EXISTS history_asked_at ON history (asked_at DESC);

CREATE INDEX IF NOT EXISTS history_conversation ON history (conversation_id, id);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title      TEXT NOT NULL,
    dataset_id TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    database_url TEXT NOT NULL,
    tables       TEXT NOT NULL,
    row_counts   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT
);
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


_HISTORY_COLUMNS = (
    "id, asked_at, question, answer, sql, dataset_id, conversation_id, result_preview, "
    "ok, error, row_count, seconds, model_calls, tokens, repairs"
)
"""The SELECT list for history rows, named once.

It was previously written out at each of three query sites, which is exactly the
shape of duplication that breaks when a column is added: the row is unpacked
positionally, so one site left un-updated does not error — it silently assigns
the wrong value to every field after the new column.
"""


def _to_entry(row) -> HistoryEntry:
    """Map one row of ``_HISTORY_COLUMNS`` onto a :class:`HistoryEntry`."""
    return HistoryEntry(
        id=row[0], asked_at=row[1], question=row[2], answer=row[3], sql=row[4],
        dataset_id=row[5], conversation_id=row[6],
        result_preview=json.loads(row[7]) if row[7] else None,
        ok=bool(row[8]), error=row[9], row_count=row[10], seconds=row[11],
        model_calls=row[12], tokens=row[13], repairs=row[14],
    )


class Store:
    """Conversations, their turns, and dataset records, in one SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(f"sqlite:///{self.path}")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._engine.begin() as connection:
            # Migrate *first*. `SCHEMA` includes an index over conversation_id,
            # and creating it against a pre-conversation table fails with
            # "no such column" — so the column has to exist before the schema
            # statements run, not after them.
            self._migrate(connection)
            for statement in filter(None, (s.strip() for s in SCHEMA.split(";"))):
                connection.execute(text(statement))

    @staticmethod
    def _migrate(connection) -> None:
        """Bring an older store file up to the current shape.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
        exists, so a store written before a column was added keeps the old
        shape — and every insert then fails. Adding the columns explicitly is
        the migration.

        Column checks rather than a version number: every change so far has been
        an added nullable column, for which "is it there?" is both the test and
        the precondition of the fix. A migration framework would be more
        machinery than the problem. That stops being true the first time a
        column needs a backfill or a type change.
        """
        existing = {
            row[1]  # PRAGMA table_info: (cid, name, type, notnull, default, pk)
            for row in connection.execute(text("PRAGMA table_info(history)")).fetchall()
        }
        if not existing:
            return
        for column in ("conversation_id", "result_preview"):
            if column not in existing:
                logger.info("migrating history table: adding %s", column)
                connection.execute(
                    text(f"ALTER TABLE history ADD COLUMN {column} TEXT")
                )

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
    ) -> int:
        """Append one question. Returns its id.

        Failures are recorded too. A question that could not be answered is
        often the most useful thing in the log — it is what tells you the schema
        is confusing or the phrasing was ambiguous.
        """
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    "INSERT INTO history (asked_at, question, answer, sql, dataset_id, "
                    "conversation_id, result_preview, ok, error, row_count, "
                    "seconds, model_calls, tokens, repairs) "
                    "VALUES (:asked_at, :question, :answer, :sql, :dataset_id, "
                    ":conversation_id, :result_preview, :ok, :error, :row_count, "
                    ":seconds, :model_calls, :tokens, :repairs)"
                ),
                {
                    "asked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "question": question,
                    "answer": answer,
                    "sql": sql,
                    "dataset_id": dataset_id,
                    "conversation_id": conversation_id,
                    "result_preview": json.dumps(result_preview) if result_preview else None,
                    "ok": 1 if ok else 0,
                    "error": error,
                    "row_count": row_count,
                    "seconds": seconds,
                    "model_calls": model_calls,
                    "tokens": tokens,
                    "repairs": repairs,
                },
            )
            return int(result.lastrowid or 0)

    def search_turns(self, term: str, limit: int = 50) -> list[HistoryEntry]:
        """Find past questions containing a term, newest first.

        Substring matching over the question text. Not full-text search: the
        corpus is one person's questions, where scanning a few thousand short
        strings is instant and an FTS index would be more machinery than the
        problem needs.

        Kept after the flat history view was removed because search is the one
        thing that view was genuinely good at — finding the query you wrote last
        week. It now returns turns so the caller can jump to the conversation
        they belong to, which is the context that makes them readable.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {_HISTORY_COLUMNS} "
                    f"FROM history WHERE question LIKE :term "
                    f"ORDER BY id DESC LIMIT :limit"
                ),
                {"term": f"%{term}%", "limit": limit},
            ).fetchall()

        return [_to_entry(row) for row in rows]

    def recent_turns(self, limit: int = 400) -> list[HistoryEntry]:
        """The most recent questions, newest first.

        Used for drift detection, which needs the raw per-question record
        rather than the aggregate :meth:`stats` returns — an average over every
        question ever asked cannot show that the last hundred went differently
        from the hundred before them.

        Ordered by id, not by ``asked_at``: timestamps are stored to the second
        and several questions in one second would tie, which puts them in
        arbitrary order and quietly mixes the two periods being compared.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {_HISTORY_COLUMNS} FROM history ORDER BY id DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).fetchall()

        return [_to_entry(row) for row in rows]

    def stats(self) -> dict:
        """Aggregate numbers across every question ever asked."""
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT count(*), sum(ok), avg(seconds), sum(tokens) FROM history"
                )
            ).fetchone()

        total = row[0] or 0
        return {
            "total": total,
            "successful": row[1] or 0,
            "success_rate": (row[1] or 0) / total if total else 0.0,
            "mean_seconds": round(row[2], 2) if row[2] else 0.0,
            "total_tokens": row[3] or 0,
        }

    # ------------------------------------------------------------------
    # Conversations
    #
    # A conversation is a thread of questions where each may refer back to the
    # ones before it. It is stored server-side rather than held by the browser
    # for two reasons: a reload must not lose the thread, and the turns fed to
    # the model then come from the same record the history view shows, so the
    # two cannot disagree about what was asked.
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        """Timestamp for conversations, to microsecond precision.

        History rows are stored to the second — they are ordered by their
        autoincrement id, so the timestamp is only ever displayed. Conversations
        are ordered *by* their timestamp, and two threads started in the same
        second then tie and come back in arbitrary order. The extra digits are
        what makes "most recent first" actually mean that.
        """
        return datetime.now(UTC).isoformat()

    def create_conversation(self, *, dataset_id: str | None = None, title: str = "") -> str:
        """Start a thread and return its id."""
        conversation_id = uuid.uuid4().hex[:16]
        now = self._now()
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversations (id, started_at, updated_at, title, dataset_id) "
                    "VALUES (:id, :now, :now, :title, :dataset_id)"
                ),
                {
                    "id": conversation_id,
                    "now": now,
                    "title": title or "New conversation",
                    "dataset_id": dataset_id,
                },
            )
        return conversation_id

    def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None:
        """Mark a thread as just used, and name it if it has no name yet.

        The title is the first question asked, which is both the cheapest
        summary available and the one a person recognises — the thread they
        remember as "the one about refunds" begins with a question about
        refunds. Later questions do not rename it: a thread whose label moves
        as it grows cannot be found again.
        """
        now = self._now()
        with self._engine.begin() as connection:
            connection.execute(
                text("UPDATE conversations SET updated_at = :now WHERE id = :id"),
                {"now": now, "id": conversation_id},
            )
            if title:
                connection.execute(
                    text(
                        "UPDATE conversations SET title = :title "
                        "WHERE id = :id AND (title = '' OR title = 'New conversation')"
                    ),
                    {"title": title[:120], "id": conversation_id},
                )

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, started_at, updated_at, title, dataset_id "
                    "FROM conversations WHERE id = :id"
                ),
                {"id": conversation_id},
            ).fetchone()

        if row is None:
            return None
        return {
            "id": row[0],
            "started_at": row[1],
            "updated_at": row[2],
            "title": row[3],
            "dataset_id": row[4],
        }

    def list_conversations(self, limit: int = 50) -> list[dict]:
        """Threads, most recently used first, each with its question count."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.id, c.started_at, c.updated_at, c.title, c.dataset_id, "
                    "       count(h.id) "
                    "FROM conversations c "
                    "LEFT JOIN history h ON h.conversation_id = c.id "
                    "GROUP BY c.id "
                    "ORDER BY c.updated_at DESC, c.rowid DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).fetchall()

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

    def conversation_entries(self, conversation_id: str, limit: int = 200) -> list[HistoryEntry]:
        """Every turn in one thread, **oldest first**.

        Ascending order, unlike the history list: a transcript reads forwards,
        and the model needs the turns in the order they happened for "the
        previous one" to mean anything.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {_HISTORY_COLUMNS} FROM history "
                    f"WHERE conversation_id = :id ORDER BY id ASC LIMIT :limit"
                ),
                {"id": conversation_id, "limit": limit},
            ).fetchall()

        return [_to_entry(row) for row in rows]

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a thread and every question in it.

        The questions go too. A history entry whose thread is gone has lost the
        context that made it readable — half of them are fragments like "and for
        April?" that mean nothing alone.
        """
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM history WHERE conversation_id = :id"),
                {"id": conversation_id},
            )
            result = connection.execute(
                text("DELETE FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def save_dataset(self, dataset) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO datasets "
                    "(id, name, kind, database_url, tables, row_counts, created_at, note) "
                    "VALUES (:id, :name, :kind, :url, :tables, :counts, :created, :note)"
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
                },
            )

    def list_datasets(self) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, name, kind, database_url, tables, row_counts, created_at, note "
                    "FROM datasets ORDER BY created_at DESC"
                )
            ).fetchall()

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

    def get_dataset(self, dataset_id: str) -> dict | None:
        for dataset in self.list_datasets():
            if dataset["id"] == dataset_id:
                return dataset
        return None

    def delete_dataset(self, dataset_id: str) -> bool:
        """Forget a dataset, and delete its SQLite file if it has one.

        A PostgreSQL-backed dataset leaves its database in place: dropping
        someone's restored database as a side effect of a UI click is not a
        decision this code should make on its own.
        """
        dataset = self.get_dataset(dataset_id)
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
            connection.execute(
                text("DELETE FROM datasets WHERE id = :id"), {"id": dataset_id}
            )
        return True

    def close(self) -> None:
        self._engine.dispose()
