"""Hosted mode: the service token is a gate, and writes stay impossible.

The interesting assertions are the negative ones. Local Aperture treats a
missing credential as "the local user", which is right for a tool on your own
laptop and exactly wrong for a service holding a production connection, so
these tests pin the difference.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aperture.config import settings
from aperture.db import Database


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("APERTURE_HOSTED", "true")
    monkeypatch.setenv("APERTURE_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("APERTURE_HOME_DIR", str(tmp_path))
    settings.cache_clear()
    from aperture import server

    yield server
    settings.cache_clear()


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.delenv("APERTURE_HOSTED", raising=False)
    monkeypatch.setenv("APERTURE_HOME_DIR", str(tmp_path))
    settings.cache_clear()
    from aperture import server

    yield server
    settings.cache_clear()


def client(server) -> TestClient:
    # `raise_server_exceptions=False` keeps a startup refusal visible as a
    # response rather than an exception escaping the test client.
    return TestClient(server.app)


def test_missing_token_is_rejected(hosted):
    with client(hosted) as c:
        assert c.get("/connections").status_code == 401


def test_wrong_token_is_rejected(hosted):
    with client(hosted) as c:
        response = c.get("/connections", headers={"X-Aperture-Token": "not-it"})
        assert response.status_code == 401


def test_correct_token_is_accepted(hosted):
    with client(hosted) as c:
        response = c.get(
            "/connections",
            headers={"X-Aperture-Token": "test-token", "X-Aperture-User": "admin-1"},
        )
        assert response.status_code == 200


def test_health_stays_open(hosted):
    with client(hosted) as c:
        body = c.get("/health").json()
        assert body["ok"] is True
        assert body["hosted"] is True


def test_adding_a_connection_is_disabled(hosted):
    with client(hosted) as c:
        response = c.post(
            "/connections",
            json={"name": "x", "url": "sqlite:///tmp.db"},
            headers={"X-Aperture-Token": "test-token", "X-Aperture-User": "admin-1"},
        )
        assert response.status_code == 403


def test_upload_is_disabled(hosted):
    with client(hosted) as c:
        response = c.post(
            "/upload",
            files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
            headers={"X-Aperture-Token": "test-token", "X-Aperture-User": "admin-1"},
        )
        assert response.status_code == 403


def test_two_callers_get_separate_identities(hosted):
    from aperture.auth import service_user

    assert service_user("admin-1").id != service_user("admin-2").id
    assert service_user("admin-1").id == "service:admin-1"


def test_local_mode_still_needs_no_credentials(local):
    with client(local) as c:
        assert c.get("/connections").status_code == 200


def test_empty_service_token_never_authenticates(monkeypatch):
    from aperture.auth import ServiceAuthError, check_service_token

    monkeypatch.setenv("APERTURE_SERVICE_TOKEN", "")
    settings.cache_clear()
    with pytest.raises(ServiceAuthError):
        check_service_token("")
    with pytest.raises(ServiceAuthError):
        check_service_token(None)
    settings.cache_clear()


def test_writable_sqlite_is_not_read_only(tmp_path):
    path = tmp_path / "data.db"
    database = Database(f"sqlite:///{path}")
    database.run("SELECT 1")
    report = database.read_only_report()
    assert report.read_only is False
    assert "mode=ro" in report.summary()


def test_read_only_sqlite_uri_is_accepted(tmp_path):
    path = tmp_path / "data.db"
    Database(f"sqlite:///{path}").run("SELECT 1")
    database = Database(f"sqlite:///file:{path}?mode=ro&uri=true")
    assert database.read_only_report().read_only is True
    assert database.assert_read_only().read_only is True


def test_assert_read_only_raises_for_a_writable_database(tmp_path):
    from aperture.db import NotReadOnly

    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    with pytest.raises(NotReadOnly):
        database.assert_read_only()
