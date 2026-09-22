"""The authentication boundary, as the API actually exposes it.

The test that matters here is `test_every_data_route_requires_a_principal`. It
walks the app's own route table rather than a hand-written list, because
"remember to add the dependency" is not a security control and the one route
somebody forgets is the one that matters. A list maintained by hand would be
missing exactly the route that was missing from the code.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlagent.api import app as api
from sqlagent.api.app import app
from sqlagent.api.auth_routes import get_control
from sqlagent.config import Settings

PUBLIC = {
    "/api/health",      # a load balancer probes it before anyone signs in
    "/api/plans",       # a pricing page is read before there is an account
    "/api/options",     # static descriptions of the toggles; no tenant data
    "/api/auth/sign-up",
    "/api/auth/sign-in",
    "/api/auth/sign-out",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}


def all_routes(router=None):
    """Every route, flattened.

    This FastAPI version wraps an included router in `_IncludedRouter` rather
    than copying its routes onto the app, so a flat scan of `app.routes` misses
    everything mounted that way — including, of course, the authentication
    routes. Recursing is what makes the check below actually cover the app
    rather than the half of it that happens to be declared inline.
    """
    for route in (router or app).routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from all_routes(nested)
        else:
            yield route


def data_routes():
    """Every route that is not deliberately public."""
    for route in all_routes():
        path = getattr(route, "path", None)
        if path and path not in PUBLIC:
            yield route


def test_every_data_route_requires_a_principal():
    """Walked from the route table, not from a hand-written list.

    A route added without the dependency reads the server's own database and
    returns it to whoever asked. That fails open, so nothing else in the suite
    would notice — and a list maintained by hand would be missing exactly the
    route that was missing from the code.
    """
    missing = []
    for route in data_routes():
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = {dep.call.__name__ for dep in dependant.dependencies if dep.call}
        if "principal_dependency" not in names:
            missing.append(route.path)

    assert not missing, f"routes with no authentication: {sorted(set(missing))}"


def test_the_public_list_is_deliberate():
    """Every entry is a route that really exists. A stale entry silently
    exempts nothing, but a typo in a new one silently exempts a real route."""
    paths = {getattr(route, "path", None) for route in all_routes()}
    for public in PUBLIC:
        if public.startswith("/api"):
            assert public in paths, public


# --------------------------------------------------------------------------
# Single-tenant mode
# --------------------------------------------------------------------------


@pytest.fixture
def single_tenant(monkeypatch, tmp_path):
    from tests.test_api import StubAgent, _install

    _install(monkeypatch, StubAgent(), tmp_path)
    monkeypatch.setattr(
        api, "settings", lambda: Settings(_env_file=None, database_url="sqlite://")
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_without_auth_required_the_api_works_as_before(single_tenant):
    """The CLI, the benchmark and a team running this against their own
    warehouse must not have to invent an account."""
    assert single_tenant.get("/api/health").status_code == 200
    assert single_tenant.post("/api/ask", json={"question": "how many?"}).status_code == 200


# --------------------------------------------------------------------------
# Multi-tenant mode
# --------------------------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    """The API with authentication on and a real control database."""
    from tests.test_api import StubAgent, _install

    from sqlagent.api import auth_routes
    from sqlagent.saas.control import ControlStore
    from sqlagent.saas.secrets import Cipher

    _install(monkeypatch, StubAgent(), tmp_path)

    config = Settings(
        _env_file=None,
        database_url="sqlite://",
        require_auth=True,
        secret_key=Cipher.generate_key(),
    )
    control = ControlStore(tmp_path / "control.db")

    for module in (api, auth_routes):
        monkeypatch.setattr(module, "settings", lambda: config)
    monkeypatch.setattr(auth_routes, "get_control", lambda: control)

    get_control.cache_clear()
    with TestClient(app) as client:
        yield client, control
    app.dependency_overrides.clear()
    get_control.cache_clear()


PASSWORD = "a long enough passphrase"


def test_an_unauthenticated_request_is_refused(hosted):
    client, _ = hosted

    assert client.post("/api/ask", json={"question": "how many?"}).status_code == 401


def test_the_refusal_says_how_to_authenticate(hosted):
    """The one useful thing a 401 can say without leaking anything."""
    client, _ = hosted
    response = client.post("/api/ask", json={"question": "x"})

    assert "Bearer" in response.json()["detail"]
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_health_is_still_public(hosted):
    """A load balancer probes it before anyone has signed in."""
    client, _ = hosted

    assert client.get("/api/health").status_code == 200


def test_pricing_is_still_public(hosted):
    client, _ = hosted

    assert client.get("/api/plans").status_code == 200


def test_signing_up_creates_a_workspace_and_signs_you_in(hosted):
    client, _ = hosted

    response = client.post(
        "/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["plan"] == "FREE"
    assert client.cookies.get("aperture_session")


def test_a_session_cookie_then_works(hosted):
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})

    assert client.post("/api/ask", json={"question": "how many?"}).status_code == 200


def test_the_session_cookie_is_not_readable_by_javascript(hosted):
    """httponly is the single highest-value flag here: it is what stops an XSS
    bug from becoming stolen sessions."""
    client, _ = hosted
    response = client.post(
        "/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD}
    )

    assert "httponly" in response.headers["set-cookie"].lower()


def test_signing_out_kills_the_session_server_side(hosted):
    """Not merely clearing the cookie — a cookie is a copy the client holds,
    and anyone who captured it would still be signed in."""
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    stolen = client.cookies.get("aperture_session")

    client.post("/api/auth/sign-out")
    client.cookies.set("aperture_session", stolen)

    assert client.post("/api/ask", json={"question": "x"}).status_code == 401


def test_a_duplicate_email_is_refused(hosted):
    client, _ = hosted
    body = {"email": "ada@example.com", "password": PASSWORD}
    client.post("/api/auth/sign-up", json=body)

    assert client.post("/api/auth/sign-up", json=body).status_code == 409


def test_a_short_password_is_refused(hosted):
    client, _ = hosted

    response = client.post(
        "/api/auth/sign-up", json={"email": "ada@example.com", "password": "short"}
    )

    assert response.status_code == 400


def test_signing_in_with_the_wrong_password_fails(hosted):
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    client.post("/api/auth/sign-out")

    response = client.post(
        "/api/auth/sign-in", json={"email": "ada@example.com", "password": "wrong password!!"}
    )

    assert response.status_code == 401


def test_an_unknown_email_gives_the_same_message_as_a_wrong_password(hosted):
    """Two messages would let anyone enumerate which addresses have accounts."""
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    client.post("/api/auth/sign-out")

    wrong = client.post(
        "/api/auth/sign-in", json={"email": "ada@example.com", "password": "wrong password!!"}
    )
    unknown = client.post(
        "/api/auth/sign-in", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_an_api_key_authenticates(hosted):
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/sign-out")

    key = control.issue_api_key(tenant_id, name="ci")
    response = client.post(
        "/api/ask", json={"question": "how many?"}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200


def test_a_forged_api_key_does_not(hosted):
    client, _ = hosted

    response = client.post(
        "/api/ask",
        json={"question": "x"},
        headers={"Authorization": "Bearer ak_live_deadbeef.madeupsecret"},
    )

    assert response.status_code == 401


def test_a_revoked_api_key_stops_working(hosted):
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]
    key = control.issue_api_key(tenant_id)
    prefix = key.split(".")[0]

    headers = {"Authorization": f"Bearer {key}"}
    assert client.post("/api/ask", json={"question": "x"}, headers=headers).status_code == 200

    control.revoke_api_key(tenant_id, prefix)
    assert client.post("/api/ask", json={"question": "x"}, headers=headers).status_code == 401


def test_a_suspended_workspace_is_refused_with_403_not_401(hosted):
    """They authenticated. A generic 401 against a working credential is cruel
    and unactionable."""
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]

    from sqlalchemy import text

    with control._engine.begin() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            text("UPDATE tenants SET active = 0 WHERE id = :id"), {"id": tenant_id}
        )

    assert client.post("/api/ask", json={"question": "x"}).status_code == 403


# --------------------------------------------------------------------------
# Quotas
# --------------------------------------------------------------------------


def test_usage_starts_empty(hosted):
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})

    usage = client.get("/api/usage").json()

    assert usage["questions_used"] == 0
    assert usage["questions_limit"] == 20


def test_asking_a_question_counts_against_the_allowance(hosted):
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})

    client.post("/api/ask", json={"question": "how many?"})

    assert client.get("/api/usage").json()["questions_used"] == 1


def test_the_allowance_runs_out(hosted):
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]
    control.record_usage(tenant_id, "questions", 20)

    response = client.post("/api/ask", json={"question": "one more"})

    assert response.status_code == 402
    assert "20 of 20" in response.json()["detail"]


def test_a_free_tenant_asking_for_detailed_is_clamped_not_refused(hosted):
    """Refusing a whole question over one optional field turns an upsell into
    an outage."""
    client, _ = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})

    response = client.post(
        "/api/ask",
        json={"question": "how many?", "options": {"quality_tier": "thorough"}},
    )

    assert response.status_code == 200
    assert api.resolve_agent(None).seen_options["quality_tier"] == "fast"


def test_a_failed_question_does_not_consume_the_allowance(hosted, monkeypatch):
    """A customer charged for an error writes a support ticket that costs more
    than the question did."""
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]

    stub = api.resolve_agent(None)
    monkeypatch.setattr(stub, "fail", True)
    client.post("/api/ask", json={"question": "this will fail"})

    assert control.usage(tenant_id).get("questions", 0) == 0


def test_a_detailed_question_counts_against_the_detailed_budget(hosted):
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]
    control.set_plan(tenant_id, "PRO")

    client.post(
        "/api/ask",
        json={"question": "how many?", "options": {"quality_tier": "thorough"}},
    )

    counts = control.usage(tenant_id)
    assert counts.get("detailed") == 1
    assert counts.get("questions", 0) == 0


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_one_tenants_key_never_resolves_to_another(hosted):
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    first = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/sign-out")

    client.post("/api/auth/sign-up", json={"email": "bo@example.com", "password": PASSWORD})
    second = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/sign-out")

    assert first != second
    key = control.issue_api_key(first)
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {key}"})

    assert response.json()["tenant_id"] == first


def test_one_tenant_cannot_revoke_anothers_key(hosted):
    """The tenant is in the WHERE clause, not checked beforehand — a check and
    a write in two statements is a check the third call site skips."""
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    victim = client.get("/api/auth/me").json()["tenant_id"]
    key = control.issue_api_key(victim)
    prefix = key.split(".")[0]

    assert control.revoke_api_key("t_someone_else", prefix) is False
    assert control.api_key_by_prefix(prefix).revoked is False


def test_one_tenant_cannot_read_anothers_connection_credential(hosted):
    """The single most important scoping in the system: getting it wrong hands
    one customer's database credential to another."""
    client, control = hosted
    client.post("/api/auth/sign-up", json={"email": "ada@example.com", "password": PASSWORD})
    owner = client.get("/api/auth/me").json()["tenant_id"]
    saved = control.save_connection(
        owner,
        label="prod",
        encrypted_dsn="ciphertext",
        redacted_dsn="postgresql://u:***@h/db",
        dialect="postgresql",
        table_count=5,
    )

    assert control.encrypted_dsn(owner, saved.id) == "ciphertext"
    assert control.encrypted_dsn("t_someone_else", saved.id) is None
