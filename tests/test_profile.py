"""Tests for column value profiling.

Profiling replaces "here are two whole rows" with "here is what each column can
contain", because a WHERE clause needs vocabulary rather than a specimen.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from sqlagent.db.profile import ColumnProfile, TableProfile, profile_tables

pytestmark = pytest.mark.integration


def by_name(profile: TableProfile) -> dict[str, ColumnProfile]:
    return {column.name: column for column in profile.columns}


def profile_one(engine: Engine, table: str, **kwargs) -> TableProfile:
    profiles = profile_tables(engine, [table], **kwargs)
    assert profiles, f"no profile produced for {table}"
    return profiles[0]


def test_low_cardinality_column_lists_every_value(seeded_engine: Engine):
    """regions.iso_code holds exactly GB and DE in the fixture."""
    columns = by_name(profile_one(seeded_engine, "regions"))

    assert columns["iso_code"].exhaustive is True
    assert set(columns["iso_code"].values) == {"'GB'", "'DE'"}


def test_high_cardinality_column_shows_only_examples(seeded_engine: Engine):
    """An identifier teaches nothing about filtering, so it is not enumerated."""
    columns = by_name(
        profile_one(seeded_engine, "orders", max_distinct=2, example_values=2)
    )

    assert columns["id"].exhaustive is False
    assert len(columns["id"].values) == 2


def test_strings_are_quoted_and_numbers_are_not(seeded_engine: Engine):
    """A filter on 2024 and on '2024' are different queries."""
    columns = by_name(profile_one(seeded_engine, "regions"))

    assert all(v.startswith("'") for v in columns["name"].values)
    assert not any(v.startswith("'") for v in columns["id"].values)


def test_nulls_are_shown_explicitly(seeded_engine: Engine):
    """employees.manager_id is NULL for the one employee with no manager."""
    columns = by_name(profile_one(seeded_engine, "employees"))

    assert "NULL" in columns["manager_id"].values


def test_excluded_columns_never_appear(seeded_engine: Engine):
    """The privacy control: a named column is not read into the output."""
    profile = profile_one(seeded_engine, "customers", exclude_columns={"email"})

    assert "email" not in by_name(profile)
    assert "name" in by_name(profile)


def test_long_values_are_truncated(seeded_engine: Engine):
    """audit_log.action holds a 500-character value in the fixture."""
    columns = by_name(profile_one(seeded_engine, "audit_log", max_cell_chars=20))

    assert all(len(v) < 40 for v in columns["action"].values)


def test_empty_table_is_handled(seeded_engine: Engine):
    """A table with no rows must profile cleanly rather than raising.

    The probe table is dropped again in a finally block. Creating it and
    leaving it behind would change the schema that other tests reflect and
    compare against — which is exactly what happened the first time this test
    was written.
    """
    from sqlalchemy import text

    with seeded_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS empty_probe"))
        connection.execute(text("CREATE TABLE empty_probe (id INTEGER)"))

    try:
        profile = profile_one(seeded_engine, "empty_probe")
        assert profile.sampled_rows == 0
        assert "empty table" in profile.render()
    finally:
        with seeded_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS empty_probe"))


def test_unreadable_table_is_skipped_not_fatal(seeded_engine: Engine):
    """A profile is an optimisation; failing to build one must not break a request."""
    profiles = profile_tables(seeded_engine, ["does_not_exist", "regions"])

    assert [p.table for p in profiles] == ["regions"]


def test_render_marks_example_lists_as_non_exhaustive():
    """Claiming a partial list is complete would produce an IN() that drops rows."""
    partial = ColumnProfile(name="id", values=("1", "2"), exhaustive=False)
    complete = ColumnProfile(name="sex", values=("'M'", "'F'"), exhaustive=True)

    assert "e.g." in partial.render()
    assert "e.g." not in complete.render()


def test_render_separates_categorical_from_example_columns(seeded_engine: Engine):
    rendered = profile_one(seeded_engine, "regions").render()

    assert "small fixed set of values" in rendered
