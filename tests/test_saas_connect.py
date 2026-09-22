"""Tests for connecting a tenant's database and proving the role cannot write.

A tenant pastes a connection string and says it is read-only. That is a claim
about their database by someone who may have created the role five minutes ago.
The whole safety argument of the product — "it cannot damage your data" — must
not rest on a stranger having configured GRANT correctly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from sqlagent.saas.connect import (
    PROBE_TABLE,
    ConnectionCheck,
    ConnectionRejected,
    WriteProbe,
    check_connection,
)


def check(**kwargs) -> ConnectionCheck:
    base = {"reachable": True, "read_only": True, "table_count": 3}
    return ConnectionCheck(**{**base, **kwargs})


def refused(*operations) -> tuple[WriteProbe, ...]:
    return tuple(WriteProbe(operation=op, refused=True) for op in operations)


def allowed(*operations) -> tuple[WriteProbe, ...]:
    return tuple(WriteProbe(operation=op, refused=False) for op in operations)


# --------------------------------------------------------------------------
# What counts as usable
# --------------------------------------------------------------------------


def test_reachable_and_read_only_is_usable():
    assert check(probes=refused("INSERT")).usable


def test_reachable_but_writable_is_not_usable():
    """The case this module exists for. A reachable database with a writable
    role is exactly what would otherwise sail through."""
    assert not check(read_only=False, probes=allowed("INSERT")).usable


def test_unreachable_is_not_usable():
    assert not check(reachable=False, read_only=True).usable


def test_the_writable_operations_are_named():
    """So the tenant knows which grant to look at."""
    result = check(
        read_only=False, probes=allowed("INSERT", "UPDATE") + refused("CREATE TABLE")
    )

    assert result.writable_operations == ("INSERT", "UPDATE")


# --------------------------------------------------------------------------
# What the tenant is told
# --------------------------------------------------------------------------


def test_a_good_connection_says_what_was_verified():
    message = check(probes=refused("CREATE TABLE", "INSERT", "UPDATE", "DELETE")).explain()

    assert "read-only" in message
    assert "4 write attempts" in message


def test_a_writable_role_is_told_what_to_do():
    """Actionable, not diagnostic."""
    message = check(read_only=False, probes=allowed("INSERT", "DELETE")).explain()

    assert "INSERT, DELETE" in message
    assert "read-only role" in message


def test_an_unreachable_database_reports_the_reason():
    assert "Could not reach" in check(reachable=False, problem="timeout").explain()


# --------------------------------------------------------------------------
# Refusals before anything is attempted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dsn", ["", "not a dsn", "just-a-host:5432"])
def test_a_malformed_dsn_is_rejected(dsn):
    with pytest.raises(ConnectionRejected):
        check_connection(dsn)


def test_sqlite_is_refused_with_a_reason():
    """A file has no role to make read-only, and a tenant "connecting" one in
    a hosted product is pointing at our disk."""
    with pytest.raises(ConnectionRejected, match="no role"):
        check_connection("sqlite:///somewhere.db")


def test_the_rejection_suggests_the_thing_that_does_work():
    with pytest.raises(ConnectionRejected, match="[Uu]pload"):
        check_connection("sqlite:///somewhere.db")


# --------------------------------------------------------------------------
# Nothing leaks
# --------------------------------------------------------------------------


def test_an_unreachable_result_carries_a_redacted_dsn():
    result = check_connection("postgresql+psycopg://u:hunter2@127.0.0.1:1/none")

    assert not result.reachable
    assert "hunter2" not in result.redacted_dsn
    assert "hunter2" not in result.explain()


def test_the_redacted_dsn_still_identifies_the_database():
    result = check_connection("postgresql+psycopg://reader:hunter2@127.0.0.1:1/sales")

    assert "127.0.0.1" in result.redacted_dsn
    assert "sales" in result.redacted_dsn


def test_an_unreachable_database_is_a_result_not_an_exception():
    """Both "cannot reach" and "can write" are ordinary answers to "can this be
    used", rendered to a person filling in a form."""
    assert check_connection("postgresql+psycopg://u:p@127.0.0.1:1/none").reachable is False


# --------------------------------------------------------------------------
# Against a real database
# --------------------------------------------------------------------------


@pytest.fixture
def writable_sqlite(tmp_path):
    """SQLite is refused by `check_connection`, so the probes are exercised
    directly here. The point is the probe *statements*, not the dialect."""
    path = tmp_path / "probe.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, total INTEGER)"))
        connection.execute(text("INSERT INTO orders VALUES (1, 100), (2, 200)"))
    return engine


def test_the_probes_change_nothing_even_when_permitted(writable_sqlite):
    """The row-level probes are written as no-ops — `WHERE 1 = 0` — so a probe
    that is wrongly allowed *and* whose rollback fails still changes nothing.
    Writing a probe that would do damage in order to find out whether damage is
    possible is not an acceptable design."""
    from sqlagent.saas.connect import _ProbeRunner, _run_probes

    with writable_sqlite.connect() as connection, connection.begin():
        runner = _ProbeRunner()
        _run_probes(connection, runner, "orders")

    with writable_sqlite.connect() as connection:
        rows = connection.execute(text("SELECT id, total FROM orders ORDER BY id")).all()
        tables = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).scalars().all()

    assert rows == [(1, 100), (2, 200)]
    assert PROBE_TABLE not in tables


def test_a_writable_role_is_detected(writable_sqlite):
    """The half that matters. A check that only ever confirms read-only roles
    has not been tested."""
    from sqlagent.saas.connect import _ProbeRunner, _run_probes

    with writable_sqlite.connect() as connection, connection.begin():
        runner = _ProbeRunner()
        _run_probes(connection, runner, "orders")

    assert runner.probes
    assert not all(probe.refused for probe in runner.probes)


def test_all_four_powers_are_tested(writable_sqlite):
    """A role can lack one and hold another, and any one of them loses data."""
    from sqlagent.saas.connect import _ProbeRunner, _run_probes

    with writable_sqlite.connect() as connection, connection.begin():
        runner = _ProbeRunner()
        _run_probes(connection, runner, "orders")

    assert {probe.operation for probe in runner.probes} == {
        "CREATE TABLE",
        "INSERT",
        "UPDATE",
        "DELETE",
    }


def test_with_no_readable_table_only_creation_can_be_tested(writable_sqlite):
    """Reported honestly rather than counted as a pass. One probe out of four
    is not a verified read-only role."""
    from sqlagent.saas.connect import _ProbeRunner, _run_probes

    with writable_sqlite.connect() as connection, connection.begin():
        runner = _ProbeRunner()
        _run_probes(connection, runner, None)

    assert [probe.operation for probe in runner.probes] == ["CREATE TABLE"]
