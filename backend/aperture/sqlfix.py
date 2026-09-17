"""Deterministic identifier repair.

Postgres folds unquoted identifiers to lower case, so a Prisma-style schema
(`"createdAt"`) rejects `createdAt` written bare. Models get this wrong
repeatedly, and asking again costs a round trip and often fails the same way.

The schema already says how every identifier is spelled, so this corrects the
AST instead of arguing with the model: a bare identifier that matches a known
column case-insensitively is rewritten to the real spelling and quoted. Only
unambiguous matches are touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .db.introspect import SchemaSnapshot


@dataclass
class IdentifierFix:
    sql: str
    changed: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(self.changed)


def _column_spellings(snapshot: SchemaSnapshot) -> dict[str, set[str]]:
    """lowercase name -> the real spellings it could refer to."""
    spellings: dict[str, set[str]] = {}
    for table in snapshot.tables.values():
        for column in table.columns:
            spellings.setdefault(column.name.lower(), set()).add(column.name)
    return spellings


def _table_spellings(snapshot: SchemaSnapshot) -> dict[str, set[str]]:
    spellings: dict[str, set[str]] = {}
    for name in snapshot.tables:
        spellings.setdefault(name.lower(), set()).add(name)
    return spellings


def repair_identifiers(sql: str, snapshot: SchemaSnapshot, *, dialect: str) -> IdentifierFix:
    """Requote identifiers that only differ from the schema by case."""
    if dialect not in {"postgres", "postgresql"}:
        # Only Postgres folds unquoted identifiers to lower case.
        return IdentifierFix(sql=sql)

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return IdentifierFix(sql=sql)

    columns = _column_spellings(snapshot)
    tables = _table_spellings(snapshot)
    changed: list[str] = []

    for identifier in tree.find_all(exp.Identifier):
        if identifier.quoted:
            continue
        name = identifier.this
        if not isinstance(name, str) or not name:
            continue

        candidates = columns.get(name.lower(), set()) | tables.get(name.lower(), set())
        # Ambiguous matches are left alone: guessing between two spellings is
        # worse than reporting the database's own error.
        if len(candidates) != 1:
            continue

        correct = next(iter(candidates))
        # A mixed-case identifier must be quoted even when spelled correctly:
        # bare `createdAt` folds to `createdat` and the column is not found.
        needs_quoting = correct != correct.lower()
        if correct == name and not needs_quoting:
            continue
        identifier.set("this", correct)
        if needs_quoting:
            identifier.set("quoted", True)
        changed.append(f"{name} -> " + (f'"{correct}"' if needs_quoting else correct))

    if not changed:
        return IdentifierFix(sql=sql)
    return IdentifierFix(sql=tree.sql(dialect=dialect, pretty=True), changed=changed)
