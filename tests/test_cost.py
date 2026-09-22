"""Tests for the planner cost gate.

The gate runs ``EXPLAIN`` before executing, so the obvious disasters — a cross
join, an unfiltered scan of everything — are refused for the price of one cheap
round trip rather than thirty seconds of load stopped partway through by a
timeout.

These exercise the parsing and the thresholds against a stub engine. The
PostgreSQL round trip itself is covered by the integration tests, which run
against a real database.
"""

from __future__ import annotations

import pytest

from sqlagent.db.dialects import MYSQL, POSTGRES, SQLITE
from sqlagent.guards.cost import CostRejected, check, estimate


class StubEngine:
    """Returns a fixed EXPLAIN output, or raises."""

    def __init__(self, rows=None, *, fail: bool = False):
        self._rows = rows if rows is not None else []
        self._fail = fail

    def connect(self):
        engine = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                if engine._fail:
                    raise RuntimeError("EXPLAIN not permitted")
                return Result(engine._rows)

        return Connection()


class Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


PLAN = [("Seq Scan on orders  (cost=0.00..123456.78 rows=999999 width=40)",)]


# --------------------------------------------------------------------------
# Reading a plan
# --------------------------------------------------------------------------


def test_cost_and_rows_are_parsed_from_the_first_line():
    """The first line is the total for the whole plan."""
    cost, rows = estimate(StubEngine(PLAN), "SELECT 1", dialect=POSTGRES)

    assert cost == pytest.approx(123456.78)
    assert rows == 999999


def test_a_dialect_without_usable_explain_abstains():
    """SQLite's EXPLAIN emits bytecode with no cost estimate — nothing to gate
    on, so the timeout does the work alone."""
    assert estimate(StubEngine(PLAN), "SELECT 1", dialect=SQLITE) == (None, None)
    assert estimate(StubEngine(PLAN), "SELECT 1", dialect=MYSQL) == (None, None)


def test_a_failing_explain_abstains_rather_than_failing_the_query():
    """A permission quirk must not turn a runnable query into a failed one."""
    assert estimate(StubEngine(fail=True), "SELECT 1", dialect=POSTGRES) == (None, None)


def test_an_empty_plan_abstains():
    assert estimate(StubEngine([]), "SELECT 1", dialect=POSTGRES) == (None, None)


def test_an_unparseable_plan_abstains():
    engine = StubEngine([("something unexpected",)])

    assert estimate(engine, "SELECT 1", dialect=POSTGRES) == (None, None)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_a_cheap_query_passes():
    engine = StubEngine([("Index Scan (cost=0.00..8.30 rows=1 width=40)",)])

    check(engine, "SELECT 1", dialect=POSTGRES, max_cost=1000, max_estimated_rows=1000)


def test_an_expensive_query_is_rejected():
    with pytest.raises(CostRejected):
        check(
            StubEngine(PLAN),
            "SELECT 1",
            dialect=POSTGRES,
            max_cost=1000,
            max_estimated_rows=0,
        )


def test_the_rejection_names_the_estimate_and_the_ceiling():
    """The message goes into a repair prompt. "Too expensive" tells the model
    nothing; a number and a limit tell it to add a filter."""
    with pytest.raises(CostRejected) as caught:
        check(
            StubEngine(PLAN),
            "SELECT 1",
            dialect=POSTGRES,
            max_cost=1000,
            max_estimated_rows=0,
        )

    message = str(caught.value)
    assert "123,457" in message
    assert "1,000" in message


def test_too_many_estimated_rows_is_rejected():
    with pytest.raises(CostRejected) as caught:
        check(
            StubEngine(PLAN),
            "SELECT 1",
            dialect=POSTGRES,
            max_cost=0,
            max_estimated_rows=1000,
        )

    assert "999,999 rows" in str(caught.value)


def test_a_rejection_routes_as_a_loop_a_failure():
    """The schema was fine; the query was not. Regenerating with the estimate
    attached is the right repair."""
    with pytest.raises(CostRejected) as caught:
        check(
            StubEngine(PLAN), "SELECT 1", dialect=POSTGRES,
            max_cost=1, max_estimated_rows=0,
        )

    assert caught.value.kind == "cost_too_high"


def test_zero_thresholds_disable_the_gate_without_an_explain():
    """Both off means no round trip at all, not a round trip that ignores the
    answer."""
    check(
        StubEngine(fail=True),
        "SELECT 1",
        dialect=POSTGRES,
        max_cost=0,
        max_estimated_rows=0,
    )


def test_an_unreadable_plan_does_not_block_execution():
    engine = StubEngine([("no numbers here",)])

    check(engine, "SELECT 1", dialect=POSTGRES, max_cost=1, max_estimated_rows=1)
