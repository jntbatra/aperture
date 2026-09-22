"""The control plane: who exists, what they may do, and what they have used.

Separate from the history store on purpose
------------------------------------------
``sqlagent.store`` holds questions, answers and result rows — tenant *data*.
This holds accounts, credentials and entitlements — the things that decide who
may reach that data. Two different blast radiuses, two different backup and
access policies, and in production two different databases.

Keeping them in one file would mean the credential that reads a tenant's
question history is the same credential that can grant a tenant an enterprise
plan.

What is stored hashed, and what is not
--------------------------------------
* **API key secrets** — SHA-256. 256 bits of our own randomness; nothing for a
  work factor to buy. See :mod:`sqlagent.saas.tenancy`.
* **Passwords** — scrypt. Chosen by a person, short, probably reused; the
  entire defence is making each guess expensive. See
  :mod:`sqlagent.saas.passwords`.
* **Session tokens** — SHA-256, same reasoning as API keys.
* **Tenant connection strings** — encrypted, not hashed, because they have to
  be used. See :mod:`sqlagent.saas.secrets`.

A stolen dump of this database therefore names the customers and does not let
the reader become one of them.

Usage is counted per period, not decremented from a balance
-----------------------------------------------------------
``usage`` rows are ``(tenant, period, kind, count)``. Counting up within a
named period rather than decrementing a stored allowance means changing a plan
mid-month is a change to a limit, not a migration of every balance — and the
audit question "what did they use in March" is still answerable in April.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

from sqlagent.saas.passwords import hash_password, needs_rehash, verify_password
from sqlagent.saas.tenancy import ApiKey, Tenant, hash_secret, mint_api_key

logger = logging.getLogger(__name__)

SESSION_DAYS = 30
"""How long a browser session lasts.

Long enough that an analyst is not signed out mid-week, short enough that a
stolen laptop stops working within a month. Refreshed on use, so an active user
is never interrupted and an abandoned session still expires.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'FREE',
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    prefix        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    hashed_secret TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions (expires_at);

-- The tenant's own database. One row per connection, credentials encrypted.
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    label         TEXT NOT NULL,
    encrypted_dsn TEXT NOT NULL,
    redacted_dsn  TEXT NOT NULL,
    dialect       TEXT NOT NULL DEFAULT '',
    table_count   INTEGER NOT NULL DEFAULT 0,
    verified_at   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS connections_tenant ON connections (tenant_id);

CREATE TABLE IF NOT EXISTS usage (
    tenant_id TEXT NOT NULL,
    period    TEXT NOT NULL,
    kind      TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, period, kind)
);
"""


@dataclass(frozen=True, slots=True)
class User:
    id: str
    tenant_id: str
    email: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Connection:
    """A tenant's database, as stored. Never carries the usable DSN."""

    id: str
    tenant_id: str
    label: str
    redacted_dsn: str
    dialect: str
    table_count: int
    verified_at: str


class EmailTaken(ValueError):
    """That address already has an account."""


