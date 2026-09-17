"""Application storage: users, conversations, messages, connections.

Separate from the databases being analysed and from the LangGraph checkpointer:
this is the product's own state. SQLite keeps it a file rather than a service,
which matters for a tool people run locally.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE,
    name        TEXT,
    picture     TEXT,
    provider    TEXT DEFAULT 'local',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    connection    TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    payload         TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS user_connections (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'database',
    source      TEXT,
    created_at  REAL NOT NULL,
    UNIQUE (user_id, name)
);
"""

LOCAL_USER_ID = "local"


def store_path() -> Path:
    home = Path(os.path.expanduser(settings().home_dir))
    home.mkdir(parents=True, exist_ok=True)
    return home / "aperture.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(store_path()), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


@dataclass
class User:
    id: str
    email: str = ""
    name: str = ""
    picture: str = ""
    provider: str = "local"

    @property
    def is_anonymous(self) -> bool:
        return self.provider == "local"


@dataclass
class Conversation:
    id: str
    user_id: str
    title: str
    connection: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0


@dataclass
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


def _new_id() -> str:
    return uuid.uuid4().hex


def ensure_local_user() -> User:
    """The signed-out user. Everything works without an account."""
    return upsert_user(
        User(id=LOCAL_USER_ID, email="", name="Local", provider="local")
    )


def upsert_user(user: User) -> User:
    connection = connect()
    with connection:
        connection.execute(
            """
            INSERT INTO users (id, email, name, picture, provider, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT (id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                picture = excluded.picture,
                provider = excluded.provider
            """,
            (user.id, user.email, user.name, user.picture, user.provider, time.time()),
        )
    connection.close()
    return user


def get_user(user_id: str) -> User | None:
    connection = connect()
    row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    connection.close()
    if not row:
        return None
    return User(
        id=row["id"],
        email=row["email"] or "",
        name=row["name"] or "",
        picture=row["picture"] or "",
        provider=row["provider"] or "local",
    )


# --- conversations -----------------------------------------------------------


def create_conversation(user_id: str, *, title: str = "New chat", connection: str = "") -> Conversation:
    now = time.time()
    conversation = Conversation(
        id=_new_id(), user_id=user_id, title=title, connection=connection,
        created_at=now, updated_at=now,
    )
    db = connect()
    with db:
        db.execute(
            "INSERT INTO conversations (id, user_id, title, connection, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (conversation.id, user_id, title, connection, now, now),
        )
    db.close()
    return conversation


def list_conversations(user_id: str, limit: int = 100) -> list[Conversation]:
    db = connect()
    rows = db.execute(
        """
        SELECT c.*, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS n
        FROM conversations c WHERE c.user_id = ?
        ORDER BY c.updated_at DESC LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    db.close()
    return [
        Conversation(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            connection=row["connection"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["n"],
        )
        for row in rows
    ]


def get_conversation(user_id: str, conversation_id: str) -> Conversation | None:
    db = connect()
    row = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    db.close()
    if not row:
        return None
    return Conversation(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        connection=row["connection"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def rename_conversation(user_id: str, conversation_id: str, title: str) -> bool:
    db = connect()
    with db:
        cursor = db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (title[:120], time.time(), conversation_id, user_id),
        )
    db.close()
    return cursor.rowcount > 0


def set_conversation_connection(user_id: str, conversation_id: str, connection: str) -> bool:
    db = connect()
    with db:
        cursor = db.execute(
            "UPDATE conversations SET connection = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (connection, time.time(), conversation_id, user_id),
        )
    db.close()
    return cursor.rowcount > 0


def delete_conversation(user_id: str, conversation_id: str) -> bool:
    db = connect()
    with db:
        cursor = db.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        )
    db.close()
    return cursor.rowcount > 0


# --- messages ----------------------------------------------------------------


def add_message(
    conversation_id: str, role: str, content: str, payload: dict | None = None
) -> Message:
    now = time.time()
    message = Message(
        id=_new_id(),
        conversation_id=conversation_id,
        role=role,
        content=content,
        payload=payload or {},
        created_at=now,
    )
    db = connect()
    with db:
        db.execute(
            "INSERT INTO messages (id, conversation_id, role, content, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (message.id, conversation_id, role, content, json.dumps(message.payload), now),
        )
        db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    db.close()
    return message


def list_messages(conversation_id: str, limit: int = 200) -> list[Message]:
    db = connect()
    rows = db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    db.close()
    messages = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        messages.append(
            Message(
                id=row["id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                payload=payload,
                created_at=row["created_at"],
            )
        )
    return messages


# --- per-user connections ----------------------------------------------------


def add_user_connection(
    user_id: str, *, name: str, url: str, kind: str = "database", source: str = ""
) -> None:
    db = connect()
    with db:
        db.execute(
            """
            INSERT INTO user_connections (id, user_id, name, url, kind, source, created_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (user_id, name) DO UPDATE SET
                url = excluded.url, kind = excluded.kind, source = excluded.source
            """,
            (_new_id(), user_id, name, url, kind, source, time.time()),
        )
    db.close()


def list_user_connections(user_id: str) -> list[dict]:
    db = connect()
    rows = db.execute(
        "SELECT name, url, kind, source FROM user_connections WHERE user_id = ? ORDER BY name",
        (user_id,),
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def get_user_connection(user_id: str, name: str) -> dict | None:
    db = connect()
    row = db.execute(
        "SELECT name, url, kind, source FROM user_connections WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def delete_user_connection(user_id: str, name: str) -> bool:
    db = connect()
    with db:
        cursor = db.execute(
            "DELETE FROM user_connections WHERE user_id = ? AND name = ?", (user_id, name)
        )
    db.close()
    return cursor.rowcount > 0
