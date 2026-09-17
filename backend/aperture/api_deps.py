"""Shared request-scoped dependencies: the user, and their analyst graph.

Connections live in two places by design. A signed-in user's connections are
rows in the app store, isolated by user id. The signed-out local user's
connections are the CLI's registry file, so `aperture load` in a terminal and
the web app show the same datasets -- which is the whole point of a local-first
tool.
"""

from __future__ import annotations

import logging
import threading

from .config import settings
from .db import Database
from .graph import build_analyst
from .graph.nodes import AnalystContext
from .ingest import IngestResult
from .registry import Connection, Registry
from .store import (
    User,
    add_user_connection,
    delete_user_connection,
    ensure_local_user,
    get_user_connection,
    list_user_connections,
)

log = logging.getLogger(__name__)

# Building a context profiles the database, so graphs are cached per URL.
_GRAPHS: dict[str, tuple] = {}
_LOCK = threading.Lock()


def analyst_for(url: str):
    """Compiled graph and context for a database URL, built once per process.

    When `APERTURE_REQUIRE_READ_ONLY` is set, the role behind the URL is checked
    before a graph is built, and a writable role is refused. The check happens
    here rather than at startup so it covers every connection the process ever
    serves, not just the one it was configured with.
    """
    with _LOCK:
        if url not in _GRAPHS:
            database = Database(url)
            if settings().require_read_only:
                report = database.assert_read_only()
                log.info("read-only verified: %s", report.summary())
            ctx = AnalystContext.create(database)
            _GRAPHS[url] = build_analyst(ctx)
        return _GRAPHS[url]


def forget_analyst(url: str) -> None:
    with _LOCK:
        _GRAPHS.pop(url, None)


def list_connections(user: User) -> list[dict]:
    """Connections as the UI should see them.

    The URL never leaves the server: it carries the database password, and the
    browser has no use for it. Callers that need to connect go through
    `find_connection`, which stays server-side.
    """
    if user.is_anonymous:
        registry = Registry.load()
        return [
            {
                "name": name,
                "kind": connection.kind,
                "dialect": connection.dialect,
                "target": connection.source or connection.safe_url,
            }
            for name, connection in sorted(registry.connections.items())
        ]

    listed = []
    for row in list_user_connections(user.id):
        described = Connection(name=row["name"], url=row["url"])
        listed.append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "dialect": described.dialect,
                "target": row.get("source") or described.safe_url,
            }
        )
    return listed


def find_connection(user: User, name: str) -> dict | None:
    if user.is_anonymous:
        connection = Registry.load().connections.get(name)
        if not connection:
            return None
        return {
            "name": connection.name,
            "url": connection.url,
            "kind": connection.kind,
            "source": connection.source,
        }
    return get_user_connection(user.id, name)


def save_connection(user: User, *, name: str, url: str, kind: str, source: str = "") -> None:
    if user.is_anonymous:
        Registry.load().add(
            Connection(name=name, url=url, kind=kind, source=source), activate=True
        )
        return
    add_user_connection(user.id, name=name, url=url, kind=kind, source=source)


def remove_connection(user: User, name: str) -> bool:
    if user.is_anonymous:
        return Registry.load().remove(name)
    return delete_user_connection(user.id, name)


def default_connection(user: User) -> dict | None:
    """The connection a new chat should use when none was chosen.

    Server-side only: the result carries a URL, so it must not be returned to a
    browser unfiltered.
    """
    if user.is_anonymous:
        current = Registry.load().current()
        if current:
            return {"name": current.name, "url": current.url, "kind": current.kind}
    named = list_connections(user)
    if named:
        full = find_connection(user, named[0]["name"])
        if full:
            return full
    return {"name": "configured", "url": settings().database_url, "kind": "database"}


def register_upload(user: User, result: IngestResult, *, name: str, source: str, kind: str) -> None:
    save_connection(user, name=name, url=result.database_url, kind=kind, source=source)


def local_user() -> User:
    return ensure_local_user()
