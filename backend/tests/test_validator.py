import pytest

from aperture.guards.validator import validate_sql

PG = {"dialect": "postgres"}


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("delete from orders", "write"),
        ("update orders set status = 'X'", "write"),
        ("drop table orders", "write"),
        ("with x as (delete from orders returning 1) select * from x", "write"),
        ("select * into copies from orders", "write"),
        ("select 1; drop table orders", "multi_statement"),
        ("select pg_sleep(10)", "banned_function"),
        ("select * from orders for update", "locking"),
        ("SELECT", "no_projection"),
        ("", "empty"),
        ("not sql at all", "parse"),
    ],
)
def test_rejects(sql, kind):
    result = validate_sql(sql, **PG)
    assert not result.ok
    assert result.kind == kind


def test_accepts_read_query_and_injects_limit():
    result = validate_sql("select id from orders", row_limit=50, **PG)
    assert result.ok
    assert result.limit_injected
    assert "LIMIT 50" in result.sql


def test_existing_limit_is_left_alone():
    result = validate_sql("select id from orders limit 5", **PG)
    assert result.ok
    assert not result.limit_injected


def test_reports_referenced_tables_and_columns():
    result = validate_sql("select o.id from orders o join users u on u.id = o.userId", **PG)
    assert result.ok
    assert set(result.tables) == {"orders", "users"}
    assert "userId" in result.columns


def test_write_is_not_repairable():
    assert not validate_sql("delete from orders", **PG).repairable
    assert validate_sql("SELECT", **PG).repairable


def test_dialect_is_respected():
    # SQLite has no FOR UPDATE, so the same text parses differently.
    result = validate_sql("select id from orders limit 1", dialect="sqlite")
    assert result.ok
