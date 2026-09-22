"""Tests for the authentication boundary.

The property under test is not "a valid key works". It is that **nothing works
without one**, on every front, with no path that falls back to the server's own
database. An authentication system fails by failing open, and failing open is
silent — every test passes, nothing looks wrong, and the defect is only visible
from outside.
"""

from __future__ import annotations

import inspect

import pytest

from sqlagent.saas import auth as auth_module
from sqlagent.saas.auth import (
    UNAUTHENTICATED,
    AuthError,
    SuspendedError,
    authenticate_api_key,
    authenticate_session,
    require_scope,
)
from sqlagent.saas.tenancy import Principal, Tenant, mint_api_key, split_api_key

FRONTS = ["api_key", "cli", "sdk", "mcp"]


class FakeControl:
    """An in-memory control plane.

    Deliberately not a database. Testing the most important property in the
    system through SQLite would make these the slowest tests in the suite,
    which is how they end up marked slow and then skipped.
    """

    def __init__(self):
        self.keys: dict[str, object] = {}
        self.tenants: dict[str, Tenant] = {}
        self.sessions: dict[str, str] = {}
        self.touched: list[str] = []

    def add_tenant(self, tenant_id="t_1", active=True) -> Tenant:
        tenant = Tenant(id=tenant_id, name="Acme", created_at="2026-01-01", active=active)
        self.tenants[tenant_id] = tenant
        return tenant

    def add_key(self, tenant_id="t_1", **kwargs) -> str:
        shown, record = mint_api_key(tenant_id, **kwargs)
        self.keys[record.prefix] = record
        return shown

    def api_key_by_prefix(self, prefix):
        return self.keys.get(prefix)

    def tenant(self, tenant_id):
        return self.tenants.get(tenant_id)

    def session_tenant(self, session_token):
        return self.sessions.get(session_token)

    def touch_api_key(self, prefix):
        self.touched.append(prefix)


@pytest.fixture
def control() -> FakeControl:
    fake = FakeControl()
    fake.add_tenant()
    return fake


# --------------------------------------------------------------------------
# Nothing works without a credential
# --------------------------------------------------------------------------


@pytest.mark.parametrize("presented", [None, "", "   ", "Bearer ", "Bearer undefined"])
@pytest.mark.parametrize("via", FRONTS)
def test_no_credential_is_refused_on_every_front(control, presented, via):
    with pytest.raises(AuthError):
        authenticate_api_key(control, presented, via=via)


def test_no_session_cookie_is_refused(control):
    with pytest.raises(AuthError):
        authenticate_session(control, None)


def test_a_garbage_session_cookie_is_refused(control):
    with pytest.raises(AuthError):
        authenticate_session(control, "not-a-session")


def test_authentication_never_returns_none(control):
    """The signature is the guarantee. A function that can return None invites
    `principal = authenticate(...)` followed by code that forgets to check."""
    shown = control.add_key()

    assert authenticate_api_key(control, shown) is not None

    with pytest.raises(AuthError):
        authenticate_api_key(control, None)


def test_there_is_no_anonymous_principal():
    """Absence of a principal is represented by absence, not by a sentinel
    object that downstream code has to remember to test."""
    assert not hasattr(auth_module, "ANONYMOUS")
    assert not hasattr(auth_module, "anonymous")


def test_no_authentication_function_has_a_default_tenant():
    """The dangerous line, asserted against directly.

    `resolve_agent(dataset_id)` in the single-tenant API returns the default
    agent when its argument is None. The tenant translation of that shape hands
    the server's own database to an unauthenticated caller, and it fails open.
    """
    for name in ("authenticate_api_key", "authenticate_session"):
        signature = inspect.signature(getattr(auth_module, name))
        for parameter in signature.parameters.values():
            if parameter.name in ("control", "via"):
                continue
            assert parameter.default is inspect.Parameter.empty, (
                f"{name}({parameter.name}=...) has a default; a credential "
                f"parameter with a default is a fallback waiting to happen"
            )


# --------------------------------------------------------------------------
# A valid credential works, on every front
# --------------------------------------------------------------------------


@pytest.mark.parametrize("via", FRONTS)
def test_a_valid_key_resolves_to_its_tenant(control, via):
    shown = control.add_key("t_1")

    principal = authenticate_api_key(control, shown, via=via)

    assert principal.tenant_id == "t_1"
    assert principal.via == via


def test_a_bearer_header_is_accepted(control):
    shown = control.add_key()

    assert authenticate_api_key(control, f"Bearer {shown}").tenant_id == "t_1"


def test_a_valid_session_resolves_to_its_tenant(control):
    control.sessions["sess_abc"] = "t_1"

    principal = authenticate_session(control, "sess_abc")

    assert principal.tenant_id == "t_1"
    assert principal.via == "session"


