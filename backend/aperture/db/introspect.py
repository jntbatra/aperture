"""Bulk schema introspection.

Deliberately not SQLAlchemy's `Inspector`: that issues a round-trip per table,
which is fine for 56 tables and unusable for 50,000. Every dialect here reads
its whole catalog in a fixed number of queries, so introspection cost is flat
in table count.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from sqlalchemy import text

from .connection import Database


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True
    comment: str | None = None
    is_pk: bool = False
    is_fk: bool = False
    enum_values: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"{self.name} {self.data_type}"]
        if self.is_pk:
            bits.append("PK")
        if self.is_fk:
            bits.append("FK")
        if self.enum_values:
            bits.append("values: " + ", ".join(self.enum_values[:12]))
        if self.comment:
            bits.append(f"-- {self.comment}")
        return " ".join(bits)


@dataclass
class TableInfo:
    name: str
    schema: str = "public"
    comment: str | None = None
    approx_rows: int = 0
    columns: list[ColumnInfo] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)

    def ddl(self, annotate: Callable[[str, str], str] | None = None, *, rows: int | None = None) -> str:
        """Compact CREATE-TABLE-ish rendering used in prompts.

        `annotate(table, column)` supplies observed values and ranges from a
        profile, so schema and data render together rather than in two passes.
        """
        head = f"TABLE {self.name}"
        if self.comment:
            head += f"  -- {self.comment}"
        count = rows if rows is not None else self.approx_rows
        if count:
            head += f"  [{count:,} rows]"
        elif count == 0 and rows is not None:
            head += "  [EMPTY]"
        lines = []
        for column in self.columns:
            note = annotate(self.name, column.name) if annotate else ""
            lines.append(f"  {column.describe()}" + (f"   -- {note}" if note else ""))
        return f"{head}\n" + "\n".join(lines)


@dataclass
class ForeignKey:
    src_table: str
    src_column: str
    tgt_table: str
    tgt_column: str
    name: str = ""


@dataclass
class SchemaSnapshot:
    dialect: str
    tables: dict[str, TableInfo] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return sorted(self.tables)

    def ddl_for(self, names: list[str], *, profile=None) -> str:
        """Render DDL for `names`, interleaving profile annotations if given."""
        annotate = profile.annotate if profile is not None else None
        blocks = []
        for name in names:
            table = self.tables.get(name)
            if table is None:
                continue
            rows = None
            if profile is not None and name in profile.tables:
                rows = profile.tables[name].exact_rows
            blocks.append(table.ddl(annotate, rows=rows))
        return "\n\n".join(blocks)

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "tables": {
                name: {
                    "name": t.name,
                    "schema": t.schema,
                    "comment": t.comment,
                    "approx_rows": t.approx_rows,
                    "columns": [asdict(c) for c in t.columns],
                }
                for name, t in self.tables.items()
            },
            "foreign_keys": [asdict(fk) for fk in self.foreign_keys],
        }

    @classmethod
    def from_dict(cls, data: dict) -> SchemaSnapshot:
        snap = cls(dialect=data["dialect"])
        for name, t in data["tables"].items():
            snap.tables[name] = TableInfo(
                name=t["name"],
                schema=t["schema"],
                comment=t["comment"],
                approx_rows=t["approx_rows"],
                columns=[ColumnInfo(**c) for c in t["columns"]],
            )
        snap.foreign_keys = [ForeignKey(**fk) for fk in data["foreign_keys"]]
        return snap


_PG_COLUMNS = """
SELECT c.relname                                   AS table_name,
       a.attname                                   AS column_name,
       format_type(a.atttypid, a.atttypmod)        AS data_type,
       NOT a.attnotnull                            AS nullable,
       col_description(c.oid, a.attnum)            AS comment,
       t.typname                                   AS type_name,
       t.typtype                                   AS type_kind,
       obj_description(c.oid)                      AS table_comment,
       c.reltuples                                 AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_type t ON t.oid = a.atttypid
WHERE n.nspname = :schema AND c.relkind IN ('r', 'v', 'm', 'p')
ORDER BY c.relname, a.attnum
"""

_PG_ENUMS = """
SELECT t.typname AS type_name, e.enumlabel AS label
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
ORDER BY t.typname, e.enumsortorder
"""

_PG_KEYS = """
SELECT con.contype                AS kind,
       con.conname                AS name,
       src.relname                AS src_table,
       srcatt.attname             AS src_column,
       tgt.relname                AS tgt_table,
       tgtatt.attname             AS tgt_column