class ControlStore:
    """Accounts, credentials and entitlements.

    SQLite locally, PostgreSQL in production — the same SQLAlchemy Core code
    either way, because nothing here needs a dialect-specific feature and a
    local developer should not have to run two databases to work on sign-in.
    """

    def __init__(self, path_or_url: Path | str) -> None:
        url = str(path_or_url)
        if "://" not in url:
            url = f"sqlite:///{url}"
        self._engine: Engine = create_engine(url, future=True, pool_pre_ping=True)
        self._create_schema()

    def _create_schema(self) -> None:
        with self._engine.begin() as connection:
            for statement in filter(None, (s.strip() for s in SCHEMA.split(";"))):
                connection.execute(text(statement))

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def current_period() -> str:
        """The billing period a question counts against: ``YYYY-MM``.

        Calendar months, in UTC. Not rolling 30-day windows from the signup
        date: a customer needs to be able to predict when their allowance
        resets without doing arithmetic, and support needs "March" to mean the
        same thing for everyone.
        """
        return datetime.now(UTC).strftime("%Y-%m")

    # ------------------------------------------------------------------
    # Tenants and users
    # ------------------------------------------------------------------

    def create_account(self, *, email: str, password: str, name: str = "") -> tuple[Tenant, User]:
        """Create a tenant and its first user, atomically.

        One transaction. A tenant with no user is unreachable and a user with
        no tenant cannot be authorised, so a partial failure that leaves either
        is worse than no account at all.
        """
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("That does not look like an email address.")

        # Hashed outside the transaction: scrypt takes ~60ms and holding a
        # write lock for it would serialise signups behind each other.
        password_hash = hash_password(password)

        tenant_id = f"t_{uuid.uuid4().hex[:16]}"
        user_id = f"u_{uuid.uuid4().hex[:16]}"
        now = self._now()

        with self._engine.begin() as connection:
            existing = connection.execute(
                text("SELECT 1 FROM users WHERE email = :email"), {"email": email}
            ).fetchone()
            if existing:
                raise EmailTaken("That email already has an account.")

            connection.execute(
                text(
                    "INSERT INTO tenants (id, name, plan, created_at, active) "
                    "VALUES (:id, :name, 'FREE', :now, 1)"
                ),
                {"id": tenant_id, "name": name or email.split("@")[0], "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, created_at) "
                    "VALUES (:id, :tenant, :email, :hash, :now)"
                ),
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "email": email,
                    "hash": password_hash,
                    "now": now,
                },
            )

        return (
            Tenant(id=tenant_id, name=name or email.split("@")[0], created_at=now),
            User(id=user_id, tenant_id=tenant_id, email=email, created_at=now),
        )

    def verify_user(self, email: str, password: str) -> User | None:
        """Check a sign-in. None for every failure.

        One return value for "no such user" and "wrong password", because two
        would let anyone enumerate which email addresses have accounts.

        The work is done either way: a missing user still runs a scrypt hash
        against a dummy value, so the response time does not reveal whether the
        address exists. Without that, the timing says what the message refuses
        to.
        """
        email = (email or "").strip().lower()
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, tenant_id, email, password_hash, created_at "
                    "FROM users WHERE email = :email"
                ),
                {"email": email},
            ).fetchone()

        if row is None:
            verify_password(password, _DUMMY_HASH)
            return None

        if not verify_password(password, row[3]):
            return None

        if needs_rehash(row[3]):
            # Sign-in is the only moment the plaintext exists to rehash with.
            # Without this, raising the cost parameters protects nobody who
            # already has an account — which is everybody.
            self._update_password_hash(row[0], hash_password(password))

        return User(id=row[0], tenant_id=row[1], email=row[2], created_at=row[4])

    def _update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET password_hash = :hash WHERE id = :id"),
                {"hash": password_hash, "id": user_id},
            )

    def tenant(self, tenant_id: str) -> Tenant | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT id, name, created_at, active FROM tenants WHERE id = :id"),
                {"id": tenant_id},
            ).fetchone()
        if row is None:
            return None
        return Tenant(id=row[0], name=row[1], created_at=row[2], active=bool(row[3]))

    def plan_name(self, tenant_id: str) -> str:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT plan FROM tenants WHERE id = :id"), {"id": tenant_id}
            ).fetchone()
        return row[0] if row else "FREE"

    def set_plan(self, tenant_id: str, plan: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("UPDATE tenants SET plan = :plan WHERE id = :id"),
                {"plan": plan.upper(), "id": tenant_id},
            )

    def user_by_id(self, user_id: str) -> User | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT id, tenant_id, email, created_at FROM users WHERE id = :id"),
                {"id": user_id},
            ).fetchone()
        return User(id=row[0], tenant_id=row[1], email=row[2], created_at=row[3]) if row else None

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(self, user: User) -> str:
        """Issue a session token. Returns the value for the cookie.

        Only the hash is stored, for the same reason as API keys: a leaked
        dump of this table must not be a set of usable sessions.
        """
        import secrets as _secrets

        token = _secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions (token_hash, tenant_id, user_id, created_at, expires_at) "
                    "VALUES (:hash, :tenant, :user, :now, :expires)"
                ),
                {
                    "hash": hash_secret(token),
                    "tenant": user.tenant_id,
                    "user": user.id,
                    "now": now.isoformat(),
                    "expires": (now + timedelta(days=SESSION_DAYS)).isoformat(),
                },
            )
        return token

    def session_tenant(self, session_token: str) -> str | None:
        """The tenant a session belongs to, or None if it is not valid.

        Expiry is checked here rather than by a cleanup job. A job that has not
        run yet would leave expired sessions working, which is the one failure
        mode this must not have.
        """
        if not session_token:
            return None
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT tenant_id, expires_at FROM sessions WHERE token_hash = :hash"
                ),
                {"hash": hash_secret(session_token)},
            ).fetchone()

        if row is None:
            return None
        if datetime.fromisoformat(row[1]) <= datetime.now(UTC):
            return None
        return row[0]

    def session_user(self, session_token: str) -> User | None:
        if not session_token:
            return None
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT user_id, expires_at FROM sessions WHERE token_hash = :hash"
                ),
                {"hash": hash_secret(session_token)},
            ).fetchone()
        if row is None or datetime.fromisoformat(row[1]) <= datetime.now(UTC):
            return None
        return self.user_by_id(row[0])

    def end_session(self, session_token: str) -> None:
        """Sign out. Deletes server-side, so the token is dead immediately.

        Not merely clearing the cookie: a cookie is a copy the client holds,
        and anyone who captured it would still be signed in.
        """
        if not session_token:
            return
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE token_hash = :hash"),
                {"hash": hash_secret(session_token)},
            )

    def purge_expired_sessions(self) -> int:
        """Housekeeping. Correctness does not depend on it having run."""
        with self._engine.begin() as connection:
            result = connection.execute(
                text("DELETE FROM sessions WHERE expires_at <= :now"),
                {"now": datetime.now(UTC).isoformat()},
            )
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def issue_api_key(self, tenant_id: str, name: str = "") -> str:
        """Create a key. Returns the secret, which is shown once and never again."""
        shown, record = mint_api_key(tenant_id, name=name)
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO api_keys (prefix, tenant_id, hashed_secret, name, created_at) "
                    "VALUES (:prefix, :tenant, :hash, :name, :now)"
                ),
                {
                    "prefix": record.prefix,
                    "tenant": tenant_id,
                    "hash": record.hashed_secret,
                    "name": name,
                    "now": record.created_at,
                },
            )
        return shown

    def api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT prefix, tenant_id, hashed_secret, name, created_at, "
                    "last_used_at, revoked FROM api_keys WHERE prefix = :prefix"
                ),
                {"prefix": prefix},
            ).fetchone()
        if row is None:
            return None
        return ApiKey(
            prefix=row[0],
            tenant_id=row[1],
            hashed_secret=row[2],
            name=row[3],
            created_at=row[4],
            last_used_at=row[5],
            revoked=bool(row[6]),
        )

    def touch_api_key(self, prefix: str) -> None:
        """Record that a key was used, so "revoke what nothing has touched in a
        year" is an answerable question."""
        with self._engine.begin() as connection:
            connection.execute(
                text("UPDATE api_keys SET last_used_at = :now WHERE prefix = :prefix"),
                {"now": self._now(), "prefix": prefix},
            )

    def list_api_keys(self, tenant_id: str) -> list[ApiKey]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT prefix, tenant_id, hashed_secret, name, created_at, "
                    "last_used_at, revoked FROM api_keys WHERE tenant_id = :tenant "
                    "ORDER BY created_at DESC"
                ),
                {"tenant": tenant_id},
            ).fetchall()
        return [
            ApiKey(
                prefix=row[0],
                tenant_id=row[1],
                hashed_secret=row[2],
                name=row[3],
                created_at=row[4],
                last_used_at=row[5],
                revoked=bool(row[6]),
            )
            for row in rows
        ]

    def revoke_api_key(self, tenant_id: str, prefix: str) -> bool:
        """Revoke a key. Scoped by tenant so one tenant cannot revoke another's.

        The tenant is in the WHERE clause rather than checked beforehand: a
        check and a write in two statements is a check that can be skipped by
        whoever writes the third call site.
        """
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE api_keys SET revoked = 1 "
                    "WHERE prefix = :prefix AND tenant_id = :tenant"
                ),
                {"prefix": prefix, "tenant": tenant_id},
            )
        return bool(result.rowcount)

    # ------------------------------------------------------------------
    # Connected databases
    # ------------------------------------------------------------------

    def save_connection(
        self,
        tenant_id: str,
        *,
        label: str,
        encrypted_dsn: str,
        redacted_dsn: str,
        dialect: str,
        table_count: int,
    ) -> Connection:
        connection_id = f"c_{uuid.uuid4().hex[:16]}"
        now = self._now()
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, label, encrypted_dsn, "
                    "redacted_dsn, dialect, table_count, verified_at, created_at) "
                    "VALUES (:id, :tenant, :label, :enc, :red, :dialect, :count, :now, :now)"
                ),
                {
                    "id": connection_id,
                    "tenant": tenant_id,
                    "label": label,
                    "enc": encrypted_dsn,
                    "red": redacted_dsn,
                    "dialect": dialect,
                    "count": table_count,
                    "now": now,
                },
            )
        return Connection(
            id=connection_id,
            tenant_id=tenant_id,
            label=label,
            redacted_dsn=redacted_dsn,
            dialect=dialect,
            table_count=table_count,
            verified_at=now,
        )

    def list_connections(self, tenant_id: str) -> list[Connection]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, tenant_id, label, redacted_dsn, dialect, table_count, "
                    "verified_at FROM connections WHERE tenant_id = :tenant "
                    "ORDER BY created_at DESC"
                ),
                {"tenant": tenant_id},
            ).fetchall()
        return [
            Connection(
                id=row[0], tenant_id=row[1], label=row[2], redacted_dsn=row[3],
                dialect=row[4], table_count=row[5], verified_at=row[6],
            )
            for row in rows
        ]

    def encrypted_dsn(self, tenant_id: str, connection_id: str) -> str | None:
        """The stored ciphertext for one connection, scoped by tenant.

        The tenant is in the WHERE clause, not checked by the caller. This is
        the single most important scoping in the system: getting it wrong hands
        one customer's database credential to another.
        """
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT encrypted_dsn FROM connections "
                    "WHERE id = :id AND tenant_id = :tenant"
                ),
                {"id": connection_id, "tenant": tenant_id},
            ).fetchone()
        return row[0] if row else None

    def delete_connection(self, tenant_id: str, connection_id: str) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                text("DELETE FROM connections WHERE id = :id AND tenant_id = :tenant"),
                {"id": connection_id, "tenant": tenant_id},
            )
        return bool(result.rowcount)

    def count_connections(self, tenant_id: str) -> int:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT count(*) FROM connections WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            ).fetchone()
        return row[0] or 0

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    def record_usage(self, tenant_id: str, kind: str = "questions", amount: int = 1) -> None:
        """Count one unit against this period.

        Upsert rather than read-modify-write: two questions answered at the
        same instant on two workers would otherwise both read the old value and
        write the same new one, and the tenant would get one for free. At scale
        that is not a rounding error, it is the business model.
        """
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO usage (tenant_id, period, kind, count) "
                    "VALUES (:tenant, :period, :kind, :amount) "
                    "ON CONFLICT (tenant_id, period, kind) "
                    "DO UPDATE SET count = usage.count + :amount"
                ),
                {
                    "tenant": tenant_id,
                    "period": self.current_period(),
                    "kind": kind,
                    "amount": amount,
                },
            )

    def usage(self, tenant_id: str) -> dict[str, int]:
        """This period's counts, by kind."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT kind, count FROM usage "
                    "WHERE tenant_id = :tenant AND period = :period"
                ),
                {"tenant": tenant_id, "period": self.current_period()},
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def close(self) -> None:
        self._engine.dispose()


_DUMMY_HASH = hash_password("a password that belongs to nobody at all")
"""Hashed once at import, to spend the same time on a missing user as on a real
one. Computing it per call would double the cost of every failed sign-in, which
is the request an attacker controls the rate of."""
