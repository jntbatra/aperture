"""Tenant identity, and the credentials that prove it.

The rule everything here exists to enforce
------------------------------------------
**A request that does not resolve to a tenant is rejected.** It does not fall
back to the server's configured database.

That fallback is the single most dangerous line that could be written in this
package, and it is the one a reasonable person writes by accident. The
single-tenant code already has its shape::

    def resolve_agent(dataset_id):
        return get_dataset_agent(dataset_id) if dataset_id else get_agent()

Harmless when there is one customer and one database. Translated naively to
tenants, it hands the server's own connection to an unauthenticated caller —
and it fails *open*, so nothing goes wrong until the wrong person notices.
There is a test asserting the absence of that fallback on every front.

Why API keys are hashed
-----------------------
The control plane stores ``sha256(secret)``, never the secret. A dump of the
tenants table is then an inconvenience rather than a breach: it names who the
customers are, and it does not let the reader become one of them.

The consequence is that a key is displayed exactly once, at creation, and
cannot be recovered — only rotated. That is the correct trade and it has to be
said out loud in the interface, because a user who assumes they can look it up
later will store it somewhere worse than a password manager.

Why the prefix is stored in clear
---------------------------------
``ak_live_7f3a…`` — the first segment is an unauthenticated lookup key. Without
it, verifying a presented key means hashing it against every key in the table,
which is a linear scan on the hot path of every request. With it, the lookup is
indexed and the hash comparison happens once.

The prefix is not a secret and is safe to show: it identifies which key, not
what it is. Showing it is what makes "revoke the key on that old laptop"
possible at all.

Why comparison is constant-time
-------------------------------
``hmac.compare_digest``. A plain ``==`` on a hash leaks how many leading bytes
matched through timing, which over enough requests is enough to forge one. The
cost is nothing and the alternative is a subtle, real vulnerability.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

KEY_PREFIX = "ak"
"""Marks a string as one of this system's API keys.

Present so a key is recognisable in a log, a paste, or a secret scanner. GitHub
and similar tools match on known prefixes, and a key that looks like random
base64 is one nobody's tooling will ever flag as leaked.
"""

PREFIX_BYTES = 6
SECRET_BYTES = 32
"""32 bytes from ``secrets.token_urlsafe`` is 256 bits of entropy.

Not a round number chosen for looks: this is the value being protected, and it
must not be guessable by an attacker who can make unlimited attempts against a
public endpoint.
"""


class TenantError(RuntimeError):
    """Something about identity is wrong. Never carries the secret."""


@dataclass(frozen=True, slots=True)
class Tenant:
    """One customer.

    ``database_url`` is deliberately absent. A tenant's connection string is
    held encrypted and fetched only at the moment an agent is built for them
    (see :mod:`sqlagent.saas.secrets`), so an object that gets logged, put in a
    response model or included in an exception cannot carry it.
    """

    id: str
    name: str
    created_at: str
    active: bool = True
    """False disables every front at once.

    Checked at authentication rather than at each endpoint, so suspending a
    tenant is one write and cannot be partially applied.
    """


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A credential belonging to one tenant.

    Holds the *hash*, never the secret. The secret exists only in the return
    value of :func:`mint_api_key` and in whatever the user does with it after.
    """

    prefix: str
    tenant_id: str
    hashed_secret: str
    name: str = ""
    created_at: str = ""
    last_used_at: str | None = None
    revoked: bool = False

    def matches(self, secret: str) -> bool:
        """Whether ``secret`` is this key. Constant-time.

        A revoked key never matches, checked here rather than at the call site:
        a revocation that depends on every caller remembering to test a flag is
        not a revocation.
        """
        if self.revoked:
            return False
        return hmac.compare_digest(self.hashed_secret, hash_secret(secret))


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, resolved from whichever front it arrived on.

    The single currency of authorisation. A session cookie from the web app, an
    API key from a script, an MCP token from another assistant and the CLI all
    reduce to one of these, and everything downstream is scoped by
    ``tenant_id`` without caring which door was used.

    There is no "anonymous" Principal and no ``tenant_id = None``. The absence
    of a principal is represented by the absence of a principal — an
    unauthenticated request raises before one exists, so no code downstream has
    to remember to check.
    """

    tenant_id: str
    via: str
    """Which front: ``session``, ``api_key``, ``mcp`` or ``cli``.

    Recorded because "this key was used from the API at 04:00" is the sentence
    that makes an incident investigable, and because rate limits differ per
    front — a browser and a batch script are not the same traffic.
    """

    key_prefix: str | None = None
    """Which credential, when it was a key. Never the key itself."""

    scopes: frozenset[str] = field(default_factory=frozenset)
    """What this credential may do. Empty means read-only question asking.

    An allow-list, so a scope added later is not implicitly granted to every
    key that already exists.
    """

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def hash_secret(secret: str) -> str:
    """The stored form of an API key secret.

    Plain SHA-256, deliberately, and this is the one place where *not* using a
    slow password hash is right. A 256-bit random secret has no smaller search
    space to protect — there is no dictionary of likely values, no reuse across
    sites, and nothing for bcrypt's work factor to buy. What it would cost is
    real: a KDF on the hot path of every API request, on every front.

    The reasoning does not transfer to user passwords, which are low-entropy
    and must use a real KDF.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_api_key(
    tenant_id: str, *, name: str = "", environment: str = "live"
) -> tuple[str, ApiKey]:
    """Create a key. Returns ``(the secret to show once, the record to store)``.

    The tuple shape is the point. A function returning only the record would
    make it impossible to show the user their key; a function returning only
    the secret would make it impossible to store anything. Returning both,
    separately, forces the caller to be explicit about which one it is holding.

    Args:
        environment: ``live`` or ``test``, visible in the key itself. Someone
            reading a config file can tell at a glance which one they are
            looking at, and a test key pasted into production is recognisable
            rather than merely broken.
    """
    if not tenant_id:
        raise TenantError("an API key must belong to a tenant")

    prefix = f"{KEY_PREFIX}_{environment}_{secrets.token_hex(PREFIX_BYTES)}"
    secret = secrets.token_urlsafe(SECRET_BYTES)

    record = ApiKey(
        prefix=prefix,
        tenant_id=tenant_id,
        hashed_secret=hash_secret(secret),
        name=name,
        created_at=datetime.now(UTC).isoformat(),
    )
    return f"{prefix}.{secret}", record


def split_api_key(presented: str) -> tuple[str, str]:
    """Split ``prefix.secret`` into its parts.

    Raises:
        TenantError: The string is not shaped like a key at all. Raised before
            any lookup, so a malformed header costs a string split rather than
            a database round trip — which is also what stops a flood of garbage
            from becoming a way to load the control plane.

    The error deliberately says nothing about *which* part was wrong.
    """
    candidate = (presented or "").strip()
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()

    prefix, separator, secret = candidate.partition(".")
    if not separator or not prefix.startswith(f"{KEY_PREFIX}_") or not secret:
        raise TenantError("not a valid API key")
    return prefix, secret
