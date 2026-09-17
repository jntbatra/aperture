"""Patterns worth pointing out in a result set.

Computed arithmetically from the rows that were returned, never asked of a
model: the statements are either true of the data or they are not. A narrator
asked to "find something interesting" will always find something, which is
exactly the failure mode to avoid.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, pstdev
from typing import Any

from .charts.rules import classify_columns

# A single category holding this share of the total is worth naming.
CONCENTRATION_SHARE = 0.5
# How many points before a monotonic run counts as a trend.
MIN_TREND_POINTS = 3
# Distance from the mean, in standard deviations, before a value is an outlier.
OUTLIER_SIGMA = 2.0
MAX_INSIGHTS = 3


@dataclass
class Insight:
    kind: str
    message: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _label(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()[:10]
    return str(value)


def find_insights(columns: list[str], rows: list[list[Any]]) -> list[Insight]:
    """Describe the shape of a result, when it has a shape worth describing."""
    if not rows or len(rows) < 2 or not columns:
        return []

    fields = classify_columns(columns, rows)
    measures = [f for f in fields if f.type == "quantitative"]
    categories = [f for f in fields if f.type == "nominal"]
    temporal = [f for f in fields if f.type == "temporal"]
    if not measures:
        return []

    measure = measures[0]
    index = columns.index(measure.name)
    values = [(_number(row[index]), row) for row in rows]
    numeric = [(v, row) for v, row in values if v is not None]
    if len(numeric) < 2:
        return []

    insights: list[Insight] = []
    total = sum(v for v, _ in numeric)

    # Concentration: is one row most of the answer?
    if categories and total > 0:
        label_index = columns.index(categories[0].name)
        top_value, top_row = max(numeric, key=lambda pair: pair[0])
        share = top_value / total
        if share >= CONCENTRATION_SHARE and len(numeric) > 2:
            insights.append(
                Insight(
                    "concentration",
                    f"{_label(top_row[label_index])} alone accounts for {share:.0%} of the total "
                    f"across {len(numeric)} {categories[0].name} values.",
                )
            )

    # Trend: does an ordered series move consistently in one direction?
    if temporal and len(numeric) >= MIN_TREND_POINTS:
        time_index = columns.index(temporal[0].name)
        ordered = sorted(values, key=lambda pair: str(pair[1][time_index]))
        series = [v for v, _ in ordered if v is not None]
        if len(series) >= MIN_TREND_POINTS:
            rising = all(b >= a for a, b in zip(series, series[1:]))
            falling = all(b <= a for a, b in zip(series, series[1:]))
            if (rising or falling) and series[0] != series[-1]:
                direction = "risen" if rising else "fallen"
                change = (
                    abs(series[-1] - series[0]) / abs(series[0]) if series[0] else 0
                )
                insights.append(
                    Insight(
                        "trend",
                        f"{measure.name} has {direction} in every period, "
                        f"{change:.0%} in total from first to last.",
                    )
                )

    # Outlier: one value far from the rest.
    if len(numeric) >= 4:
        series = [v for v, _ in numeric]
        spread = pstdev(series)
        centre = mean(series)
        if spread > 0:
            extreme_value, extreme_row = max(numeric, key=lambda pair: abs(pair[0] - centre))
            distance = abs(extreme_value - centre) / spread
            if distance >= OUTLIER_SIGMA:
                label = ""
                if categories:
                    label = f"{_label(extreme_row[columns.index(categories[0].name)])}: "
                insights.append(
                    Insight(
                        "outlier",
                        f"{label}{extreme_value:,.0f} sits {distance:.1f} standard deviations "
                        f"from the average of {centre:,.0f}.",
                    )
                )

    return insights[:MAX_INSIGHTS]
