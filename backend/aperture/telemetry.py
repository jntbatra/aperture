"""Per-run telemetry.

The graph already produces everything worth measuring -- which path it took,
how many repairs it needed, what it cost. Persisting it turns "it seems to
work" into answerable questions: how often does the repair loop fire, what
share of answers carry a caveat, where does the latency actually go.

Storage is a local SQLite file, so this costs nothing to run and cannot become
another service to keep alive.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL    NOT NULL,
    finished_at   REAL    NOT NULL,
    duration_ms   REAL    NOT NULL,
    dataset       TEXT,
    dialect       TEXT,
    question      TEXT    NOT NULL,
    intent        TEXT,
    status        TEXT,
    sql           TEXT,
    attempts      INTEGER DEFAULT 0,
    repaired      INTEGER DEFAULT 0,
    row_count     INTEGER DEFAULT 0,
    linked_tables INTEGER DEFAULT 0,
    tokens        INTEGER DEFAULT 0,
    cost_usd      REAL    DEFAULT 0,
    caveats       TEXT,
    path          TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS runs_started_at ON runs (started_at);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
"""


def telemetry_path() -> Path:
    home = Path(os.path.expanduser(settings().home_dir))
    home.mkdir(parents=True, exist_ok=True)
    return home / "telemetry.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(telemetry_path()))
    connection.executescript(SCHEMA)
    return connection


def record_run(state: dict, *, dataset: str, dialect: str, cost_usd: float) -> None:
    """Persist one answered question. Never raises: telemetry is not the product."""
    try:
        started = float(state.get("started_at") or time.time())
        finished = time.time()
        trace = state.get("trace") or []
        connection = connect()
        with connection:
            connection.execute(
                """
                INSERT INTO runs (
                    started_at, finished_at, duration_ms, dataset, dialect, question,
                    intent, status, sql, attempts, repaired, row_count, linked_tables,
                    tokens, cost_usd, caveats, path, error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    started,
                    finished,
                    (finished - started) * 1000,
                    dataset,
                    dialect,
                    state.get("question", ""),
                    state.get("intent"),
                    state.get("status"),
                    state.get("sql"),
                    int(state.get("attempts") or 0),
                    len(state.get("identifier_fixes") or []),
                    int(state.get("row_count") or 0),
                    len(state.get("linked_tables") or []),
                    int(state.get("tokens_used") or 0),
                    cost_usd,
                    json.dumps([f["kind"] for f in state.get("verification", [])]),
                    " -> ".join(step.get("node", "") for step in trace),
                    state.get("last_error") or None,
                ),
            )
        connection.close()
    except Exception as err:
        log.debug("telemetry write failed: %s", err)


@dataclass
class Stats:
    runs: int = 0
    by_status: dict[str, int] = None
    median_ms: float = 0.0
    p95_ms: float = 0.0
    repair_rate: float = 0.0
    identifier_repair_rate: float = 0.0
    caveat_rate: float = 0.0
    empty_rate: float = 0.0
    total_cost: float = 0.0
    tokens: int = 0


def summarise(limit: int = 500) -> Stats:
    connection = connect()
    rows = connection.execute(
        "SELECT status, duration_ms, attempts, repaired, caveats, row_count, cost_usd, tokens"
        " FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    connection.close()

    stats = Stats(by_status={})
    if not rows:
        return stats

    durations = sorted(row[1] for row in rows)
    stats.runs = len(rows)
    stats.median_ms = durations[len(durations) // 2]
    stats.p95_ms = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
    for row in rows:
        stats.by_status[row[0] or "unknown"] = stats.by_status.get(row[0] or "unknown", 0) + 1
    stats.repair_rate = sum(1 for row in rows if row[2] > 0) / len(rows)
    stats.identifier_repair_rate = sum(1 for row in rows if row[3] > 0) / len(rows)
    stats.caveat_rate = sum(1 for row in rows if row[4] and row[4] != "[]") / len(rows)
    stats.empty_rate = sum(1 for row in rows if row[5] == 0) / len(rows)
    stats.total_cost = sum(row[6] or 0 for row in rows)
    stats.tokens = sum(row[7] or 0 for row in rows)
    return stats
