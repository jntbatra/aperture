"""Named connections.

A tool people actually use points at more than one thing: a production replica,
a staging copy, three CSVs someone sent over email. The registry keeps those as
names rather than connection strings pasted into a shell, records which one is
active, and never stores a password that was not already in the URL the user
supplied.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class Connection:
    name: str
    url: str
    kind: str = "database"  # database | csv
    created_at: float = field(default_factory=time.time)
    source: str = ""
    table: str = ""

    @property
    def safe_url(self) -> str:
        """The URL with any password removed, for display."""
        try:
            return make_url(self.url).render_as_string(hide_password=True)
        except Exception:
            return self.url

    @property
    def dialect(self) -> str:
        try:
            return make_url(self.url).get_backend_name()
        except Exception:
            return "unknown"


@dataclass
class Registry:
    path: Path
    connections: dict[str, Connection] = field(default_factory=dict)
    active: str = ""

    @classmethod
    def load(cls) -> Registry:
        path = Path(os.path.expanduser(settings().home_dir)) / "connections.json"
        registry = cls(path=path)
        if not path.exists():
            return registry
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            log.warning("ignoring unreadable connection registry: %s", err)
            return registry
        registry.active = data.get("active", "")
        for name, body in (data.get("connections") or {}).items():
            registry.connections[name] = Connection(name=name, **body)
        return registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active": self.active,
            "connections": {
                name: {k: v for k, v in asdict(connection).items() if k != "name"}
                for name, connection in self.connections.items()
            },
        }
        self.path.write_text(json.dumps(payload, indent=2))

    def add(self, connection: Connection, *, activate: bool = True) -> Connection:
        self.connections[connection.name] = connection
        if activate or not self.active:
            self.active = connection.name
        self.save()
        return connection

    def remove(self, name: str) -> bool:
        if name not in self.connections:
            return False
        del self.connections[name]
        if self.active == name:
            self.active = next(iter(self.connections), "")
        self.save()
        return True

    def use(self, name: str) -> Connection | None:
        connection = self.connections.get(name)
        if connection:
            self.active = name
            self.save()
        return connection

    def current(self) -> Connection | None:
        return self.connections.get(self.active)


def active_url() -> str:
    """URL of the active connection, falling back to the configured database."""
    current = Registry.load().current()
    return current.url if current else settings().database_url


def active_name() -> str:
    current = Registry.load().current()
    return current.name if current else "configured database"
