"""Suggest the next question.

Suggestions are generated from the result that was just returned and the
columns that actually exist, not asked of a model. That keeps them free, keeps
them instant, and -- more importantly -- means a suggested question can always
be answered: it never proposes a breakdown by a column the database does not
have.
"""

from __future__ import annotations

from dataclasses import dataclass

from .charts.rules import classify_columns
from .db.introspect import SchemaSnapshot
from .db.profile import DatabaseProfile

# A dimension worth offering: few enough distinct values to read as a chart.
MAX_DIMENSION_CARDINALITY = 12
MAX_SUGGESTIONS = 4


@dataclass
class Suggestion:
    text: str
    reason: str


def _dimensions(
    tables: list[str], snapshot: SchemaSnapshot, profile: DatabaseProfile, used: set[str]
) -> list[tuple[str, str]]:
    """Low-cardinality columns in the queried tables, as (column, table)."""
    found: list[tuple[str, str]] = []
    for table in tables:
        table_profile = profile.tables.get(table)
        table_info = snapshot.tables.get(table)
        if not table_profile or not table_info:
            continue
        for column in table_info.columns:
            if column.name.lower() in used or column.is_pk or column.is_fk:
                continue
            observed = table_profile.columns.get(column.name)
            values = column.enum_values or (observed.common_values if observed else [])
            if values and 1 < len(values) <= MAX_DIMENSION_CARDINALITY:
                found.append((column.name, table))
    return found


def _temporal_columns(tables: list[str], snapshot: SchemaSnapshot) -> list[str]:
    columns = []
    for table in tables:
        info = snapshot.tables.get(table)
        if not info:
            continue
        columns.extend(
            c.name for c in info.columns if any(t in c.data_type.lower() for t in ("date", "time"))
        )
    return columns


def suggest(
    *,
    question: str,
    columns: list[str],
    rows: list[list],
    linked_tables: list[str],
    snapshot: SchemaSnapshot,
    profile: DatabaseProfile,
    status: str = "answered",
    has_caveat: bool = False,
) -> list[Suggestion]:
    """Propose follow-up questions grounded in this result and this schema."""
    suggestions: list[Suggestion] = []
    used = {c.lower() for c in columns}

    if has_caveat:
        suggestions.append(
            Suggestion(
                "Recalculate this counting each row only once",
                "the join inflated the aggregate",
            )
        )

    if status == "empty":
        suggestions.append(
            Suggestion("What date range does this data actually cover?", "the result was empty")
        )

    fields = classify_columns(columns, rows) if rows else []
    has_measure = any(f.type == "quantitative" for f in fields)
    has_time = any(f.type == "temporal" for f in fields)

    if has_measure and not has_time:
        for column in _temporal_columns(linked_tables, snapshot)[:1]:
            suggestions.append(
                Suggestion("How has this changed over time, by month?", f"{column} is available")
            )

    if has_measure:
        for column, table in _dimensions(linked_tables, snapshot, profile, used)[:2]:
            suggestions.append(
                Suggestion(f"Break this down by {column}", f"{table}.{column} has few values")
            )

    if rows and len(rows) > 5 and has_measure:
        suggestions.append(Suggestion("Show only the top 5", "the result has many rows"))

    if has_time and has_measure:
        suggestions.append(
            Suggestion("Which period had the biggest change?", "the result is a time series")
        )

    deduped: list[Suggestion] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        key = suggestion.text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(suggestion)
    return deduped[:MAX_SUGGESTIONS]
