"""Vega-Lite spec construction and validation.

The chart is data, not code. A model that emits Python to draw a chart is a
remote-code-execution hole; a spec can be checked field by field against the
result set and simply rejected if it references something that is not there.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from .rules import MAX_CATEGORIES, choose_mark, classify_columns

VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

_MARK_FOR = {"line": "line", "bar": "bar", "point": "point"}


def jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def records(columns: list[str], rows: list[list[Any]]) -> list[dict]:
    return [{c: jsonable(v) for c, v in zip(columns, row, strict=False)} for row in rows]


def build_spec(
    columns: list[str], rows: list[list[Any]], *, title: str = ""
) -> dict | None:
    """Build a Vega-Lite spec for a result set, or None when a table is better."""
    if not columns or not rows:
        return None

    fields = classify_columns(columns, rows)
    mark, chosen = choose_mark(fields, len(rows))

    if mark == "table":
        return None

    data = records(columns, rows)

    if mark == "metric":
        field = chosen[0]
        return {
            "$schema": VEGA_LITE_SCHEMA,
            "title": title or field.name,
            "aperture": {"kind": "metric", "value": data[0][field.name], "label": field.name},
            "data": {"values": data},
            "mark": {"type": "text", "fontSize": 48},
            "encoding": {"text": {"field": field.name, "type": "quantitative"}},
        }

    if mark == "bar":
        category, measure = chosen
        # Long tails are unreadable; show the top slice and say so in the title.
        ordered = sorted(data, key=lambda r: (r.get(measure.name) is None, r.get(measure.name)), reverse=True)
        trimmed = ordered[:MAX_CATEGORIES]
        suffix = f" (top {MAX_CATEGORIES})" if len(ordered) > MAX_CATEGORIES else ""
        return {
            "$schema": VEGA_LITE_SCHEMA,
            "title": (title or f"{measure.name} by {category.name}") + suffix,
            "data": {"values": trimmed},
            "mark": "bar",
            "encoding": {
                "y": {"field": category.name, "type": "nominal", "sort": "-x"},
                "x": {"field": measure.name, "type": "quantitative"},
                "tooltip": [
                    {"field": category.name, "type": "nominal"},
                    {"field": measure.name, "type": "quantitative"},
                ],
            },
        }

    x, y = chosen
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "title": title or f"{y.name} by {x.name}",
        "data": {"values": data},
        "mark": {"type": _MARK_FOR[mark], "point": mark == "line"},
        "encoding": {
            "x": {"field": x.name, "type": x.type},
            "y": {"field": y.name, "type": y.type},
            "tooltip": [
                {"field": x.name, "type": x.type},
                {"field": y.name, "type": y.type},
            ],
        },
    }


def validate_spec(spec: dict, columns: list[str]) -> tuple[bool, str]:
    """Check a caller-supplied spec against the columns actually returned."""
    if not isinstance(spec, dict):
        return False, "spec is not an object"
    if "mark" not in spec:
        return False, "spec has no mark"

    encoding = spec.get("encoding")
    if not isinstance(encoding, dict) or not encoding:
        return False, "spec has no encoding"

    known = set(columns)
    for channel, definition in encoding.items():
        entries = definition if isinstance(definition, list) else [definition]
        for entry in entries:
            if not isinstance(entry, dict):
                return False, f"encoding.{channel} is malformed"
            field = entry.get("field")
            if field is not None and field not in known:
                return False, f"encoding.{channel} references unknown column {field!r}"
    return True, ""
