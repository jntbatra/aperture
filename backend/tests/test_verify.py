import sqlite3

import pytest

from aperture.db.connection import Database
from aperture.db.introspect import ColumnInfo, SchemaSnapshot, TableInfo
from aperture.verify import verify


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER, rider TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, order_id INTEGER, kind TEXT);
        INSERT INTO orders VALUES (1, 100, 'r1'), (2, 200, NULL), (3, 300, NULL);
        INSERT INTO events VALUES
            (1, 1, 'a'), (2, 1, 'b'), (3, 1, 'c'),
            (4, 2, 'a'), (5, 2, 'b'), (6, 3, 'a');
        CREATE TABLE riders (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO riders VALUES (1, 'r1');
        """
    )
    connection.commit()
    connection.close()
    return Database(f"sqlite:///{path}")


@pytest.fixture
def snapshot():
    snap = SchemaSnapshot(dialect="sqlite")
    snap.tables["orders"] = TableInfo(
        name="orders",
        columns=[
            ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
            ColumnInfo(name="amount", data_type="INTEGER"),
            ColumnInfo(name="rider", data_type="TEXT"),
        ],
    )
    snap.tables["riders"] = TableInfo(
        name="riders",
        columns=[
            ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
            ColumnInfo(name="name", data_type="TEXT"),
        ],
    )
    snap.tables["events"] = TableInfo(
        name="events",
        columns=[
            ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
            ColumnInfo(name="order_id", data_type="INTEGER"),
        ],
    )
    return snap


def test_fan_out_is_detected(db, snapshot):
    # 3 orders join to 6 events, so SUM(amount) doubles.
    sql = "SELECT SUM(o.amount) FROM orders o JOIN events e ON e.order_id = o.id"
    findings = verify(db, sql, snapshot, dialect="sqlite")
    assert [f.kind for f in findings] == ["fan_out"]
    assert "2.0x" in findings[0].message


def test_fan_out_detected_without_an_alias(db, snapshot):
    sql = "SELECT SUM(orders.amount) FROM orders JOIN events ON events.order_id = orders.id"
    assert [f.kind for f in verify(db, sql, snapshot, dialect="sqlite")] == ["fan_out"]


def test_no_finding_without_an_aggregate(db, snapshot):
    sql = "SELECT o.id FROM orders o JOIN events e ON e.order_id = o.id"
    assert verify(db, sql, snapshot, dialect="sqlite") == []


def test_no_finding_for_an_unjoined_aggregate(db, snapshot):
    assert verify(db, "SELECT SUM(amount) FROM orders", snapshot, dialect="sqlite") == []


def test_null_keys_are_reported_as_a_bucket_not_as_loss(db, snapshot):
    # A GROUP BY keeps NULL keys as their own group: nothing is dropped, but a
    # large unlabelled bucket is easy to misread as a category.
    sql = "SELECT rider, count(*) FROM orders GROUP BY rider"
    findings = verify(db, sql, snapshot, dialect="sqlite")
    assert [f.kind for f in findings] == ["null_group"]
    assert "counted" in findings[0].message


def test_group_by_without_nulls_is_silent(db, snapshot):
    sql = "SELECT amount, count(*) FROM orders GROUP BY amount"
    assert verify(db, sql, snapshot, dialect="sqlite") == []


def test_inner_join_discarding_rows_is_reported(db, snapshot):
    # Only order 1 has a rider, so joining discards two of three orders.
    sql = "SELECT o.id FROM orders o JOIN riders r ON r.name = o.rider"
    findings = verify(db, sql, snapshot, dialect="sqlite")
    assert [f.kind for f in findings] == ["join_excluded_rows"]
    assert "67%" in findings[0].message


def test_left_join_is_not_reported(db, snapshot):
    sql = "SELECT o.id FROM orders o LEFT JOIN riders r ON r.name = o.rider"
    assert [f.kind for f in verify(db, sql, snapshot, dialect="sqlite")] == []


def test_unparseable_sql_yields_no_findings(db, snapshot):
    assert verify(db, "NOT SQL", snapshot, dialect="sqlite") == []
