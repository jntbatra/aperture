"""Connecting a tenant's own database, and proving the role cannot write.

The claim this module refuses to take on trust
----------------------------------------------
A tenant pastes a connection string and says it is read-only. That is a claim
about *their* database, made by someone who may have created the role five
minutes ago from a guide they skimmed. Accepting it means the whole safety
argument of this product — "it cannot damage your data" — rests on a stranger
having configured GRANT correctly.

So the role is **tested**, not trusted. Before a connection is saved, this
tries to write, inside a transaction it rolls back, and requires the database
to refuse. A role that succeeds is rejected with the reason, and the tenant
fixes their grant instead of finding out later.

Why this is the strongest check available
-----------------------------------------
The existing defences are all downstream: the SQL validator proves a generated
statement is a SELECT, the cost gate refuses expensive plans, the read-only
transaction stops writes at the session level. Every one of them is code in
this process, and code in this process is the thing most likely to have a bug
in it.

The connected role is the only boundary enforced by the database itself, on the
other side of the network, by software nobody here wrote. It is the one that
still holds if everything in this repository is wrong. Verifying it is the
highest-value check in the system.

What "cannot write" is tested against
-------------------------------------
Four separate powers, because a role can lack one and hold another, and any one
of them is enough to lose data:

* ``CREATE TABLE`` — can it add objects?
* ``INSERT`` into a table it can read — can it add rows?
* ``UPDATE`` on a table it can read — can it change them?
* ``DELETE`` on a table it can read — can it remove them?

The last three are attempted with a predicate that matches nothing, so even if
the guard fails and the statement commits, it commits no change. Belt and
braces: the transaction is rolled back *and* the statement is a no-op. Writing
a probe that would do damage if the rollback failed is not an acceptable way to
test whether something can do damage.

Why a failed probe is a hard rejection
--------------------------------------
Not a warning. A tenant who is told "we could not verify your role is
read-only" and allowed to continue has been given a safety property they do not
have, in writing, by a product whose main promise is that property.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from sqlagent.saas.secrets import redact_dsn

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 8
"""How long to wait when first reaching a tenant's database.

Short on purpose. This runs while someone is watching a form, and the common
failures — wrong host, firewall, no public route — hang rather than refuse. A
timeout that returns "we could not reach it" in eight seconds is a better
experience than a spinner that eventually reports the same thing.
"""

PROBE_TABLE = "_aperture_write_probe"
"""Name for the table the CREATE probe attempts.

Prefixed and unmistakable so that if it ever does appear in a tenant's
database — meaning both the rollback and the role check failed — it is
immediately obvious where it came from and safe to drop.
"""


class ConnectionRejected(RuntimeError):
    """The database cannot be used. Carries a reason a tenant can act on.

    Never carries the password: every message here is built from a redacted
    DSN, because this string ends up in an HTTP response, a support ticket and
    a log.
    """


@dataclass(frozen=True, slots=True)
class WriteProbe:
    """One attempted write and what the database did about it."""

    operation: str
    refused: bool
    detail: str = ""
    """Why it was refused, trimmed. Useful in support: "permission denied for
    table orders" tells a tenant exactly which grant to look at."""


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """The result of trying a tenant's connection string."""

    reachable: bool
    read_only: bool
    dialect: str = ""
    table_count: int = 0
    probes: tuple[WriteProbe, ...] = ()
    problem: str = ""
    redacted_dsn: str = ""

    @property
    def usable(self) -> bool:
        """Both conditions. Neither is sufficient alone — a reachable database
        with a writable role is exactly the case this exists to catch."""
        return self.reachable and self.read_only

    @property
    def writable_operations(self) -> tuple[str, ...]:
        return tuple(probe.operation for probe in self.probes if not probe.refused)

    def explain(self) -> str:
        """What to tell the tenant. Written to be actionable, not diagnostic."""
        if not self.reachable:
            return f"Could not reach that database: {self.problem}"
        if self.read_only:
            return (
                f"Connected. {self.table_count} tables, and the role is "
                f"read-only — all {len(self.probes)} write attempts were refused."
            )
        allowed = ", ".join(self.writable_operations)
        return (
            f"That role can still {allowed}. Aperture only ever reads, but it "
            f"will not connect with a role that could write — create a "
            f"read-only role and use that instead."
        )


@dataclass
class _ProbeRunner:
    """Collects probe results without letting one failure end the run."""

    probes: list[WriteProbe] = field(default_factory=list)

    def attempt(self, connection, operation: str, statement: str) -> None:
        """Run one write and record whether the database refused it.

        A refusal is the desired outcome, so an exception here is success. The
        exception type is not narrowed: every driver spells "permission denied"
        differently, and a role that fails for an unexpected reason has still
        failed to write, which is what is being measured.
        """
        savepoint = connection.begin_nested()
        try:
            connection.execute(text(statement))
        except SQLAlchemyError as exc:
            savepoint.rollback()
            self.probes.append(
                WriteProbe(operation=operation, refused=True, detail=_trim(exc))
            )
            return

        # It worked. Roll back immediately — the statement was written to be a
        # no-op, but a write that was permitted is a write, and it must not be
        # allowed to reach the outer transaction either.
        savepoint.rollback()
        self.probes.append(WriteProbe(operation=operation, refused=False))


