"""Execution-guided self-consistency.

Nine out of ten benchmark failures were queries that ran cleanly and answered
the wrong question, so validation and repair cannot help: both accept a query
that is merely plausible. Sampling several candidates and letting the database
break the tie attacks that directly.

Candidates are grouped by the result they produce, not by their text -- two
different queries returning the same rows are the same answer, and agreement
between independently written queries is evidence. Ties prefer a non-empty
result, since an empty one is almost never what was asked for.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .db.connection import Database, QueryFailed
from .db.introspect import SchemaSnapshot
from .guards.validator import validate_sql
from .sqlfix import repair_identifiers

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    sql: str
    ok: bool = False
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    error: str = ""
    error_kind: str = ""
    fingerprint: str = ""

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0


@dataclass
class Vote:
    winner: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    agreement: int = 0

    @property
    def considered(self) -> int:
        return len(self.candidates)

    @property
    def executable(self) -> int:
        return sum(1 for c in self.candidates if c.ok)


def fingerprint(columns: list[str], rows: list[list[Any]]) -> str:
    """Identity of a result set, order-insensitive, like the benchmark's scorer."""
    rendered = sorted("\x1f".join("" if v is None else str(v) for v in row) for row in rows)
    digest = hashlib.sha256()
    digest.update(str(len(columns)).encode())
    for row in rendered:
        digest.update(row.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def evaluate(
    db: Database, snapshot: SchemaSnapshot, sql: str, *, dialect: str, row_limit: int
) -> Candidate:
    """Validate, repair and run one candidate. Never raises."""
    candidate = Candidate(sql=sql)
    result = validate_sql(sql, dialect=dialect, row_limit=row_limit)
    if not result.ok:
        candidate.error = result.reason
        candidate.error_kind = result.kind
        return candidate

    repaired = repair_identifiers(result.sql, snapshot, dialect=dialect)
    candidate.sql = repaired.sql
    try:
        output = db.run(candidate.sql)
    except QueryFailed as err:
        candidate.error = err.error.for_prompt()
        candidate.error_kind = err.error.sqlstate or "db_error"
        return candidate
    except Exception as err:  # a bad candidate must not end the question
        candidate.error = str(err)[:200]
        candidate.error_kind = "error"
        return candidate

    candidate.ok = True
    candidate.columns = output.columns
    candidate.rows = [list(row) for row in output.rows]
    candidate.row_count = output.row_count
    candidate.fingerprint = fingerprint(candidate.columns, candidate.rows)
    return candidate


def choose(candidates: list[Candidate]) -> Vote:
    """Pick the result the most candidates agree on."""
    runnable = [c for c in candidates if c.ok]
    if not runnable:
        return Vote(winner=None, candidates=candidates)

    groups: dict[str, list[Candidate]] = {}
    for candidate in runnable:
        groups.setdefault(candidate.fingerprint, []).append(candidate)

    def rank(group: list[Candidate]) -> tuple:
        # Agreement first; then prefer a result that actually has rows, since an
        # empty set is almost never the answer to a question someone asked.
        return (len(group), 0 if group[0].is_empty else 1, group[0].row_count > 0)

    best = max(groups.values(), key=rank)
    return Vote(winner=best[0], candidates=candidates, agreement=len(best))


def gather(
    generate: Callable[[float], str | None],
    temperatures: list[float],
    *,
    db: Database,
    snapshot: SchemaSnapshot,
    dialect: str,
    row_limit: int,
) -> Vote:
    """Generate candidates in parallel, execute each, and vote."""
    with ThreadPoolExecutor(max_workers=min(4, len(temperatures))) as pool:
        drafts = list(pool.map(generate, temperatures))

    seen: set[str] = set()
    candidates: list[Candidate] = []
    for draft in drafts:
        if not draft:
            continue
        key = " ".join(draft.split()).lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            evaluate(db, snapshot, draft, dialect=dialect, row_limit=row_limit)
        )
    return choose(candidates)
