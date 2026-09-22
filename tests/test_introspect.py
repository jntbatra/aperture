"""Tests for schema reflection and the schema-version fingerprint."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from sqlagent.schema.introspect import (
    Column,
    ForeignKey,
    SchemaSnapshot,
    Table,
    compute_version,
    reflect_schema,
)

# --------------------------------------------------------------------------
# ForeignKey
# --------------------------------------------------------------------------


def test_join_condition_single_column():
    fk = ForeignKey("orders", ("customer_id",), "customers", ("id",))
    assert fk.join_condition() == "orders.customer_id = customers.id"


def test_join_condition_composite_key_ands_every_pair():
    """A composite key must produce every column pair, not just the first.

    Joining on half a composite key returns wrong rows and raises no error,
    so this is a correctness guard, not a formatting nicety.
    """
    fk = ForeignKey(
        "order_items",
        ("tenant_id", "order_id"),
        "orders",
        ("tenant_id", "id"),
    )
    assert fk.join_condition() == (
        "order_items.tenant_id = orders.tenant_id AND order_items.order_id = orders.id"
    )


def test_join_condition_accepts_aliases():
    fk = ForeignKey("orders", ("customer_id",), "customers", ("id",))
    assert fk.join_condition(source_alias="o", target_alias="c") == "o.customer_id = c.id"


def test_join_condition_rejects_mismatched_column_counts():
    """Zip with strict=True turns a malformed key into a loud failure."""
    fk = ForeignKey("a", ("x", "y"), "b", ("z",))
    with pytest.raises(ValueError):
        fk.join_condition()


# --------------------------------------------------------------------------
# Table / SchemaSnapshot accessors
# --------------------------------------------------------------------------


def test_table_exposes_column_names_and_primary_key(snapshot: SchemaSnapshot):
    orders = snapshot["orders"]
    assert orders.column_names[:2] == ("id", "customer_id")
    assert orders.primary_key == ("id",)


def test_composite_primary_key_returns_every_column(snapshot: SchemaSnapshot):
    assert snapshot["order_items"].primary_key == ("order_id", "line_no")


def test_snapshot_container_protocol(snapshot: SchemaSnapshot):
    assert len(snapshot) == 8
    assert "orders" in snapshot
    assert "nonexistent" not in snapshot
    assert snapshot["orders"].name == "orders"


def test_snapshot_foreign_keys_collects_every_key(snapshot: SchemaSnapshot):
    keys = snapshot.foreign_keys
    # orders declares 4, customers 1, invoices 1, order_items 1, employees 1.
    assert len(keys) == 8
    assert all(isinstance(key, ForeignKey) for key in keys)


# --------------------------------------------------------------------------
# Version fingerprint
# --------------------------------------------------------------------------


def test_version_is_stable_across_calls(snapshot: SchemaSnapshot):
    assert compute_version(snapshot.tables) == compute_version(snapshot.tables)


def test_version_ignores_table_insertion_order(snapshot: SchemaSnapshot):
    """Dict ordering must not affect the hash.

    Otherwise a cache would be invalidated at random depending on the order the
    database happened to return tables in.
    """
    reversed_tables = dict(reversed(list(snapshot.tables.items())))
    assert compute_version(reversed_tables) == compute_version(snapshot.tables)


def test_version_changes_when_a_column_is_added(snapshot: SchemaSnapshot):
    before = compute_version(snapshot.tables)

    tables = dict(snapshot.tables)
    regions = tables["regions"]
    tables["regions"] = Table(
        name=regions.name,
        columns=regions.columns + (Column("population", "INTEGER", True, False),),
        foreign_keys=regions.foreign_keys,
    )

    assert compute_version(tables) != before


def test_version_changes_when_a_column_type_changes(snapshot: SchemaSnapshot):
    """A widened column can change query semantics, so it must bust the cache."""
    before = compute_version(snapshot.tables)

    tables = dict(snapshot.tables)
    orders = tables["orders"]
    retyped = tuple(
        Column("total", "NUMERIC(18, 4)", column.nullable, column.primary_key)
        if column.name == "total"
        else column
        for column in orders.columns
    )
    tables["orders"] = Table(orders.name, retyped, orders.foreign_keys)

    assert compute_version(tables) != before


def test_version_changes_when_a_foreign_key_is_dropped(snapshot: SchemaSnapshot):
    before = compute_version(snapshot.tables)

    tables = dict(snapshot.tables)
    customers = tables["customers"]
    tables["customers"] = Table(customers.name, customers.columns, ())

    assert compute_version(tables) != before


def test_version_is_short_and_hex(snapshot: SchemaSnapshot):
    version = compute_version(snapshot.tables)
    assert len(version) == 16
    assert all(character in "0123456789abcdef" for character in version)


# --------------------------------------------------------------------------
# Integration: reflect a live database
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_reflect_finds_every_table(live_snapshot: SchemaSnapshot):
    assert set(live_snapshot.tables) == {
        "regions",
        "customers",
        "addresses",
        "orders",
        "invoices",
        "order_items",
        "employees",
        "audit_log",
    }


@pytest.mark.integration
def test_reflect_captures_isolated_table(live_snapshot: SchemaSnapshot):
    """A table with no foreign keys must still be reflected."""
    assert live_snapshot["audit_log"].foreign_keys == ()
    assert "action" in live_snapshot["audit_log"].column_names


@pytest.mark.integration
def test_reflect_keeps_composite_foreign_key_whole(live_snapshot: SchemaSnapshot):
    """order_items -> orders is declared as one constraint and must stay one."""
    keys = live_snapshot["order_items"].foreign_keys
    assert len(keys) == 1
    assert keys[0].source_columns == ("order_id",)
    assert keys[0].target_table == "orders"


@pytest.mark.integration
def test_reflect_captures_composite_primary_key(live_snapshot: SchemaSnapshot):
    assert live_snapshot["order_items"].primary_key == ("order_id", "line_no")


@pytest.mark.integration
def test_reflect_captures_self_reference(live_snapshot: SchemaSnapshot):
    keys = live_snapshot["employees"].foreign_keys
    assert len(keys) == 1
    assert keys[0].source_table == "employees"
    assert keys[0].target_table == "employees"


@pytest.mark.integration
def test_reflect_captures_both_sides_of_a_cycle(live_snapshot: SchemaSnapshot):
    """orders -> invoices and invoices -> orders both exist."""
    order_targets = {key.target_table for key in live_snapshot["orders"].foreign_keys}
    invoice_targets = {key.target_table for key in live_snapshot["invoices"].foreign_keys}
    assert "invoices" in order_targets
    assert "orders" in invoice_targets


@pytest.mark.integration
def test_reflect_captures_two_keys_to_the_same_table(live_snapshot: SchemaSnapshot):
    """orders references addresses twice: billing and shipping."""
    address_keys = [
        key for key in live_snapshot["orders"].foreign_keys if key.target_table == "addresses"
    ]
    assert len(address_keys) == 2
    assert {key.source_columns[0] for key in address_keys} == {
        "billing_address_id",
        "shipping_address_id",
    }


@pytest.mark.integration
def test_reflect_marks_nullability(live_snapshot: SchemaSnapshot):
    columns = {column.name: column for column in live_snapshot["orders"].columns}
    assert columns["customer_id"].nullable is False
    assert columns["invoice_id"].nullable is True


@pytest.mark.integration
def test_reflection_is_deterministic(engine: Engine):
    """Reflecting twice must produce the same fingerprint.

    If this is flaky, the version hash cannot be trusted to drive caching.
    """
    assert reflect_schema(engine).version == reflect_schema(engine).version


@pytest.mark.integration
def test_handbuilt_snapshot_matches_reality(
    live_snapshot: SchemaSnapshot, snapshot: SchemaSnapshot
):
    """Guard against the unit fixture drifting away from fixtures/schema.sql.

    Unit tests are only meaningful while the hand-built snapshot describes the
    same schema the integration tests reflect. Compares structure — table names
    and foreign-key topology — rather than exact type spellings, which are
    dialect-specific.
    """
    assert set(live_snapshot.tables) == set(snapshot.tables)

    def topology(snap: SchemaSnapshot) -> set[tuple[str, tuple[str, ...], str]]:
        return {
            (key.source_table, key.source_columns, key.target_table)
            for key in snap.foreign_keys
        }

    assert topology(live_snapshot) == topology(snapshot)
