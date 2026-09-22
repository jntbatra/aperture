"""Read a database's structure into immutable Python objects.

Why reflection instead of parsing SQL text
------------------------------------------
A text-to-SQL agent needs to know what tables and columns exist and how they
relate. Two ways to learn that: parse the ``CREATE TABLE`` statements, or ask
the database itself. We ask the database.

SQLAlchemy's ``MetaData.reflect()`` reads the live catalog (``information_schema``
and friends) and hands back structured objects. That means:

* No SQL parser to maintain, and no drift between what we parsed and what is
  actually deployed.
* One code path for Postgres, MySQL and Snowflake — SQLAlchemy absorbs the
  dialect differences.
* Foreign keys arrive as real constraint objects, not something we infer from
  column naming conventions.

Everything here is frozen (immutable). A schema snapshot is shared across
requests and cached, so it must not be mutable by accident.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import Engine, MetaData


@dataclass(frozen=True, slots=True)
class Column:
    """One column, reduced to the facts that matter for generating SQL."""

    name: str
    type: str
    """Rendered type as the dialect reports it, e.g. ``INTEGER``, ``VARCHAR(50)``.

    Kept as a string rather than a SQLAlchemy type object because it is
    ultimately placed into a prompt, and because it must be JSON-serialisable
    for the schema-version hash.
    """

    nullable: bool
    primary_key: bool


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """A foreign-key constraint, kept whole rather than split per column.

    Composite keys matter. ``FOREIGN KEY (tenant_id, order_id) REFERENCES
    orders (tenant_id, id)`` is *one* relationship across two columns. Modelling
    it as two independent single-column links would let the agent generate a
    join on half a key, which silently returns wrong rows rather than erroring.

    ``source`` is the table holding the constraint (the child); ``target`` is
    the table being referenced (the parent).
    """

    source_table: str
    source_columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]

    def join_condition(
        self, source_alias: str | None = None, target_alias: str | None = None
    ) -> str:
        """Render this key as a SQL ``ON`` clause.

        Used when describing available joins to the model, so the agent is
        shown the exact predicate rather than left to infer it from column
        names. Handles composite keys by ANDing each column pair.
        """
        from sqlagent.prompts import quote_identifier

        left = source_alias or quote_identifier(self.source_table)
        right = target_alias or quote_identifier(self.target_table)
        pairs = [
            f"{left}.{quote_identifier(src)} = {right}.{quote_identifier(tgt)}"
            for src, tgt in zip(self.source_columns, self.target_columns, strict=True)
        ]
        return " AND ".join(pairs)


@dataclass(frozen=True, slots=True)
class Table:
    """A single table: its columns and the foreign keys it declares."""

    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]
    """Only keys *declared by this table* (i.e. where this table is the child).

    Keys pointing *at* this table live on the other table. The graph layer is
    what makes both directions navigable; see ``sqlagent.schema.graph``.
    """

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns if column.primary_key)


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """The whole database structure at one point in time, plus its fingerprint.

    ``version`` is the load-bearing field. Caches for the schema graph, the
    generated-SQL templates and the sample-row store are all keyed by it, so a
    column rename invalidates every downstream cache at once instead of leaving
    stale entries that produce queries referencing columns that no longer exist.
    """

    tables: dict[str, Table]
    version: str

    def __len__(self) -> int:
        return len(self.tables)

    def __contains__(self, table_name: str) -> bool:
        return table_name in self.tables

    def __getitem__(self, table_name: str) -> Table:
        return self.tables[table_name]

    @property
    def foreign_keys(self) -> tuple[ForeignKey, ...]:
        """Every foreign key in the database, in deterministic order."""
        return tuple(
            fk for table in self.tables.values() for fk in table.foreign_keys
        )


def reflect_schema(engine: Engine, *, schema: str | None = None) -> SchemaSnapshot:
    """Read the live database structure.

    Args:
        engine: A SQLAlchemy engine. A read-only connection is sufficient and
            is what production should use — reflection only reads the catalog.
        schema: Optional named schema (Postgres ``search_path`` schema, not the
            general sense of "database schema"). ``None`` uses the connection's
            default, normally ``public``.

    Returns:
        An immutable snapshot, including a version hash of the structure.

    Note:
        This is a relatively expensive call — it issues a batch of catalog
        queries. It is meant to run at startup and on schema change, never per
        user request. The caching layer above is responsible for enforcing that.
    """
    metadata = MetaData(schema=schema)
    metadata.reflect(bind=engine)

    tables: dict[str, Table] = {}

    # Sort for determinism: the version hash below must not change just because
    # the database returned tables in a different order.
    for table_name in sorted(metadata.tables):
        sa_table = metadata.tables[table_name]

        columns = tuple(
            Column(
                name=column.name,
                type=str(column.type),
                nullable=bool(column.nullable),
                primary_key=bool(column.primary_key),
            )
            for column in sa_table.columns
        )

        # Iterate constraints, not `sa_table.foreign_keys`. The latter yields one
        # entry per column, which would tear a composite key into separate
        # single-column relationships. See ForeignKey's docstring.
        foreign_keys = tuple(
            sorted(
                (
                    ForeignKey(
                        source_table=_unqualified(constraint.table.fullname),
                        source_columns=tuple(col.name for col in constraint.columns),
                        target_table=_unqualified(constraint.referred_table.fullname),
                        target_columns=tuple(
                            element.column.name for element in constraint.elements
                        ),
                    )
                    for constraint in sa_table.foreign_key_constraints
                ),
                key=lambda fk: (fk.target_table, fk.source_columns),
            )
        )

        name = _unqualified(table_name)
        tables[name] = Table(name=name, columns=columns, foreign_keys=foreign_keys)

    return SchemaSnapshot(tables=tables, version=compute_version(tables))


def compute_version(tables: dict[str, Table]) -> str:
    """Fingerprint the schema's *structure*.

    Included: table names, column names, column types, nullability, primary
    keys, and foreign-key constraints. Excluded: row counts, statistics, and
    anything else that changes as data changes — those must not invalidate a
    cache that only depends on structure.

    Why a content hash rather than DDL event hooks: not every deployment
    exposes event triggers, and a missed event fails silently by serving a
    stale schema. Re-hashing on an interval is cheap and self-correcting.
    """
    canonical = [
        {
            "table": table.name,
            "columns": [
                [column.name, column.type, column.nullable, column.primary_key]
                for column in table.columns
            ],
            "foreign_keys": [
                [
                    list(fk.source_columns),
                    fk.target_table,
                    list(fk.target_columns),
                ]
                for fk in table.foreign_keys
            ],
        }
        for table in sorted(tables.values(), key=lambda t: t.name)
    ]
    payload = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _unqualified(table_name: str) -> str:
    """Strip a leading schema qualifier: ``public.orders`` -> ``orders``.

    Reflection returns qualified names when a schema is specified. We store
    bare names because that is what appears in generated SQL and in the user's
    mental model. Cross-schema deployments that genuinely need qualification
    are out of scope for now — a deliberate simplification, not an oversight.
    """
    _, _, bare = table_name.rpartition(".")
    return bare
