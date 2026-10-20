"""The container serves the built frontend from the API process."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sqlagent.api.app import mount_frontend


@pytest.fixture
def client(tmp_path):
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "app.js").write_text("console.log(1)")
    target = FastAPI()

    @target.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    mount_frontend(target, str(tmp_path))
    return TestClient(target)


def test_pages_assets_and_client_routes(client):
    assert "app" in client.get("/").text
    assert client.get("/app.js").text == "console.log(1)"
    assert "app" in client.get("/some/client/route").text  # SPA fallback


def test_api_routes_still_win(client):
    assert client.get("/api/ping").json() == {"ok": True}


def test_unknown_api_path_is_not_the_page(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert "<html>" not in response.text


def test_liveness_needs_no_login_and_no_database():
    from sqlagent.api.app import app

    with TestClient(app) as test_client:
        assert test_client.get("/api/healthz").json() == {"status": "ok"}


def test_health_reports_no_database_instead_of_failing(monkeypatch):
    """With no configured database the server is up: say so, do not 500."""
    from fastapi.testclient import TestClient

    from sqlagent.api import app as api

    def unavailable():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(api, "get_agent", unavailable)
    client = TestClient(api.app)
    body = client.get("/api/health").json()
    assert body["status"] == "no_database"
    assert body["tables"] == 0
    assert client.get("/api/schema").status_code == 503
