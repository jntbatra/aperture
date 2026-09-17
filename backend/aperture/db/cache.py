"""On-disk cache for the schema snapshot and data profile.

Profiling issues an exact `count(*)` per table plus a min/max query per table
with temporal columns. That is fine once and wasteful on every question, so the
result is cached per database fingerprint and refreshed on demand.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .connection import Database
from .introspect import SchemaSnapshot, introspect
from .profile import DatabaseProfile, profile_database

log = logging.getLogger(__name__)

CACHE_VERSION = 1


@dataclass
class SchemaBundle:
    snapshot: SchemaSnapshot
    profile: DatabaseProfile
    built_at: float
    from_cache: bool = False

    @property
    def age_seconds(self) -> float:
        return time.time() - self.built_at


def cache_dir() -> Path:
    path = Path(os.path.expanduser(settings().home_dir)) / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(db: Database) -> Path:
    return cache_dir() / f"schema-{db.fingerprint}.json"


def load_schema(db: Database, *, refresh: bool = False, schema: str = "public") -> SchemaBundle:
    path = cache_path(db)

    if not refresh and path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("version") == CACHE_VERSION:
                return SchemaBundle(
                    snapshot=SchemaSnapshot.from_dict(data["snapshot"]),
                    profile=DatabaseProfile.from_dict(data["profile"]),
                    built_at=data["built_at"],
                    from_cache=True,
                )
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            log.warning("ignoring unreadable schema cache %s: %s", path, err)

    snapshot = introspect(db, schema=schema)
    profile = profile_database(db, snapshot, schema=schema)
    built_at = time.time()
    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "built_at": built_at,
                "snapshot": snapshot.to_dict(),
                "profile": profile.to_dict(),
            }
        )
    )
    return SchemaBundle(snapshot=snapshot, profile=profile, built_at=built_at)
