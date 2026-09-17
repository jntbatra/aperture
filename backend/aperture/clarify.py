"""Ask before guessing.

Most wrong answers in this system are not broken SQL -- they are a reasonable
query answering a slightly different question than the one intended. When the
schema itself shows the question is underspecified, asking one short question
is cheaper and more honest than picking an interpretation and burying it in an
assumptions line.

The rules here are deliberately few and high-precision. A tool that asks about
everything is worse than one that guesses: people stop reading the questions.
Ambiguity is only raised when the database offers genuinely different answers
and nothing in the question chooses between them.

Benchmarks turn this off: there is nobody to ask, and the question is taken as
complete by definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .db.introspect import SchemaSnapshot
from .db.profile import DatabaseProfile
from .schema.linker import LinkedSchema, tokenize

RELATIVE_TIME = re.compile(
    r"\b(last|this|past|previous|recent|current)\s+(day|week|month|quarter|year)\b|"
    r"\b(today|yesterday|ytd|mtd|so far)\b",
    re.I,
)
SUPERLATIVE = re.compile(
    r"\b(best|worst|top|bottom|most|least|highest|lowest|leading|biggest|smallest)\b", re.I
)
# Words that already name a measure, so a superlative is not ambiguous.
MEASURE_WORDS = {
    "revenue", "sales", "amount", "value", "total", "count", "orders", "quantity",
    "units", "spend", "profit", "price", "volume", "number", "rate", "duration", "time",
}
TEMPORAL_TYPES = ("date", "time")

# Timestamps that record a state change on a row rather than when the thing
# happened. Offering "bannedAt" as the date to measure orders by is noise.
ATTRIBUTE_TIMESTAMP = re.compile(
    r"(deleted|banned|updated|verified|expires?|refreshed|lastlogin|synced|seen)", re.I
)
# Column names that plausibly answer "ranked by what?".
MEASURE_NAME = re.compile(
    r"(amount|total|price|qty|quantity|count|value|fee|revenue|spend|duration|minutes|seconds|score|rating)",
    re.I,
)
# A column must be this populated before it is worth offering as an option.
MIN_FILL = 0.5


@dataclass
class Clarification:
    question: str
    options: list[str] = field(default_factory=list)
    reason: str = ""


def _is_populated(table: str, column: str, profile: DatabaseProfile) -> bool:
    table_profile = profile.tables.get(table)
    if not table_profile:
        return True
    observed = table_profile.columns.get(column)
    return not observed or observed.null_fraction <= (1 - MIN_FILL)


def _event_dates(table: str, snapshot: SchemaSnapshot, profile: DatabaseProfile) -> list[str]:
    """Date columns that plausibly say when the thing happened."""
    info = snapshot.tables.get(table)
    if not info:
        return []
    return [
        column.name
        for column in info.columns
        if any(t in column.data_type.lower() for t in TEMPORAL_TYPES)
        and not ATTRIBUTE_TIMESTAMP.search(column.name)
        and _is_populated(table, column.name, profile)
    ]


def _measure_columns(table: str, snapshot: SchemaSnapshot, profile: DatabaseProfile) -> list[str]:
    info = snapshot.tables.get(table)
    if not info:
        return []
    found = []
    for column in info.columns:
        kind = column.data_type.lower()
        numeric = any(t in kind for t in ("int", "numeric", "decimal", "real", "double", "money"))
        if not numeric or column.is_pk or column.is_fk:
            continue
        if not MEASURE_NAME.search(column.name):
            continue
        if _is_populated(table, column.name, profile):
            found.append(column.name)
    return found


def needs_clarification(
    question: str,
    linked: LinkedSchema,
    snapshot: SchemaSnapshot,
    profile: DatabaseProfile,
    *,
    metric_names: set[str] | None = None,
) -> Clarification | None:
    """Return the one question worth asking, or None to proceed."""
    asked = tokenize(question)
    metric_names = metric_names or set()

    # Only the table the question is actually about is considered. Ambiguity
    # across a dozen linked tables is not ambiguity, it is a wide join.
    primary = linked.seeds[0] if linked.seeds else (linked.tables[0] if linked.tables else "")
    if not primary:
        return None

    # 1. A relative date, and more than one event date it could mean.
    if RELATIVE_TIME.search(question):
        dates = _event_dates(primary, snapshot, profile)
        # Match the column name against the raw text: tokenising splits
        # "createdAt" into "created" and "at", both of which are stopwords, so a
        # question that names its date column would look ambiguous.
        lowered = question.lower()
        named = any(date.lower() in lowered for date in dates)
        if len(dates) > 1 and not named:
            return Clarification(
                question="Which date should I measure that by?",
                options=sorted(dates)[:4],
                reason=f"{primary} has more than one date that could define the period",
            )

    # 2. A ranking with no measure named, and several measures to rank by.
    if SUPERLATIVE.search(question) and not (asked & MEASURE_WORDS) and not metric_names:
        measures = _measure_columns(primary, snapshot, profile)
        if len(measures) < 2:
            # The entity table may hold no measures of its own -- "best kitchen"
            # is measured in the orders table, not the kitchen table.
            measures = []
            for table in linked.tables[:4]:
                measures.extend(_measure_columns(table, snapshot, profile))
            measures = sorted(set(measures))
        if len(measures) > 1:
            return Clarification(
                question="Ranked by what?",
                options=sorted(measures)[:4] + ["number of rows"],
                reason="the question asks for a ranking without naming a measure",
            )

    return None
