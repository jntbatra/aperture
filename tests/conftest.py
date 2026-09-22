"""Shared test fixtures.

Two kinds of test live in this suite:

* **Unit tests** build a :class:`SchemaSnapshot` by hand. No database, no
  network, millisecond runtime. Most tests are these.
* **Integration tests** reflect a real Postgres instance, because that is the
  only way to prove the reflection code actually reads a live catalog
  correctly. They are marked ``@pytest.mark.integration`` and skip themselves
  when no database is reachable, so ``pytest`` still passes on a bare checkout.

To run integration tests locally:

    docker run -d --name sqlagent-test-pg \\
        -e POSTGRES_PASSWORD=testpass -e POSTGRES_USER=testuser \\
        -e POSTGRES_DB=testdb -p 5434:5432 postgres:16-alpine
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from sqlagent.schema.graph import build_graph
from sqlagent.schema.introspect import (
    Column,
    ForeignKey,
    SchemaSnapshot,
    Table,
    compute_version,
    reflect_schema,
)

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://testuser:testpass@localhost:5434/testdb"


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch):
    """Keep the developer's own configuration out of the test suite.

    ``Settings`` reads ``.env`` and ``SQLAGENT_*`` environment variables, which
    means a working local configuration silently changes what the tests
    exercise. That is not hypothetical: adding a glossary path to ``.env`` for a
    real database broke two cache tests, because the agent then hashed a
    non-empty glossary into its cache key while the tests computed the key with
    an empty one.

    The failure was in the tests, but the fragility was real — a suite whose
    behaviour depends on an untracked file passes or fails for reasons nobody
    can see in the diff. Every ``SQLAGENT_*`` variable is cleared, and the
    settings cache is dropped so the next construction re-reads a clean
    environment.

    Tests that want a real configuration pass it explicitly, which is the point.
    """
    from sqlagent.config import Settings, settings

    for name in list(os.environ):
        if name.startswith("SQLAGENT_") and name != "SQLAGENT_TEST_DATABASE_URL":
            monkeypatch.delenv(name, raising=False)

    # Both halves are needed. Clearing the environment does nothing about the
    # ``.env`` file, which pydantic-settings reads from `model_config` at every
    # construction — so the file is detached too.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    settings.cache_clear()
    yield
    settings.cache_clear()

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "schema.sql"
SEED_SQL = Path(__file__).parent / "fixtures" / "seed.sql"


# --------------------------------------------------------------------------
# Integration fixtures: real Postgres
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("SQLAGENT_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Engine:
    """A live engine with the fixture schema loaded.

    Skips the whole test rather than failing when no database is listening —
    a developer without Docker should still be able to run the unit suite.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no test database reachable at {database_url}: {exc}")

    ddl = FIXTURE_SQL.read_text()
    with engine.begin() as connection:
        # exec_driver_sql runs the script as-is; SQLAlchemy's text() would try
        # to interpret the ':' in type declarations as bind parameters.
        connection.exec_driver_sql(ddl)

    return engine


@pytest.fixture(scope="session")
def live_snapshot(engine: Engine) -> SchemaSnapshot:
    """The fixture schema, reflected from the real database."""
    return reflect_schema(engine)


@pytest.fixture(scope="session")
def seeded_engine(engine: Engine) -> Engine:
    """The test database with deterministic sample data loaded.

    Separate from ``engine`` so schema-only tests are not slowed by, or coupled
    to, the contents of the seed file.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(SEED_SQL.read_text())
    return engine


# --------------------------------------------------------------------------
# Unit fixtures: hand-built snapshot mirroring fixtures/schema.sql
# --------------------------------------------------------------------------


def _column(
    name: str, type_: str = "INTEGER", *, pk: bool = False, nullable: bool = True
) -> Column:
    return Column(name=name, type=type_, nullable=nullable, primary_key=pk)


@pytest.fixture
def snapshot() -> SchemaSnapshot:
    """A hand-built schema mirroring ``fixtures/schema.sql``.

    Kept deliberately in sync with the SQL file so unit tests and integration
    tests assert the same shape. ``test_introspect.py`` has a test that fails
    if the two drift apart.
    """
    tables = {
        "regions": Table(
            name="regions",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("name", "TEXT", nullable=False),
                _column("iso_code", "VARCHAR(2)"),
            ),
            foreign_keys=(),
        ),
        "customers": Table(
            name="customers",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("name", "TEXT", nullable=False),
                _column("email", "TEXT"),
                _column("region_id"),
                _column("created_at", "TIMESTAMP"),
            ),
            foreign_keys=(
                ForeignKey("customers", ("region_id",), "regions", ("id",)),
            ),
        ),
        "addresses": Table(
            name="addresses",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("line1", "TEXT"),
                _column("city", "TEXT"),
                _column("postcode", "TEXT"),
            ),
            foreign_keys=(),
        ),
        "orders": Table(
            name="orders",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("customer_id", nullable=False),
                _column("billing_address_id"),
                _column("shipping_address_id"),
                _column("invoice_id"),
                _column("total", "NUMERIC(12, 2)"),
                _column("placed_at", "DATE"),
            ),
            foreign_keys=(
                ForeignKey("orders", ("billing_address_id",), "addresses", ("id",)),
                ForeignKey("orders", ("shipping_address_id",), "addresses", ("id",)),
                ForeignKey("orders", ("customer_id",), "customers", ("id",)),
                ForeignKey("orders", ("invoice_id",), "invoices", ("id",)),
            ),
        ),
        "invoices": Table(
            name="invoices",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("order_id", nullable=False),
                _column("issued_at", "DATE"),
                _column("amount", "NUMERIC(12, 2)"),
            ),
            foreign_keys=(ForeignKey("invoices", ("order_id",), "orders", ("id",)),),
        ),
        "order_items": Table(
            name="order_items",
            columns=(
                _column("order_id", pk=True, nullable=False),
                _column("line_no", pk=True, nullable=False),
                _column("sku", "TEXT", nullable=False),
                _column("quantity", nullable=False),
            ),
            foreign_keys=(ForeignKey("order_items", ("order_id",), "orders", ("id",)),),
        ),
        "employees": Table(
            name="employees",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("name", "TEXT", nullable=False),
                _column("manager_id"),
            ),
            foreign_keys=(ForeignKey("employees", ("manager_id",), "employees", ("id",)),),
        ),
        "audit_log": Table(
            name="audit_log",
            columns=(
                _column("id", pk=True, nullable=False),
                _column("action", "TEXT"),
                _column("occurred_at", "TIMESTAMP"),
            ),
            foreign_keys=(),
        ),
    }
    return SchemaSnapshot(tables=tables, version=compute_version(tables))


@pytest.fixture
def graph(snapshot: SchemaSnapshot):
    """The schema graph built from the hand-built snapshot."""
    return build_graph(snapshot)
