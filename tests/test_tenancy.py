"""Tests for tenant identity.

The property these protect is not "the right key works". It is that **nothing
works without one** — the failure mode of an authentication system is failing
open, and failing open is silent.
"""

from __future__ import annotations

import pytest

from sqlagent.saas.tenancy import (
    ApiKey,
    Principal,
    Tenant,
    TenantError,
    hash_secret,
    mint_api_key,
    split_api_key,
)


def minted(tenant_id: str = "t_1", **kwargs) -> tuple[str, ApiKey]:
    return mint_api_key(tenant_id, **kwargs)


# --------------------------------------------------------------------------
# The secret is never stored
# --------------------------------------------------------------------------


def test_the_record_does_not_contain_the_secret():
    """A dump of the keys table should name the customers and not let the
    reader become one of them."""
    shown, record = minted()
    _, secret = split_api_key(shown)

    serialised = repr(record)
    assert secret not in serialised
    assert record.hashed_secret != secret


def test_the_stored_form_is_the_hash_of_the_secret():
    shown, record = minted()
    _, secret = split_api_key(shown)

    assert record.hashed_secret == hash_secret(secret)


def test_a_key_is_shown_once_and_the_record_cannot_reproduce_it():
    """The consequence of hashing, asserted so nobody later adds a "reveal"
    endpoint without noticing it cannot work."""
    _, record = minted()

    assert not hasattr(record, "secret")
    assert not hasattr(record, "plaintext")


def test_two_keys_are_never_the_same():
    first, _ = minted()
    second, _ = minted()

    assert first != second


def test_two_keys_for_the_same_tenant_have_different_prefixes():
    """The prefix is the lookup key. Colliding prefixes would make one key
    shadow another."""
    _, a = minted()
    _, b = minted()

    assert a.prefix != b.prefix


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def test_the_right_secret_matches():
    shown, record = minted()
    _, secret = split_api_key(shown)

    assert record.matches(secret)


def test_a_wrong_secret_does_not_match():
    _, record = minted()

    assert not record.matches("not-the-secret")


def test_an_empty_secret_does_not_match():
    """An empty credential is the shape a missing header takes after parsing.
    It must not pass."""
    _, record = minted()

    assert not record.matches("")


def test_another_tenants_secret_does_not_match():
    shown, _ = minted("t_1")
    _, other_record = minted("t_2")
    _, secret = split_api_key(shown)

    assert not other_record.matches(secret)


def test_a_revoked_key_never_matches():
    """Checked on the record, not at the call site. A revocation that depends
    on every caller remembering to test a flag is not a revocation."""
    shown, record = minted()
    _, secret = split_api_key(shown)
    revoked = ApiKey(
        prefix=record.prefix,
        tenant_id=record.tenant_id,
        hashed_secret=record.hashed_secret,
        revoked=True,
    )

    assert record.matches(secret)
    assert not revoked.matches(secret)


def test_the_hash_is_not_reversible_by_comparison():
    """Two different secrets must not collide into one stored hash."""
    assert hash_secret("a") != hash_secret("b")


# --------------------------------------------------------------------------
# Parsing what a caller presented
# --------------------------------------------------------------------------


def test_a_bearer_prefix_is_accepted():
    """Every HTTP client sends `Authorization: Bearer <key>`. Rejecting that
    form would make the documented header wrong."""
    shown, record = minted()

    prefix, secret = split_api_key(f"Bearer {shown}")

    assert prefix == record.prefix
    assert record.matches(secret)


def test_the_bearer_prefix_is_case_insensitive():
    shown, record = minted()

    _, secret = split_api_key(f"bearer {shown}")

    assert record.matches(secret)


def test_surrounding_whitespace_is_tolerated():
    shown, record = minted()

    _, secret = split_api_key(f"  {shown}  ")

    assert record.matches(secret)


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "   ",
        "Bearer ",
        "not-a-key",
        "ak_live_nodot",
        ".secret-with-no-prefix",
        "ak_live_prefix.",
        "xx_live_prefix.secret",
        "Bearer undefined",
        "null",
    ],
)
def test_malformed_credentials_are_rejected_before_any_lookup(presented):
    """Raised on the string, so a flood of garbage costs a split rather than a
    database round trip — which is also what stops it becoming a way to load
    the control plane."""
    with pytest.raises(TenantError):
        split_api_key(presented)


