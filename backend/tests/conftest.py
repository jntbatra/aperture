"""Fixtures built from a hand-made snapshot, so tests need no live database."""

from __future__ import annotations

import pytest

from aperture.db.introspect import ColumnInfo, ForeignKey, SchemaSnapshot, TableInfo
from aperture.db.profile import ColumnProfile, DatabaseProfile, TableProfile


@pytest.fixture
def snapshot() -> SchemaSnapshot:
    snap = SchemaSnapshot(dialect="postgresql")
    snap.tables["orders"] = TableInfo(
        name="orders",
        approx_rows=2999,
        columns=[
            ColumnInfo(name="id", data_type="text", is_pk=True),
            ColumnInfo(name="userId", data_type="text", is_fk=True),
            ColumnInfo(name="createdAt", data_type="timestamp"),
            ColumnInfo(
                name="status",
                data_type="OrderStatus",
                enum_values=["DELIVERED", "CANCELLED", "CONFUSED_CUSTOMER"],
            ),
            ColumnInfo(name="totalAmount", data_type="integer"),
        ],
    )
    snap.tables["users"] = TableInfo(
        name="users",
        approx_rows=2297,
        columns=[
            ColumnInfo(name="id", data_type="text", is_pk=True),
            ColumnInfo(name="email", data_type="text"),
            ColumnInfo(name="role", data_type="Role", enum_values=["CUSTOMER", "RIDER"]),
        ],
    )
    snap.tables["order_status_history"] = TableInfo(
        name="order_status_history",
        approx_rows=16812,
        columns=[
            ColumnInfo(name="id", data_type="text", is_pk=True),
            ColumnInfo(name="orderId", data_type="text", is_fk=True),
        ],
    )
    snap.tables["refunds"] = TableInfo(
        name="refunds",
        approx_rows=0,
        columns=[ColumnInfo(name="id", data_type="text", is_pk=True)],
    )
    snap.foreign_keys = [
        ForeignKey("orders", "userId", "users", "id"),
        ForeignKey("order_status_history", "orderId", "orders", "id"),
        ForeignKey("refunds", "orderId", "orders", "id"),
    ]
    return snap


@pytest.fixture
def profile() -> DatabaseProfile:
    prof = DatabaseProfile()

    orders = TableProfile(table="orders", exact_rows=2999)
    orders.columns["status"] = ColumnProfile(
        table="orders",
        column="status",
        common_values=["DELIVERED", "CANCELLED", "CONFUSED_CUSTOMER"],
    )
    orders.columns["createdAt"] = ColumnProfile(
        table="orders",
        column="createdAt",
        min_value="2026-05-09 10:00:00",
        max_value="2026-09-17 23:00:00",
    )
    prof.tables["orders"] = orders

    users = TableProfile(table="users", exact_rows=2297)
    users.columns["role"] = ColumnProfile(table="users", column="role", common_values=["CUSTOMER", "RIDER"])
    users.columns["email"] = ColumnProfile(table="users", column="email", sensitive=True)
    prof.tables["users"] = users

    prof.tables["order_status_history"] = TableProfile(table="order_status_history", exact_rows=16812)
    prof.tables["refunds"] = TableProfile(table="refunds", exact_rows=0)
    return prof
