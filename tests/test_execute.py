"""Tests for query execution and its runtime limits.

Most of these need a real database — the point of the module is what Postgres
does when a query misbehaves, which cannot be faked convincingly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from sqlagent.db.execute import ExecutionError, QueryResult, execute

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Successful execution
# --------------------------------------------------------------------------


def test_returns_columns_and_rows(seeded_engine: Engine):
    result = execute(seeded_engine, "SELECT id, name FROM customers ORDER BY id")

    assert result.columns == ("id", "name")
    assert result.rows[0] == (1, "Ada Lovelace")
    assert result.row_count == 3


def test_aggregate_query_returns_expected_totals(seeded_engine: Engine):
    """UK orders total 400, Germany 300 — fixed by the seed data."""
    sql = """
        SELECT r.name, SUM(o.total) AS total
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        JOIN regions r ON c.region_id = r.id
        GROUP BY r.name
        ORDER BY total DESC
    """
    result = execute(seeded_engine, sql)

    assert [(row[0], float(row[1])) for row in result.rows] == [
        ("United Kingdom", 400.0),
        ("Germany", 300.0),
    ]


def test_empty_result_is_not_an_error(seeded_engine: Engine):
    result = execute(seeded_engine, "SELECT * FROM orders WHERE total > 999999")

    assert result.row_count == 0
    assert result.columns


def test_records_elapsed_time(seeded_engine: Engine):
    assert execute(seeded_engine, "SELECT 1").seconds >= 0


# --------------------------------------------------------------------------
# Row cap
# --------------------------------------------------------------------------


def test_row_cap_truncates_and_reports_it(seeded_engine: Engine):
    """Reporting truncation honestly matters more than returning everything."""
    result = execute(seeded_engine, "SELECT * FROM generate_series(1, 100)", row_limit=10)

    assert result.row_count == 10
    assert result.truncated is True


def test_result_smaller_than_cap_is_not_marked_truncated(seeded_engine: Engine):
    result = execute(seeded_engine, "SELECT * FROM generate_series(1, 5)", row_limit=10)

    assert result.row_count == 5
    assert result.truncated is False


def test_result_exactly_at_the_cap_is_not_marked_truncated(seeded_engine: Engine):
    """Off-by-one guard: fetching cap+1 rows is what makes this distinguishable."""
    result = execute(seeded_engine, "SELECT * FROM generate_series(1, 10)", row_limit=10)

    assert result.row_count == 10
    assert result.truncated is False


# --------------------------------------------------------------------------
# Runtime protection
# --------------------------------------------------------------------------


def test_statement_timeout_is_enforced(seeded_engine: Engine):
    """A slow query is cancelled by Postgres rather than hanging the request."""
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT pg_sleep(5)", statement_timeout_ms=300)

    assert excinfo.value.kind == "timeout"
    assert "time limit" in str(excinfo.value)


def test_write_is_refused_by_the_read_only_transaction(seeded_engine: Engine):
    """The last line of defence.

    The validator would reject this statement long before here, and the database
    role should lack the privilege. This proves the third layer works on its
    own: even handed a DELETE directly, execution refuses it.
    """
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "DELETE FROM orders WHERE id = 1")

    assert excinfo.value.kind == "write_attempt"


def test_data_survives_the_refused_write(seeded_engine: Engine):
    """Proves the refusal above actually prevented the delete."""
    result = execute(seeded_engine, "SELECT count(*) FROM orders")
    assert result.rows[0][0] == 4


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------


def test_unknown_table_is_classified_as_missing_relation(seeded_engine: Engine):
    """This classification drives the decision to widen the schema context."""
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT * FROM shipments")

    assert excinfo.value.kind == "missing_relation"
    assert excinfo.value.sqlstate == "42P01"


def test_unknown_column_is_classified_as_missing_column(seeded_engine: Engine):
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT nonexistent_column FROM orders")

    assert excinfo.value.kind == "missing_column"


def test_syntax_error_is_classified(seeded_engine: Engine):
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT FROM WHERE")

    assert excinfo.value.kind == "syntax"


def test_error_message_is_one_line_not_a_traceback(seeded_engine: Engine):
    """Compact errors go into the repair prompt; a traceback would waste context."""
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT * FROM shipments")

    message = str(excinfo.value)
    assert "Traceback" not in message
    assert len(message) < 300


def test_error_includes_postgres_hint_when_offered(seeded_engine: Engine):
    """Postgres often suggests the column you meant; that hint is valuable."""
    with pytest.raises(ExecutionError) as excinfo:
        execute(seeded_engine, "SELECT custmer_id FROM orders")

    assert "hint" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# QueryResult helpers (pure, no database needed)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [None])
def test_to_dicts_pairs_columns_with_values(marker):
    result = QueryResult(
        columns=("id", "name"),
        rows=((1, "Ada"), (2, "Alan")),
        seconds=0.1,
        truncated=False,
    )

    assert result.to_dicts() == [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Alan"}]


def test_preview_renders_a_table():
    result = QueryResult(
        columns=("country", "total"),
        rows=(("UK", 400), ("DE", 300)),
        seconds=0.1,
        truncated=False,
    )

    preview = result.preview()

    assert "country | total" in preview
    assert "UK | 400" in preview


def test_preview_of_empty_result():
    result = QueryResult(columns=("a",), rows=(), seconds=0.0, truncated=False)
    assert result.preview() == "(no rows)"


def test_preview_truncates_long_cells():
    """A single huge text value must not dominate the prompt."""
    result = QueryResult(
        columns=("note",),
        rows=(("x" * 500,),),
        seconds=0.0,
        truncated=False,
    )

    preview = result.preview(max_cell_chars=20)

    assert "…" in preview
    assert len(preview) < 120


def test_preview_limits_row_count_and_says_how_many_remain():
    result = QueryResult(
        columns=("n",),
        rows=tuple((i,) for i in range(50)),
        seconds=0.0,
        truncated=False,
    )

    preview = result.preview(limit=5)

    assert "45 more rows" in preview


def test_preview_renders_nulls_explicitly():
    """A blank cell is ambiguous; NULL is not."""
    result = QueryResult(columns=("x",), rows=((None,),), seconds=0.0, truncated=False)
    assert "NULL" in result.preview()