def test_the_principal_records_which_key_was_used(control):
    """Not the key. "This credential was used from the SDK at 04:00" is the
    sentence that makes an incident investigable."""
    shown = control.add_key()
    prefix, secret = split_api_key(shown)

    principal = authenticate_api_key(control, shown)

    assert principal.key_prefix == prefix
    assert secret not in repr(principal)


def test_using_a_key_records_that_it_was_used(control):
    """So "revoke the key nothing has touched in a year" is answerable."""
    shown = control.add_key()

    authenticate_api_key(control, shown)

    assert len(control.touched) == 1


def test_a_failed_attempt_does_not_record_a_use(control):
    control.add_key()

    with pytest.raises(AuthError):
        authenticate_api_key(control, "ak_live_nope.secret")

    assert control.touched == []


# --------------------------------------------------------------------------
# Cross-tenant
# --------------------------------------------------------------------------


def test_a_key_only_ever_resolves_to_its_own_tenant(control):
    control.add_tenant("t_2")
    shown_one = control.add_key("t_1")
    shown_two = control.add_key("t_2")

    assert authenticate_api_key(control, shown_one).tenant_id == "t_1"
    assert authenticate_api_key(control, shown_two).tenant_id == "t_2"


def test_one_tenants_secret_against_another_prefix_is_refused(control):
    control.add_tenant("t_2")
    shown_one = control.add_key("t_1")
    shown_two = control.add_key("t_2")
    prefix_two, _ = split_api_key(shown_two)
    _, secret_one = split_api_key(shown_one)

    with pytest.raises(AuthError):
        authenticate_api_key(control, f"{prefix_two}.{secret_one}")


def test_a_key_whose_tenant_was_deleted_does_not_authenticate(control):
    shown = control.add_key("t_1")
    del control.tenants["t_1"]

    with pytest.raises(AuthError):
        authenticate_api_key(control, shown)


# --------------------------------------------------------------------------
# Revocation and suspension
# --------------------------------------------------------------------------


def test_a_revoked_key_is_refused(control):
    from dataclasses import replace

    shown = control.add_key()
    prefix, _ = split_api_key(shown)
    control.keys[prefix] = replace(control.keys[prefix], revoked=True)

    with pytest.raises(AuthError):
        authenticate_api_key(control, shown)


def test_a_suspended_tenant_is_told_so(control):
    """The one distinction worth making: they authenticated, so saying the
    account is suspended leaks nothing, and leaving them to debug a generic 401
    against a working key is cruel."""
    control.add_tenant("t_sus", active=False)
    shown = control.add_key("t_sus")

    with pytest.raises(SuspendedError, match="suspended"):
        authenticate_api_key(control, shown)


def test_a_suspended_tenant_is_refused_on_the_session_path_too(control):
    """Suspension is one write. It must not be partially applied across
    fronts."""
    control.add_tenant("t_sus", active=False)
    control.sessions["sess"] = "t_sus"

    with pytest.raises(SuspendedError):
        authenticate_session(control, "sess")


# --------------------------------------------------------------------------
# Every failure looks the same
# --------------------------------------------------------------------------


def test_unknown_and_wrong_are_indistinguishable(control):
    """Distinguishing them tells an attacker when they have found a real key."""
    shown = control.add_key()
    prefix, _ = split_api_key(shown)

    messages = set()
    for presented in (
        "ak_live_doesnotexist.secret",     # unknown prefix
        f"{prefix}.wrong-secret",          # real prefix, wrong secret
        "garbage",                         # malformed
    ):
        try:
            authenticate_api_key(control, presented)
        except AuthError as error:
            messages.add(str(error))

    assert messages == {UNAUTHENTICATED}


def test_the_message_names_the_header_not_the_problem(control):
    """The one thing a legitimate caller needs is the format."""
    assert "Authorization" in UNAUTHENTICATED
    assert "Bearer" in UNAUTHENTICATED


def test_no_error_message_contains_a_secret(control):
    shown = control.add_key()
    _, secret = split_api_key(shown)

    try:
        authenticate_api_key(control, f"ak_live_nope.{secret}")
    except AuthError as error:
        assert secret not in str(error)


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------


def test_a_key_may_ask_questions(control):
    shown = control.add_key()

    require_scope(authenticate_api_key(control, shown), "ask")


def test_a_key_may_not_do_what_it_was_not_granted(control):
    """403, not 401: authenticated and not permitted is genuinely different,
    and it is the one case where saying so helps and costs nothing."""
    shown = control.add_key()
    principal = authenticate_api_key(control, shown)

    with pytest.raises(PermissionError, match="not permitted"):
        require_scope(principal, "billing")


def test_scopes_are_not_implicitly_granted():
    assert not Principal(tenant_id="t_1", via="sdk").can("admin")
