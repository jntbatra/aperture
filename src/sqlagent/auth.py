"""Who may use the app: Neon Auth proves identity, the administrator grants access.

Neon Auth (managed Better Auth) handles sign-up, passwords and sign-in. Its
sign-up is open, so a valid Neon identity is *not* permission to use this app.
This module adds the missing gate.

The flow
--------
1. The browser signs in with Neon Auth and receives a short-lived JWT.
2. It posts that JWT to ``/api/auth/session``. The server verifies the
   signature against Neon's JWKS, issuer and audience.
3. The first time a user is seen they are recorded as ``pending`` and the
   administrator is emailed a one-time link. Pending and rejected users get no
   session. Approved users get an HttpOnly cookie.
4. Every other ``/api`` route needs that cookie.

The approval link only shows a page; the Approve and Reject buttons POST.
Mail scanners and link previews fetch URLs, and a GET that approved an account
would let them approve everyone.

Approvals and sessions live in Neon Postgres. SQLAlchemy Core keeps the code
runnable on SQLite in tests.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import smtplib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    ForeignKey,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    update,
)

from sqlagent.config import Settings

logger = logging.getLogger(__name__)

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"

_metadata = MetaData()
_approvals = Table(
    "approvals",
    _metadata,
    # The Neon Auth user id (the JWT `sub`).
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    Column("approval_token_hash", String(64)),
)
_sessions = Table(
    "sessions",
    _metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(64), ForeignKey("approvals.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


class AuthError(Exception):
    """A refusal the caller may show the user. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    id: str
    email: str
    status: str


@dataclass(frozen=True)
class Identity:
    """What a verified Neon Auth token says about someone."""

    id: str
    email: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# Verifying the Neon Auth token
# --------------------------------------------------------------------------