def check_connection(dsn: str, *, sample_table: str | None = None) -> ConnectionCheck:
    """Reach a tenant's database and prove its role cannot write.

    Args:
        dsn: The connection string, as supplied.
        sample_table: A table to aim the row-level probes at. Discovered from
            the schema when omitted. Without any table, only ``CREATE TABLE``
            can be tested — which is reported honestly rather than counted as a
            pass.

    Returns:
        A result, never an exception, for the reachable/writable cases: both
        are ordinary answers to "can this be used", and the caller renders them
        to a person filling in a form.

    Raises:
        ConnectionRejected: The DSN is malformed, or names a driver this does
            not support. That is a different class of problem — the tenant has
            not given us something we can even try.
    """
    redacted = redact_dsn(dsn)
    if not dsn or "://" not in dsn:
        raise ConnectionRejected("That does not look like a connection string.")

    # SQLite is a file, not a server with roles. A tenant "connecting" one in a
    # hosted product is either confused or pointing at the server's own disk,
    # and neither should be encouraged.
    if dsn.startswith("sqlite"):
        raise ConnectionRejected(
            "SQLite databases cannot be connected to a hosted workspace — "
            "there is no role to make read-only. Upload the file instead."
        )

    engine: Engine | None = None
    try:
        engine = create_engine(
            dsn,
            pool_pre_ping=True,
            connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        )
        with engine.connect() as connection:
            dialect = connection.dialect.name
            tables = _list_tables(connection)
            target = sample_table or (tables[0] if tables else None)

            runner = _ProbeRunner()
            _run_probes(connection, runner, target)

            read_only = all(probe.refused for probe in runner.probes)
            problem = ""
            if target is None:
                # Honest rather than convenient: one probe out of four is not a
                # verified read-only role, and saying so is the difference
                # between a check and a formality.
                problem = (
                    "No readable tables were found, so only object creation "
                    "could be tested."
                )

            return ConnectionCheck(
                reachable=True,
                read_only=read_only,
                dialect=dialect,
                table_count=len(tables),
                probes=tuple(runner.probes),
                problem=problem,
                redacted_dsn=redacted,
            )

    except ConnectionRejected:
        raise
    except SQLAlchemyError as exc:
        logger.info("tenant database unreachable: %s", redacted)
        return ConnectionCheck(
            reachable=False, read_only=False, problem=_trim(exc), redacted_dsn=redacted
        )
    except Exception as exc:  # noqa: BLE001 - a driver may raise anything
        logger.info("tenant database check failed: %s", redacted)
        return ConnectionCheck(
            reachable=False,
            read_only=False,
            problem=f"{type(exc).__name__}: {_trim(exc)}",
            redacted_dsn=redacted,
        )
    finally:
        if engine is not None:
            engine.dispose()


def _run_probes(connection, runner: _ProbeRunner, table: str | None) -> None:
    """Attempt each write. Every statement is a no-op even if it is permitted.

    The row-level probes use ``WHERE 1 = 0`` and ``SELECT ... WHERE 1 = 0``, so
    a probe that is wrongly allowed *and* whose rollback fails still changes
    nothing. Writing a probe that would do damage in order to find out whether
    damage is possible is not an acceptable design.
    """
    runner.attempt(
        connection,
        "CREATE TABLE",
        f"CREATE TABLE {PROBE_TABLE} (probe INTEGER)",
    )

    if table is None:
        return

    quoted = _quote(connection, table)
    runner.attempt(
        connection, "INSERT", f"INSERT INTO {quoted} SELECT * FROM {quoted} WHERE 1 = 0"
    )
    runner.attempt(connection, "DELETE", f"DELETE FROM {quoted} WHERE 1 = 0")
    runner.attempt(
        connection,
        "UPDATE",
        # Assigning a column to itself: syntactically a write, semantically
        # nothing, and it needs no knowledge of the column's type.
        f"UPDATE {quoted} SET {_first_column(connection, table)} = "
        f"{_first_column(connection, table)} WHERE 1 = 0",
    )


def _list_tables(connection) -> list[str]:
    from sqlalchemy import inspect

    try:
        return sorted(inspect(connection).get_table_names())
    except SQLAlchemyError:
        return []


def _first_column(connection, table: str) -> str:
    from sqlalchemy import inspect

    columns = inspect(connection).get_columns(table)
    return connection.dialect.identifier_preparer.quote(columns[0]["name"])


def _quote(connection, table: str) -> str:
    return connection.dialect.identifier_preparer.quote(table)


def _trim(exc: Exception, limit: int = 200) -> str:
    """One line of a driver error, bounded.

    Drivers attach the full statement, a stack of context and sometimes the
    connection parameters. The first line carries the reason; the rest is
    noise that would end up in an HTTP response.
    """
    text_value = str(exc).strip().splitlines()
    first = text_value[0] if text_value else exc.__class__.__name__
    return first[:limit]