def test_the_error_does_not_say_which_part_was_wrong():
    """"Unknown prefix" and "wrong secret" as distinct errors tell an attacker
    when they have found a real key."""
    messages = set()
    for presented in ("not-a-key", "ak_live_abc", "ak_live_abc."):
        try:
            split_api_key(presented)
        except TenantError as error:
            messages.add(str(error))

    assert len(messages) == 1


def test_a_key_must_belong_to_a_tenant():
    with pytest.raises(TenantError):
        mint_api_key("")


# --------------------------------------------------------------------------
# What the key looks like
# --------------------------------------------------------------------------


def test_a_key_is_recognisable_as_one():
    """Secret scanners match known prefixes. A key that looks like random
    base64 is one nobody's tooling will ever flag as leaked."""
    shown, _ = minted()

    assert shown.startswith("ak_live_")


def test_the_environment_is_visible_in_the_key():
    """A test key pasted into production should be recognisable, not merely
    broken."""
    shown, _ = minted(environment="test")

    assert shown.startswith("ak_test_")


def test_the_secret_carries_enough_entropy_to_resist_guessing():
    """It is the value being protected, against an attacker who can make
    unlimited attempts at a public endpoint."""
    shown, _ = minted()
    _, secret = split_api_key(shown)

    assert len(secret) >= 40


# --------------------------------------------------------------------------
# Principal
#
# There is no anonymous principal and no tenant_id of None. The absence of a
# principal is the absence of a principal — an unauthenticated request raises
# before one exists, so nothing downstream has to remember to check.
# --------------------------------------------------------------------------


def test_a_principal_names_a_tenant():
    principal = Principal(tenant_id="t_1", via="api_key")

    assert principal.tenant_id == "t_1"


def test_a_principal_cannot_be_constructed_without_a_tenant():
    with pytest.raises(TypeError):
        Principal(via="api_key")


def test_a_principal_records_which_front_it_came_through():
    """"This key was used from the API at 04:00" is the sentence that makes an
    incident investigable, and the rate limits differ per front."""
    assert Principal(tenant_id="t_1", via="mcp").via == "mcp"


def test_a_principal_never_carries_the_secret():
    shown, record = minted()
    _, secret = split_api_key(shown)
    principal = Principal(tenant_id="t_1", via="api_key", key_prefix=record.prefix)

    assert secret not in repr(principal)
    assert principal.key_prefix == record.prefix


def test_scopes_are_an_allow_list():
    """A scope added later must not be implicitly granted to keys that already
    exist."""
    principal = Principal(tenant_id="t_1", via="api_key")

    assert not principal.can("admin")
    assert not principal.can("write")


def test_a_granted_scope_is_allowed():
    principal = Principal(tenant_id="t_1", via="session", scopes=frozenset({"admin"}))

    assert principal.can("admin")
    assert not principal.can("billing")


# --------------------------------------------------------------------------
# Tenant
# --------------------------------------------------------------------------


def test_a_tenant_does_not_carry_its_connection_string():
    """Held encrypted and fetched only when an agent is built, so an object
    that gets logged, serialised or attached to an exception cannot leak it."""
    tenant = Tenant(id="t_1", name="Acme", created_at="2026-01-01T00:00:00Z")

    assert not hasattr(tenant, "database_url")
    assert "postgres" not in repr(tenant).lower()


def test_a_tenant_is_active_unless_said_otherwise():
    assert Tenant(id="t_1", name="Acme", created_at="x").active


def test_a_tenant_can_be_suspended():
    """One write, checked at authentication, so it cannot be partially
    applied across five fronts."""
    assert not Tenant(id="t_1", name="Acme", created_at="x", active=False).active