FROM pg_constraint con
JOIN pg_class src ON src.oid = con.conrelid
JOIN pg_namespace n ON n.oid = src.relnamespace
LEFT JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS sk(attnum, ord) ON TRUE
LEFT JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS tk(attnum, ord) ON tk.ord = sk.ord
LEFT JOIN pg_attribute srcatt ON srcatt.attrelid = con.conrelid AND srcatt.attnum = sk.attnum
LEFT JOIN pg_class tgt ON tgt.oid = con.confrelid
LEFT JOIN pg_attribute tgtatt ON tgtatt.attrelid = con.confrelid AND tgtatt.attnum = tk.attnum
WHERE n.nspname = :schema AND con.contype IN ('p', 'f')
"""


def _introspect_postgres(db: Database, schema: str) -> SchemaSnapshot:
    snap = SchemaSnapshot(dialect="postgresql")

    with db.engine.connect() as conn:
        enum_labels: dict[str, list[str]] = {}
        for row in conn.execute(text(_PG_ENUMS)):
            enum_labels.setdefault(row.type_name, []).append(row.label)

        for row in conn.execute(text(_PG_COLUMNS), {"schema": schema}):
            table = snap.tables.get(row.table_name)
            if table is None:
                table = TableInfo(
                    name=row.table_name,
                    schema=schema,
                    comment=row.table_comment,
                    approx_rows=max(int(row.approx_rows or 0), 0),
                )
                snap.tables[row.table_name] = table
            table.columns.append(
                ColumnInfo(
                    name=row.column_name,
                    data_type=row.data_type,
                    nullable=bool(row.nullable),
                    comment=row.comment,
                    enum_values=enum_labels.get(row.type_name, []) if row.type_kind == "e" else [],
                )
            )

        for row in conn.execute(text(_PG_KEYS), {"schema": schema}):
            table = snap.tables.get(row.src_table)
            if table is None or row.src_column is None:
                continue
            column = table.column(row.src_column)
            if column is None:
                continue
            if row.kind == "p":
                column.is_pk = True
            elif row.kind == "f" and row.tgt_table and row.tgt_column:
                column.is_fk = True
                snap.foreign_keys.append(
                    ForeignKey(
                        src_table=row.src_table,
                        src_column=row.src_column,
                        tgt_table=row.tgt_table,
                        tgt_column=row.tgt_column,
                        name=row.name or "",
                    )
                )
    return snap


def _introspect_generic(db: Database, schema: str | None) -> SchemaSnapshot:
    """SQLite / MySQL path via SQLAlchemy reflection.

    Both are used at small-to-medium scale here (SQLite is the benchmark
    format), so per-table round-trips are acceptable.
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(db.engine)
    snap = SchemaSnapshot(dialect=db.dialect)

    for name in inspector.get_table_names(schema=schema):
        pk_cols = set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
        fk_defs = inspector.get_foreign_keys(name, schema=schema)
        fk_cols = {c for fk in fk_defs for c in (fk.get("constrained_columns") or [])}

        table = TableInfo(name=name, schema=schema or "")
        for col in inspector.get_columns(name, schema=schema):
            enum_values = list(getattr(col["type"], "enums", []) or [])
            table.columns.append(
                ColumnInfo(
                    name=col["name"],
                    data_type=str(col["type"]),
                    nullable=bool(col.get("nullable", True)),
                    comment=col.get("comment"),
                    is_pk=col["name"] in pk_cols,
                    is_fk=col["name"] in fk_cols,
                    enum_values=enum_values,
                )
            )
        snap.tables[name] = table

        for fk in fk_defs:
            target = fk.get("referred_table")
            for src, tgt in zip(
                fk.get("constrained_columns") or [],
                fk.get("referred_columns") or [],
                strict=False,
            ):
                snap.foreign_keys.append(
                    ForeignKey(src_table=name, src_column=src, tgt_table=target, tgt_column=tgt)
                )
    return snap


def introspect(db: Database, *, schema: str = "public") -> SchemaSnapshot:
    if db.dialect == "postgresql":
        return _introspect_postgres(db, schema)
    return _introspect_generic(db, None)
