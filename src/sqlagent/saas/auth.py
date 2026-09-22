"""Resolving a request to a tenant, or refusing it.

One door, five entrances
------------------------
The web app sends a session cookie, a script sends an API key, another
assistant sends an MCP token, the CLI and the SDK send an API key. All five
arrive here and leave as a :class:`Principal` or as an exception. Nothing
downstream asks which front a request came from in order to decide whether it
is allowed — that decision happens once, here.

Fail closed, and what that actually means
-----------------------------------------
There is no anonymous path. ``authenticate()`` returns a Principal or raises;
it never returns ``None`` and it never falls back to a default tenant.

That is worth being explicit about, because the single-tenant code contains the
shape of the mistake already::

    def resolve_agent(dataset_id):
        return get_dataset_agent(dataset_id) if dataset_id else get_agent()

Harmless with one customer. The tenant translation of it — "no tenant? use the
configured database" — hands the server's own connection to an unauthenticated
caller, and it fails *open*, so every test passes and nothing looks wrong until
the wrong person notices. ``test_saas_auth.py`` asserts the absence of that
fallback on every front.

Why lookup is by prefix and comparison is constant-time
-------------------------------------------------------
Presented keys are ``prefix.secret``. The prefix is an indexed, unauthenticated
lookup; the secret is compared against the stored hash with
``hmac.compare_digest``. Without the prefix, verifying one key means hashing it
against every row on the hot path of every request on every front.

Why every failure looks the same
--------------------------------
Unknown prefix, wrong secret, revoked key, suspended tenant: one error, one
message, one status. Distinguishing them tells an attacker when they have found
a real key, and tells a scraper which tenants exist. The *logs* distinguish
them, which is where that information is useful and safe.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlagent.saas.tenancy import ApiKey, Principal, Tenant, TenantError, split_api_key

logger = logging.getLogger(__name__)

UNAUTHENTICATED = "Not authenticated. Send an API key as 'Authorization: Bearer <key>'."
"""The only message any authentication failure produces.

Deliberately identical for every cause. It names the header rather than the
problem, because the one thing a legitimate caller needs is the format, and the
one thing an attacker must not get is a signal about which part they got right.
"""


class AuthError(RuntimeError):
    """Authentication failed. Always renders as 401, never says why."""

    def __init__(self, message: str = UNAUTHENTICATED) -> None:
        super().__init__(message)


class SuspendedError(RuntimeError):
    """The tenant exists and is disabled.

    Distinct from :class:`AuthError` on purpose, and it is the one distinction
    worth making: the caller authenticated successfully, so telling them the
    account is suspended leaks nothing they do not already know, and leaving
    them to debug a generic 401 against a working key is cruel.
    """


class ControlPlane(Protocol):
    """What authentication needs from storage, and nothing more.

    A Protocol rather than a concrete class so the tests exercise the real
    resolution logic against an in-memory double. The alternative — testing
    auth through a database — makes the slowest tests in the suite the ones
    guarding the most important property, which is how they end up skipped.
    """

    def api_key_by_prefix(self, prefix: str) -> ApiKey | None: ...
    def tenant(self, tenant_id: str) -> Tenant | None: ...
    def session_tenant(self, session_token: str) -> str | None: ...
    def touch_api_key(self, prefix: str) -> None: ...


def authenticate_api_key(
    control: ControlPlane, presented: str | None, *, via: str = "api_key"
) -> Principal:
    """Resolve an API key. Raises rather than returning None.

    Args:
        presented: The raw ``Authorization`` header, with or without ``Bearer``.
        via: Which front — ``api_key``, ``cli``, ``sdk`` or ``mcp``. Recorded
            on the Principal for rate limiting and for the audit trail, and it
            does not affect whether the key is accepted.
    """
    if not presented:
        raise AuthError

    try:
        prefix, secret = split_api_key(presented)
    except TenantError:
        # Malformed. Rejected before any lookup, so garbage costs a string
        # split rather than a database round trip.
        raise AuthError from None

    record = control.api_key_by_prefix(prefix)
    if record is None or not record.matches(secret):
        # One branch for "no such key" and "wrong secret". Two branches would
        # be two different timings and two different log lines, and the second
        # is the one that tells an attacker they found a real prefix.
        logger.info("rejected api key prefix=%s known=%s", prefix, record is not None)
        raise AuthError

    tenant = control.tenant(record.tenant_id)
    if tenant is None:
        # A key whose tenant was deleted. Not an attack — a cleanup that missed
        # a row — but it must not authenticate.
        logger.warning("api key %s names a tenant that does not exist", prefix)
        raise AuthError

    if not tenant.active:
        raise SuspendedError(
            "This workspace is suspended. Contact support to reactivate it."
        )

    control.touch_api_key(prefix)
    return Principal(
        tenant_id=tenant.id,
        via=via,
        key_prefix=prefix,
        scopes=record_scopes(record),
    )


def authenticate_session(control: ControlPlane, session_token: str | None) -> Principal:
    """Resolve a browser session cookie.

    Separate from the key path because the credential is different in kind: a
    session is issued by us, is short-lived, and is revoked by deleting it
    server-side. Sharing a code path with API keys would mean one of the two
    inherits the other's lifetime rules.
    """
    if not session_token:
        raise AuthError

    tenant_id = control.session_tenant(session_token)
    if tenant_id is None:
        raise AuthError

    tenant = control.tenant(tenant_id)
    if tenant is None:
        raise AuthError
    if not tenant.active:
        raise SuspendedError(
            "This workspace is suspended. Contact support to reactivate it."
        )

    return Principal(tenant_id=tenant.id, via="session")


def record_scopes(record: ApiKey) -> frozenset[str]:
    """What a key may do.

    Currently uniform: every key may ask questions and read its own history.
    Written as a function rather than a constant so that adding per-key scopes
    later is a change here, not a change at every construction site — and so
    the allow-list stays an allow-list.
    """
    return frozenset({"ask", "read"})


def require_scope(principal: Principal, scope: str) -> None:
    """Raise unless the principal holds ``scope``.

    Raises:
        PermissionError: Rendered as 403 — authenticated, not permitted, which
            is genuinely different from 401 and is the one case where saying so
            helps the caller and costs nothing.
    """
    if not principal.can(scope):
        raise PermissionError(f"This credential is not permitted to {scope}.")
