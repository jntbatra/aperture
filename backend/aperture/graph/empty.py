"""Why did a valid query return nothing?

Zero rows is the most likely live failure on a real database, and it is not an
error: the SQL is correct, the database is fine, and the answer is "0". Handing
that to a model to explain wastes tokens and invites invention, so the common
causes are diagnosed deterministically from data already profiled.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..db.introspect import SchemaSnapshot
from ..db.profile import DatabaseProfile


@dataclass
class EmptyDiagnosis:
    explanation: str
    # Whether a rewritten query could plausibly return rows. An empty table
    # cannot, so retrying is pure waste.
    retryable: bool = False
    suggestion: str = ""

    def __bool__(self) -> bool:
        return bool(self.explanation)


def _literal_equalities(tree: exp.Expr) -> list[tuple[str, str]]:
    """Collect `column = 'literal'` pairs from the WHERE clause."""
    pairs: list[tuple[str, str]] = []
    for eq in tree.find_all(exp.EQ):
        left, right = eq.left, eq.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
            pairs.append((left.name, right.this))
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal) and left.is_string:
            pairs.append((right.name, left.this))
    return pairs


def _looks_like_date(text: str) -> bool:
    return len(text) >= 8 and text[:4].isdigit() and text[4] in "-/"


def _date_comparisons(tree: exp.Expr) -> list[tuple[str, str]]:
    """Collect `column <op> 'date-literal'` pairs.

    Bare literals are not enough: reporting the range of some other temporal
    column while the query filters on `createdAt` is worse than saying nothing.
    """
    pairs: list[tuple[str, str]] = []
    comparisons = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)

    for node in tree.find_all(*comparisons):
        left, right = node.left, node.right
        column = left if isinstance(left, exp.Column) else (right if isinstance(right, exp.Column) else None)
        literal = right if isinstance(right, exp.Literal) else (left if isinstance(left, exp.Literal) else None)
        if column is None or literal is None or not literal.is_string:
            continue
        text = str(literal.this)
        if _looks_like_date(text):
            pairs.append((column.name, text))

    for node in tree.find_all(exp.Between):
        if not isinstance(node.this, exp.Column):
            continue
        for bound in (node.args.get("low"), node.args.get("high")):
            if isinstance(bound, exp.Literal) and bound.is_string and _looks_like_date(str(bound.this)):
                pairs.append((node.this.name, str(bound.this)))
    return pairs


def diagnose_empty(
    sql: str,
    tables: list[str],
    snapshot: SchemaSnapshot,
    profile: DatabaseProfile,
    *,
    dialect: str,
) -> EmptyDiagnosis:
    """Explain a zero-row result, cheapest and most certain cause first."""

    # 1. An empty table can never yield rows, however the query is written.
    referenced_empty = [
        t for t in tables if t in profile.tables and profile.tables[t].is_empty
    ]
    if referenced_empty:
        names = ", ".join(sorted(referenced_empty))
        return EmptyDiagnosis(
            explanation=(
                f"No rows because {names} contains no data at all. "
                "This is a property of the database, not of the query."
            ),
            retryable=False,
        )

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return EmptyDiagnosis(explanation="")

    # 2. A literal that does not occur in the column's observed values.
    for column_name, literal in _literal_equalities(tree):
        for table_name in tables:
            table_profile = profile.tables.get(table_name)
            column_info = snapshot.tables.get(table_name)
            if not table_profile or not column_info:
                continue
            column = column_info.column(column_name)
            observed = list(column.enum_values) if column else []
            profiled = table_profile.columns.get(column_name)
            if profiled and profiled.common_values:
                observed = observed or profiled.common_values
            if not observed:
                continue
            if literal not in observed:
                shown = ", ".join(str(v) for v in observed[:12])
                return EmptyDiagnosis(
                    explanation=(
                        f"No rows because {table_name}.{column_name} never holds "
                        f"'{literal}'. Observed values are: {shown}."
                    ),
                    retryable=True,
                    suggestion=(
                        f"Use one of the observed values for {table_name}.{column_name}: {shown}"
                    ),
                )

    # 3. A date filter outside the range the data actually covers.
    comparisons = _date_comparisons(tree)
    if comparisons:
        for table_name in tables:
            table_profile = profile.tables.get(table_name)
            if not table_profile:
                continue
            for column_name, column in table_profile.columns.items():
                if not column.min_value or not column.max_value:
                    continue
                dates = [value for name, value in comparisons if name == column_name]
                if not dates:
                    continue
                low, high = str(column.min_value)[:10], str(column.max_value)[:10]
                outside = [d for d in dates if d[:10] < low or d[:10] > high]
                if outside and len(dates) == len(outside):
                    return EmptyDiagnosis(
                        explanation=(
                            f"No rows because the date filter falls outside the data. "
                            f"{table_name}.{column_name} covers {low} to {high}, "
                            f"but the query asks for {', '.join(sorted(set(outside)))}."
                        ),
                        retryable=True,
                        suggestion=(
                            f"Restrict the query to the range {low} .. {high}, "
                            f"or report the most recent period available."
                        ),
                    )

    return EmptyDiagnosis(explanation="")
