"""Data profiling: the half of the schema that DDL cannot tell you.

Structure says `orders.status` is of type OrderStatus. Only the data says the
values in play are DELIVERED, CONFUSED_CUSTOMER, CANCELLED -- and no model
guesses a literal like CONFUSED_CUSTOMER. Value linking is the dominant
text-to-SQL failure mode, so these observed values go into the prompt.

On Postgres this reads `pg_stats`, which the planner has already computed:
most-common values, distinct counts and null fractions for free, no scans. That
keeps profiling viable on a warehouse. Other dialects fall back to sampling.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from sqlalchemy import text

from ..config import settings

from .connection import Database
from .introspect import SchemaSnapshot

# Columns with more distinct values than this are not worth enumerating.
MAX_ENUMERABLE_DISTINCT = 25


@lru_cache(maxsize=1)
def _pii_pattern() -> re.Pattern:
    return re.compile(settings().pii_column_pattern, re.I)


_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def is_opaque_identifier(column, values: list[str]) -> bool:
    """Whether a column's values are identifiers, not vocabulary.

    A foreign key holding ten UUIDs is enumerable but worthless in a prompt: it
    costs context and teaches nothing, unlike a status column whose labels the
    model genuinely cannot guess.
    """
    if getattr(column, "is_pk", False) or getattr(column, "is_fk", False):
        return True
    return any(_UUIDISH.match(str(v)) for v in values[:3])


def is_sensitive(column: str) -> bool:
    """Whether a column's observed values must never reach a prompt.

    The profiler enumerates any low-cardinality column, which on small real
    tables means customer emails and phone numbers -- and those would then flow
    into the prompt, to the model provider, and into any trace backend. Shape
    (null rate, distinct count) is still recorded; only the values are dropped.
    """
    return bool(_pii_pattern().search(column))
# Types whose min/max is worth knowing ("last month" is meaningless otherwise).
RANGE_TYPES = ("timestamp", "date", "time")


@dataclass
class ColumnProfile:
    table: str
    column: str
    null_fraction: float = 0.0
    distinct_estimate: float = 0.0
    common_values: list[str] = field(default_factory=list)
    min_value: str | None = None
    max_value: str | None = None
    sensitive: bool = False

    def describe(self) -> str:
        bits = []
        if self.sensitive:
            bits.append("values withheld (sensitive column)")
        if self.common_values:
            bits.append("values: " + ", ".join(self.common_values[:12]))
        if self.min_value is not None:
            bits.append(f"range: {self.min_value} .. {self.max_value}")
        if self.null_fraction > 0.05:
            bits.append(f"{self.null_fraction:.0%} null")
        return "; ".join(bits)


@dataclass
class TableProfile:
    table: str
    exact_rows: int = 0
    columns: dict[str, ColumnProfile] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.exact_rows == 0


@dataclass
class DatabaseProfile:
    tables: dict[str, TableProfile] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tables": {
                name: {
                    "table": t.table,
                    "exact_rows": t.exact_rows,
                    "columns": {c: asdict(p) for c, p in t.columns.items()},
                }
                for name, t in self.tables.items()
            }
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DatabaseProfile":
        profile = cls()
        for name, t in data["tables"].items():
            tp = TableProfile(table=t["table"], exact_rows=t["exact_rows"])
            tp.columns = {c: ColumnProfile(**p) for c, p in t["columns"].items()}
            profile.tables[name] = tp
        return profile

    @property
    def empty_tables(self) -> list[str]:
        """Tables an agent can write perfect SQL against and still get nothing."""
        return sorted(t for t, p in self.tables.items() if p.is_empty)

    def annotate(self, table: str, column: str) -> str:
        profile = self.tables.get(table)
        if not profile:
            return ""
        col = profile.columns.get(column)
        return col.describe() if col else ""


_PG_STATS = """
SELECT tablename, attname, null_frac, n_distinct, most_common_vals::text AS mcv
FROM pg_stats
WHERE schemaname = :schema
"""


def _parse_mcv(raw: str | None) -> list[str]:
    """pg_stats renders most_common_vals as a brace-wrapped array literal."""
    if not raw:
        return []
    inner = raw.strip().lstrip("{").rstrip("}")
    if not inner:
        return []
    values, current, in_quotes = [], [], False
    for char in inner:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            values.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        values.append("".join(current).strip())
    return [v for v in values if v]


def profile_database(
    db: Database,
    snapshot: SchemaSnapshot,
    *,
    schema: str = "public",
    max_tables: int = 200,
) -> DatabaseProfile:
    """Profile up to `max_tables` tables, largest first."""
    profile = DatabaseProfile()
    ordered = sorted(snapshot.tables.values(), key=lambda t: -t.approx_rows)[:max_tables]
    names = [t.name for t in ordered]

    if db.dialect == "postgresql":
        with db.engine.connect() as conn:
            stats: dict[tuple[str, str], dict] = {}
            for row in conn.execute(text(_PG_STATS), {"schema": schema}):
                stats[(row.tablename, row.attname)] = {
                    "null_frac": float(row.null_frac or 0.0),
                    "n_distinct": float(row.n_distinct or 0.0),
                    "mcv": _parse_mcv(row.mcv),
                }

            for table in ordered:
                tp = TableProfile(table=table.name)
                # Exact count: the planner estimate is stale after bulk loads,
                # and demo questions hinge on whether a table is truly empty.
                tp.exact_rows = int(
                    conn.execute(text(f'SELECT count(*) FROM "{schema}"."{table.name}"')).scalar()
                    or 0
                )

                range_cols = [
                    c.name
                    for c in table.columns
                    if any(t in c.data_type.lower() for t in RANGE_TYPES)
                ]
                ranges: dict[str, tuple] = {}
                if range_cols and tp.exact_rows:
                    selects = ", ".join(
                        f'min("{c}")::text AS "min_{i}", max("{c}")::text AS "max_{i}"'
                        for i, c in enumerate(range_cols)
                    )
                    row = conn.execute(
                        text(f'SELECT {selects} FROM "{schema}"."{table.name}"')
                    ).one()
                    for i, col in enumerate(range_cols):
                        ranges[col] = (row._mapping[f"min_{i}"], row._mapping[f"max_{i}"])

                for column in table.columns:
                    stat = stats.get((table.name, column.name), {})
                    n_distinct = stat.get("n_distinct", 0.0)
                    enumerable = 0 < n_distinct <= MAX_ENUMERABLE_DISTINCT
                    common = stat.get("mcv", []) if (enumerable or column.enum_values) else []
                    low, high = ranges.get(column.name, (None, None))
                    if not common and not low and not stat:
                        continue
                    sensitive = is_sensitive(column.name)
                    if common and is_opaque_identifier(column, common):
                        common = []
                    tp.columns[column.name] = ColumnProfile(
                        table=table.name,
                        column=column.name,
                        null_fraction=stat.get("null_frac", 0.0),
                        distinct_estimate=n_distinct,
                        common_values=[] if sensitive else common,
                        min_value=low,
                        max_value=high,
                        sensitive=sensitive,
                    )
                profile.tables[table.name] = tp
        return profile

    # SQLite / MySQL: no planner statistics to borrow, so sample directly.
    with db.engine.connect() as conn:
        for table in ordered:
            quoted = f"`{table.name}`" if db.dialect == "mysql" else f'"{table.name}"'
            tp = TableProfile(table=table.name)
            tp.exact_rows = int(conn.execute(text(f"SELECT count(*) FROM {quoted}")).scalar() or 0)
            if tp.exact_rows:
                for column in table.columns:
                    col_q = f"`{column.name}`" if db.dialect == "mysql" else f'"{column.name}"'
                    distinct = int(
                        conn.execute(
                            text(
                                f"SELECT count(*) FROM (SELECT DISTINCT {col_q} FROM {quoted} "
                                f"LIMIT {MAX_ENUMERABLE_DISTINCT + 1}) s"
                            )
                        ).scalar()
                        or 0
                    )
                    if 0 < distinct <= MAX_ENUMERABLE_DISTINCT:
                        if is_sensitive(column.name):
                            tp.columns[column.name] = ColumnProfile(
                                table=table.name,
                                column=column.name,
                                distinct_estimate=distinct,
                                sensitive=True,
                            )
                            continue
                        raw_values = [
                            str(r[0])
                            for r in conn.execute(
                                text(
                                    f"SELECT DISTINCT {col_q} FROM {quoted} "
                                    f"WHERE {col_q} IS NOT NULL LIMIT {MAX_ENUMERABLE_DISTINCT}"
                                )
                            )
                        ]
                        values = [] if is_opaque_identifier(column, raw_values) else raw_values
                        tp.columns[column.name] = ColumnProfile(
                            table=table.name,
                            column=column.name,
                            distinct_estimate=distinct,
                            common_values=values,
                        )
            profile.tables[table.name] = tp
    return profile
