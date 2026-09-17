"""Optional Google sign-in.

Authentication is a profile, not a gate: with no client id configured, or no
credentials on the request, the local user is returned and every feature works.
That keeps a fresh clone demoable without a Google Cloud project, while the
storage layer stays keyed by user id so hosting later is a configuration
change rather than a rewrite.

The browser performs the Google Identity Services flow and sends the resulting
ID token here; it is verified against Google's public keys. No client secret is
involved, so there is no secret to leak.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

import jwt

from .config import settings
from .store import User, ensure_local_user, get_user, upsert_user

log = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 24 * 3600


def auth_enabled() -> bool:
    return bool(settings().google_client_id)


def session_secret() -> str:
    """A signing key for session tokens, generated once and kept locally."""
    configured = settings().session_secret
    if configured:
        return configured
    path = Path(os.path.expanduser(settings().home_dir)) / "session.key"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


def issue_session(user: User) -> str:
    payload = {
        "sub": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, session_secret(), algorithm="HS256")


def read_session(token: str) -> User | None:
    try:
        payload = jwt.decode(token, session_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as err:
        log.debug("session rejected: %s", err)
        return None
    return get_user(payload["sub"])


def verify_google_token(id_token: str) -> User:
    """Verify a Google ID token and return the user it identifies."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(
        id_token, google_requests.Request(), settings().google_client_id
    )
    if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise ValueError("unexpected token issuer")

    user = User(
        id=f"google:{claims['sub']}",
        email=claims.get("email", ""),
        name=claims.get("name", ""),
        picture=claims.get("picture", ""),
        provider="google",
    )
    return upsert_user(user)


def user_from_header(authorization: str | None) -> User:
    """Resolve the user for one request.

    Never raises: an absent, malformed or expired token falls back to the local
    profile rather than locking someone out of a tool running on their own
    machine.
    """
    if authorization and authorization.lower().startswith("bearer "):
        user = read_session(authorization.split(" ", 1)[1].strip())
        if user:
            return user
    return ensure_local_user()