class TokenVerifier:
    """Checks a Neon Auth JWT: signature, expiry, issuer and audience.

    Only EdDSA is accepted. Letting the token name its own algorithm is the
    classic way to be tricked into verifying an HS256 token with the public key
    as the secret, so the algorithm is fixed here rather than read from it.
    """

    def __init__(self, config: Settings) -> None:
        self.jwks_url = config.neon_auth_jwks_url
        base = config.neon_auth_base_url
        parts = urlsplit(base)
        self.issuer = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
        self._jwks = jwt.PyJWKClient(self.jwks_url, cache_keys=True) if self.jwks_url else None

    def _key(self, token: str):
        assert self._jwks is not None
        return self._jwks.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> Identity:
        if self._jwks is None or not self.issuer:
            raise AuthError("Sign-in is not configured on this server.", 503)
        try:
            claims = jwt.decode(
                token,
                self._key(token),
                algorithms=["EdDSA"],
                issuer=self.issuer,
                audience=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError("Sign in again.", 401) from exc
        except Exception as exc:  # noqa: BLE001  (JWKS fetch failed)
            logger.exception("could not fetch signing keys")
            raise AuthError("Sign-in is temporarily unavailable.", 503) from exc
        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise AuthError("Your account has no email address.", 400)
        return Identity(id=str(claims["sub"]), email=email)


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


def send_mail(config: Settings, to: str, subject: str, body: str) -> None:
    """Send one message, or log it when no SMTP server is configured.

    Never raises: a mail outage must not turn a sign-in into a 500. The body is
    logged on failure so the administrator can still act on an approval link.
    """
    if not config.smtp_host or not to:
        logger.warning("mail not sent (no SMTP configured) to=%s subject=%s\n%s", to, subject, body)
        return
    message = EmailMessage()
    message["From"] = config.smtp_from or config.smtp_user
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(message)
    except Exception:  # noqa: BLE001
        logger.exception("could not send mail to %s; body follows\n%s", to, body)


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class Accounts:
    def __init__(
        self,
        config: Settings,
        engine: Engine | None = None,
        verifier: TokenVerifier | None = None,
    ) -> None:
        self.config = config
        self.engine = engine or _make_engine(config)
        self.verifier = verifier or TokenVerifier(config)
        _metadata.create_all(self.engine)
        self._lock = threading.Lock()

    def start_session(self, neon_token: str) -> tuple[User, str]:
        """Exchange a Neon Auth token for an app session, if the user is approved.

        A first-time user is recorded as pending and the administrator is
        asked; they are refused until a decision is made.
        """
        identity = self.verifier.verify(neon_token)
        user, asked = self._record(identity)
        if asked is not None:
            self._ask_admin(user, asked)
        if user.status == PENDING:
            raise AuthError("Your access request is waiting for approval.", 403)
        if user.status != APPROVED:
            raise AuthError("Your access request was not approved.", 403)
        return user, self._new_session(user)

    def _record(self, identity: Identity) -> tuple[User, str | None]:
        """Find or create the user. Returns a fresh approval token only when new."""
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(select(_approvals).where(_approvals.c.id == identity.id)).first()
            if row is not None:
                return User(row.id, row.email, row.status), None
            token = secrets.token_urlsafe(32)
            conn.execute(
                insert(_approvals).values(
                    id=identity.id,
                    email=identity.email,
                    status=PENDING,
                    created_at=datetime.now(UTC),
                    approval_token_hash=_digest(token),
                )
            )
        return User(identity.id, identity.email, PENDING), token

    def _ask_admin(self, user: User, token: str) -> None:
        link = f"{self.config.public_url.rstrip('/')}/api/auth/approve?token={token}"
        send_mail(
            self.config,
            self.config.admin_email,
            f"Aperture access request: {user.email}",
            f"{user.email} asked for access.\n\nReview it here (nothing changes until you "
            f"press a button):\n{link}\n",
        )

    def _new_session(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(delete(_sessions).where(_sessions.c.expires_at < now))
            conn.execute(
                insert(_sessions).values(
                    token_hash=_digest(token),
                    user_id=user.id,
                    expires_at=now + timedelta(days=self.config.session_days),
                )
            )
        return token

    # -- the administrator's decision --------------------------------------

    def pending_for_token(self, token: str) -> User | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(_approvals).where(_approvals.c.approval_token_hash == _digest(token))
            ).first()
        return User(row.id, row.email, row.status) if row else None

    def decide(self, token: str, approve: bool) -> User:
        status = APPROVED if approve else REJECTED
        with self.engine.begin() as conn:
            row = conn.execute(
                select(_approvals).where(_approvals.c.approval_token_hash == _digest(token))
            ).first()
            if row is None:
                raise AuthError("This link has already been used or is not valid.", 404)
            conn.execute(
                update(_approvals)
                .where(_approvals.c.id == row.id)
                .values(status=status, decided_at=datetime.now(UTC), approval_token_hash=None)
            )
        user = User(row.id, row.email, status)
        if approve:
            send_mail(
                self.config,
                user.email,
                "Your Aperture access is approved",
                f"You can now sign in at {self.config.public_url}.\n",
            )
        return user

    # -- sessions ----------------------------------------------------------

    def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                select(_approvals, _sessions.c.expires_at)
                .join(_sessions, _sessions.c.user_id == _approvals.c.id)
                .where(_sessions.c.token_hash == _digest(token))
            ).first()
        if row is None or row.status != APPROVED:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:  # SQLite drops the zone
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            return None
        return User(row.id, row.email, row.status)

    def sign_out(self, token: str | None) -> None:
        if token:
            with self.engine.begin() as conn:
                conn.execute(delete(_sessions).where(_sessions.c.token_hash == _digest(token)))


def _make_engine(config: Settings) -> Engine:
    url = config.control_database_url
    if not url:
        path = Path(config.data_dir) / "auth.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    elif url.startswith("postgresql://"):
        # Neon hands out a plain postgresql:// URL; name the driver we ship.
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    # Neon scales to zero and drops idle connections; pre-ping replaces a dead
    # pooled connection instead of failing the request that finds it.
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)
