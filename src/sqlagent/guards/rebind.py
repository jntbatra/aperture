"""Repair a filter whose literal does not exist in the column it compares.

The failure
-----------
``WHERE category = 'Cravings Deals'`` against a column storing
``'Cravings Deals ⭐'``. The SQL parses, passes every guard, executes without
error, and returns nothing — and "no rows" was reported to the user as the
answer. Shen et al. (FSE 2026) name this class *Violating Value Specification*
and count it as the largest non-semantic error class in their BIRD taxonomy.

It is invisible to a model reading the SQL. NL2SQL-BUGs measures detection
accuracy for exactly this class, without execution, at 34–49% across six
frontier models. It is trivial to a database: ask whether the value exists.

Why this runs before any model call
-----------------------------------
The literature is consistent that an ungated LLM repair pass is dangerous.
MAGIC applied a repair guideline to every query on BIRD dev and went 56.52 →
46.14. MapleRepair's ungated variant repairs 148 queries and breaks 49; its
deterministically-gated variant repairs 75 and breaks **4**. SIRIUS-SQL found
untyped LLM repair *worse than dropping the candidate outright*, and recovered
223 of 1,043 empty results with structural operators and no model call at all.

So this is deterministic end to end: the database says the literal is absent,
the database proposes the replacement, and the rewrite is accepted only if it
turns an empty result into a non-empty one. Nothing is guessed.

What it deliberately will not do
--------------------------------
Rebind a literal in a query that already returned rows. A query answering the
wrong question with a plausible value is the intent check's problem, and
rewriting a working result on a similarity score is how a repair pass starts
breaking correct queries.
"""

from __future__ import annotations

import difflib
import logging

import sqlglot
from sqlalchemy import text
from sqlglot import exp

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.75
"""How close a candidate must be to the literal the model wrote.

0.75 on :class:`difflib.SequenceMatcher`. ``'Cravings Deals'`` against
``'Cravings Deals ⭐'`` scores 0.93, so a decoration costs little; two
genuinely different category names score far below. Published analogues:
BRIDGE ships 0.85 on ``rapidfuzz.ratio``, CHESS uses 0.3 on the same
``difflib`` ratio as a floor and then keeps only within 0.9x of the best.
"""

MAX_CANDIDATES = 5
MAX_LITERALS = 4
"""A query comparing against more than four absent literals is not a typo."""


def _probe(connection, table: str, column: str, value: str) -> list[str]:
    """Values in this column that contain the literal, or are contained by it.

    ``LIKE '%v%'`` unanchored, which is what E-SQL's candidate-predicate
    generation uses. It finds the decorated form of a value the user spelled
    plainly, which is the direction this failure actually runs in.
    """
    preparer = connection.dialect.identifier_preparer
    quoted_table = preparer.quote(table)
    quoted_column = preparer.quote(column)
    try:
        rows = connection.execute(
            text(
                f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
                f"WHERE {quoted_column} LIKE :pattern AND {quoted_column} IS NOT NULL "
                f"LIMIT :limit"
            ),
            {"pattern": f"%{value}%", "limit": MAX_CANDIDATES},
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        logger.debug("rebind: probe failed on %s.%s: %s", table, column, exc)
        return []
    return [str(row[0]) for row in rows if row[0] is not None]


def _closest(value: str, candidates: list[str]) -> str | None:
    best, score = None, 0.0
    for candidate in candidates:
        ratio = difflib.SequenceMatcher(None, value.lower(), candidate.lower()).ratio()
        if ratio > score:
            best, score = candidate, ratio
    return best if score >= MIN_SIMILARITY else None


def propose(connection, *, sql: str, dialect: str) -> tuple[str, list[str]] | None:
    """Rewrite absent literals to the values the database actually stores.

    Returns ``(rewritten_sql, notes)`` or None when there is nothing to do.
    Never raises: a probe that fails proves nothing and must not fail the
    request that produced the query.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rebind: could not parse: %s", exc)
        return None

    aliases = {
        (t.alias or t.name): t.name for t in statement.find_all(exp.Table) if t.name
    }
    notes: list[str] = []
    changed = 0

    for comparison in statement.find_all(exp.EQ):
        if changed >= MAX_LITERALS:
            break
        column = comparison.find(exp.Column)
        literal = comparison.find(exp.Literal)
        if column is None or literal is None or not literal.is_string or not column.name:
            continue

        table = aliases.get(column.table, column.table) or None
        if table is None:
            # Unqualified in a multi-table query. Resolving it properly needs
            # more schema than belongs here, and guessing rewrites the wrong
            # column's predicate.
            if len(aliases) != 1:
                continue
            table = next(iter(aliases.values()))

        value = literal.this
        if not value or not isinstance(value, str):
            continue

        # Only act when the value is genuinely absent. A value that exists is
        # not this failure, whatever else may be wrong with the query.
        preparer = connection.dialect.identifier_preparer
        try:
            present = connection.execute(
                text(
                    f"SELECT 1 FROM {preparer.quote(table)} "
                    f"WHERE {preparer.quote(column.name)} = :v LIMIT 1"
                ),
                {"v": value},
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.debug("rebind: existence check failed: %s", exc)
            continue
        if present is not None:
            continue

        replacement = _closest(value, _probe(connection, table, column.name, value))
        if replacement is None or replacement == value:
            continue

        literal.set("this", replacement)
        notes.append(f"{table}.{column.name}: {value!r} -> {replacement!r}")
        changed += 1

    if not changed:
        return None
    return statement.sql(dialect=dialect), notes
