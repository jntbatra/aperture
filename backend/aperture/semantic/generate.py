"""Generate a starter semantic layer from what the database already reveals.

Writing 56 tables of YAML by hand at 2am is how a semantic layer ends up
stale. The skeleton is derived -- tables, foreign keys, observed values -- and
only the metric definitions need a human, because only those encode a decision
the database cannot make.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..db.introspect import SchemaSnapshot
from ..db.profile import DatabaseProfile


def build_skeleton(snapshot: SchemaSnapshot, profile: DatabaseProfile) -> dict:
    tables: dict[str, dict] = {}
    for name, table in snapshot.tables.items():
        table_profile = profile.tables.get(name)
        entry: dict = {
            "rows": table_profile.exact_rows if table_profile else 0,
            "columns": {},
        }
        for column in table.columns:
            column_entry: dict = {"type": column.data_type}
            if column.enum_values:
                column_entry["values"] = column.enum_values
            elif table_profile and column.name in table_profile.columns:
                observed = table_profile.columns[column.name]
                if observed.common_values:
                    column_entry["observed"] = observed.common_values[:12]
                if observed.min_value:
                    column_entry["range"] = [observed.min_value, observed.max_value]
            entry["columns"][column.name] = column_entry
        tables[name] = entry

    joins = [
        f"{fk.src_table}.{fk.src_column} = {fk.tgt_table}.{fk.tgt_column}"
        for fk in snapshot.foreign_keys
    ]

    return {
        "tables": tables,
        "joins": sorted(set(joins)),
        "empty_tables": profile.empty_tables,
        "metrics": {},
        "conventions": [],
    }


def write_skeleton(path: str | Path, snapshot: SchemaSnapshot, profile: DatabaseProfile) -> Path:
    path = Path(path)
    path.write_text(yaml.safe_dump(build_skeleton(snapshot, profile), sort_keys=False, width=100))
    return path
