"""Rewriting a filter whose literal does not exist in the column.

`WHERE category = 'Cravings Deals'` against a column storing
`'Cravings Deals ⭐'` parses, passes every guard, executes without error, and
returns nothing — and that nothing was reported to a user as the answer.
The database can settle it; a model reading the SQL cannot.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from sqlagent.guards.rebind import propose


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE promo (id INTEGER, category TEXT, tier TEXT)"))
        connection.execute(
            text(
                "INSERT INTO promo VALUES "
                "(1, 'Cravings Deals ⭐', 'gold'), "
                "(2, 'Weekend Specials \U0001f525', 'gold'), "
                "(3, 'Biryani', 'silver')"
            )
        )
        yield connection


def test_a_decorated_value_is_recovered(connection):
    """The real failure, and the whole reason this module exists."""
    out = propose(
        connection,
        sql="SELECT id FROM promo WHERE category = 'Cravings Deals'",
        dialect="sqlite",
    )

    assert out is not None
    rewritten, notes = out
    assert "Cravings Deals ⭐" in rewritten
    assert "'Cravings Deals' -> 'Cravings Deals ⭐'" in notes[0]


def test_the_rewrite_actually_returns_rows(connection):
    """The point is not a prettier string, it is a query that answers."""
    rewritten, _ = propose(
        connection,
        sql="SELECT id FROM promo WHERE category = 'Cravings Deals'",
        dialect="sqlite",
    )

    assert connection.execute(text(rewritten)).fetchall() == [(1,)]


def test_a_value_that_exists_is_left_alone(connection):
    """A query returning rows is not this failure, whatever else is wrong with
    it. Rewriting a working result on a similarity score is how a repair pass
    starts breaking correct queries."""
    assert (
        propose(
            connection,
            sql="SELECT id FROM promo WHERE category = 'Biryani'",
            dialect="sqlite",
        )
        is None
    )


def test_a_value_resembling_nothing_is_left_alone(connection):
    """The unmatched-literal signal survives: no rewrite, so the caller still
    learns the filter matches nothing."""
    assert (
        propose(
            connection,
            sql="SELECT id FROM promo WHERE category = 'Sushi Platter'",
            dialect="sqlite",
        )
        is None
    )


def test_several_literals_are_rebound_in_one_pass(connection):
    out = propose(
        connection,
        sql=(
            "SELECT id FROM promo WHERE category = 'Cravings Deals' "
            "OR category = 'Weekend Specials'"
        ),
        dialect="sqlite",
    )

    rewritten, notes = out
    assert len(notes) == 2
    assert connection.execute(text(rewritten)).fetchall() == [(1,), (2,)]


def test_an_unqualified_column_in_a_single_table_query_resolves(connection):
    out = propose(
        connection,
        sql="SELECT id FROM promo WHERE promo.category = 'Cravings Deals'",
        dialect="sqlite",
    )

    assert out is not None


def test_an_unqualified_column_in_a_join_is_skipped(connection):
    """Resolving it needs more schema than belongs here, and guessing rewrites
    the wrong table's predicate."""
    connection.execute(text("CREATE TABLE other (id INTEGER, category TEXT)"))

    assert (
        propose(
            connection,
            sql=(
                "SELECT p.id FROM promo p JOIN other o ON o.id = p.id "
                "WHERE category = 'Cravings Deals'"
            ),
            dialect="sqlite",
        )
        is None
    )


def test_unparseable_sql_is_not_an_error(connection):
    assert propose(connection, sql="not sql at all ((", dialect="sqlite") is None


def test_a_missing_table_does_not_raise(connection):
    assert (
        propose(
            connection,
            sql="SELECT id FROM nosuchtable WHERE category = 'x'",
            dialect="sqlite",
        )
        is None
    )


def test_a_numeric_literal_is_not_touched(connection):
    """A number absent from a column is ordinary — nobody is 200 years old —
    and there is no near-match notion for one."""
    assert (
        propose(connection, sql="SELECT id FROM promo WHERE id = 99", dialect="sqlite")
        is None
    )
