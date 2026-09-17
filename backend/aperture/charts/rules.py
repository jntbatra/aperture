"""Choosing a chart from the shape of a result set.

Chart choice is a function of column types and cardinality, so it is decided
here rather than asked of a model: deterministic, free, and it cannot
hallucinate a field name that is not in the result.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

FieldType = Literal["temporal", "quantitative", "nominal"]
Mark = Literal["line", "bar", "point", "metric", "table"]

# Beyond this many categories a bar chart stops being readable.
MAX_CATEGORIES = 12


@dataclass
class FieldInfo:
    name: str
    type: FieldType
    distinct: int


def classify_value(value: Any) -> FieldType:
    if isinstance(value, (dt.datetime, dt.date)):
        return "temporal"
    if isinstance(value, bool):
        return "nominal"
    if isinstance(value, (int, float, Decimal)):
        return "quantitative"
    return "nominal"


def classify_columns(columns: list[str], rows: list[list[Any]]) -> list[FieldInfo]:
    fields: list[FieldInfo] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        kind = classify_value(values[0]) if values else "nominal"
        # A numeric identifier column is a label, not a measure.
        if kind == "quantitative" and name.lower().endswith(("id", "_id")):
            kind = "nominal"
        distinct = len({str(v) for v in values})
        fields.append(FieldInfo(name=name, type=kind, distinct=distinct))
    return fields


def choose_mark(fields: list[FieldInfo], row_count: int) -> tuple[Mark, list[FieldInfo]]:
    """Pick a mark and the fields to encode, or `table` when nothing fits."""
    temporal = [f for f in fields if f.type == "temporal"]
    quantitative = [f for f in fields if f.type == "quantitative"]
    nominal = [f for f in fields if f.type == "nominal"]

    if row_count == 1 and len(quantitative) == 1 and len(fields) == 1:
        return "metric", quantitative

    if temporal and quantitative:
        return "line", [temporal[0], quantitative[0]]

    if nominal and quantitative:
        category = min(nominal, key=lambda f: f.distinct)
        if category.distinct <= MAX_CATEGORIES or row_count <= MAX_CATEGORIES:
            return "bar", [category, quantitative[0]]
        return "bar", [category, quantitative[0]]

    if len(quantitative) >= 2:
        return "point", quantitative[:2]

    return "table", fields
