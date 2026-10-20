"""Neon Auth token exchange and administrator approval, against the real routes.

Tokens are signed with a throwaway Ed25519 key and the verifier's key lookup is
pointed at it, so the signature, issuer, audience and expiry checks all run for
real without touching the network.
"""

from __future__ import annotations

import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

import sqlagent.api.auth_routes as routes
from sqlagent import auth as auth_module
from sqlagent.api.app import app
from sqlagent.api.auth_routes import require_user
from sqlagent.auth import Accounts, AuthError, TokenVerifier
from sqlagent.config import Settings

ISSUER = "https://ep-test.neonauth.example.tech"
KEY = ed25519.Ed25519PrivateKey.generate()


def make_token(
    sub="user-1",
    email="a@example.com",
    *,
    issuer=ISSUER,
    audience=ISSUER,
    exp=900,
    key=KEY,
    alg="EdDSA",
):
    now = int(time.time())
    claims = {"sub": sub, "iat": now, "exp": now + exp, "iss": issuer, "aud": audience}
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, key, algorithm=alg)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sent(monkeypatch):
    mails: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        auth_module,
        "send_mail",
        lambda config, to, subject, body: mails.append((to, subject, body)),
    )
    return mails


@pytest.fixture
def accounts(tmp_path):
    config = Settings(
        control_database_url=f"sqlite:///{tmp_path}/auth.db",
        neon_auth_base_url=f"{ISSUER}/neondb/auth",
        neon_auth_jwks_url=f"{ISSUER}/neondb/auth/.well-known/jwks.json",
        admin_email="admin@example.com",
        public_url="http://testserver",
    )
    verifier = TokenVerifier(config)
    verifier._key = lambda token: KEY.public_key()  # type: ignore[method-assign]
    return Accounts(config, verifier=verifier)


@pytest.fixture
def client(accounts, sent, monkeypatch):
    monkeypatch.setattr(routes, "get_accounts", lambda: accounts)
    app.dependency_overrides.pop(require_user, None)  # use the real gate
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def approval_token(sent) -> str:
    to, _, body = sent[-1]
    assert to == "admin@example.com"
    return re.search(r"token=([\w-]+)", body).group(1)


def approve(client, sent, decision="approve", token=None):
    return client.post(
        "/api/auth/approve",
        data={"token": token or approval_token(sent), "decision": decision},
    )


def test_api_is_closed_without_a_session(client):
    assert client.get("/api/schema").status_code == 401
    assert client.get("/api/datasets").status_code == 401


def test_health_stays_public_for_probes(client):
    from sqlagent.api.app import get_agent

    class Agent:
        snapshot = type("S", (dict,), {"version": "v"})({"t": 1})

    app.dependency_overrides[get_agent] = lambda: Agent()
    assert client.get("/api/healthz").status_code == 200


def test_session_needs_a_bearer_token(client):
    assert client.post("/api/auth/session").status_code == 401


def test_new_user_is_pending_and_admin_is_asked(client, sent):
    response = client.post("/api/auth/session", headers=bearer(make_token()))
    assert response.status_code == 403
    assert "waiting" in response.json()["detail"]
    assert len(sent) == 1 and "a@example.com" in sent[0][1]
    assert client.get("/api/auth/me").status_code == 401  # no cookie was issued


def test_asking_again_does_not_email_the_admin_again(client, sent):
    client.post("/api/auth/session", headers=bearer(make_token()))
    client.post("/api/auth/session", headers=bearer(make_token()))
    assert len(sent) == 1


def test_approval_flow_end_to_end(client, sent):
    client.post("/api/auth/session", headers=bearer(make_token()))
    token = approval_token(sent)

    # Opening the link must not approve anyone.
    page = client.get(f"/api/auth/approve?token={token}")
    assert "a@example.com" in page.text
    assert client.post("/api/auth/session", headers=bearer(make_token())).status_code == 403

    assert "approved" in approve(client, sent).text
    assert sent[-1][0] == "a@example.com"  # the user is told

    assert client.post("/api/auth/session", headers=bearer(make_token())).status_code == 200
    assert client.get("/api/auth/me").json() == {"email": "a@example.com"}


def test_token_is_single_use(client, sent):
    client.post("/api/auth/session", headers=bearer(make_token()))
    token = approval_token(sent)
    approve(client, sent, token=token)
    again = approve(client, sent, "reject", token=token)
    assert "already been used" in again.text
    assert client.post("/api/auth/session", headers=bearer(make_token())).status_code == 200


def test_rejected_user_gets_no_session(client, sent):
    client.post("/api/auth/session", headers=bearer(make_token()))
    approve(client, sent, "reject")
    response = client.post("/api/auth/session", headers=bearer(make_token()))
    assert response.status_code == 403
    assert client.get("/api/auth/me").status_code == 401


def test_logout_ends_the_session(client, sent):
    client.post("/api/auth/session", headers=bearer(make_token()))
    approve(client, sent)
    client.post("/api/auth/session", headers=bearer(make_token()))
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(make_token(exp=-60), id="expired"),
        pytest.param(make_token(issuer="https://evil.example"), id="wrong-issuer"),
        pytest.param(make_token(audience="https://evil.example"), id="wrong-audience"),
        pytest.param(make_token(key=ed25519.Ed25519PrivateKey.generate()), id="wrong-key"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
def test_bad_tokens_are_refused_and_create_nothing(client, sent, token):
    assert client.post("/api/auth/session", headers=bearer(token)).status_code == 401
    assert sent == []


def test_hs256_token_cannot_be_forged_with_the_public_key(accounts):
    forged = jwt.encode(
        {
            "sub": "x",
            "email": "x@example.com",
            "iss": ISSUER,
            "aud": ISSUER,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        "secret-of-at-least-thirty-two-bytes-long!!",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        accounts.verifier.verify(forged)


def test_unconfigured_server_refuses_everyone(tmp_path):
    verifier = TokenVerifier(Settings(control_database_url=f"sqlite:///{tmp_path}/a.db"))
    with pytest.raises(AuthError) as err:
        verifier.verify(make_token())
    assert err.value.status == 503


def test_neon_postgres_url_gets_the_driver_named():
    config = Settings(control_database_url="postgresql://u:p@host/db?sslmode=require")
    engine = auth_module._make_engine(config)
    assert engine.dialect.driver == "psycopg"
