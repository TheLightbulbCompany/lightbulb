"""R78/R83: the invite swap answers CORS for https://<label>.{APPS_DOMAIN} only, without credentials."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from main import app  # noqa: E402

PATH = "/api/v1/apps/invite/tok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "APPS_DOMAIN", "apps.example.com")
    with patch("routers.apps.svc.swap_invite", AsyncMock(return_value="pass")):
        yield TestClient(app)


def test_preflight_from_an_app_origin(client):
    o = "https://crew.apps.example.com"
    r = client.options(PATH, headers={"Origin": o, "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == o
    assert "access-control-allow-credentials" not in r.headers


def test_get_from_an_app_origin(client):
    o = "https://crew-preview.apps.example.com"
    r = client.get(PATH, headers={"Origin": o})
    assert r.json() == {"token": "pass"}
    assert r.headers["access-control-allow-origin"] == o
    assert "access-control-allow-credentials" not in r.headers


@pytest.mark.parametrize("o", ["https://evil.com", "https://a.b.apps.example.com", "http://crew.apps.example.com"])
def test_other_origins_get_no_cors(client, o):
    for r in (
        client.get(PATH, headers={"Origin": o}),
        client.options(PATH, headers={"Origin": o, "Access-Control-Request-Method": "GET"}),
    ):
        assert "access-control-allow-origin" not in r.headers


def test_other_paths_do_not_answer_app_origins(client):
    r = client.get("/api/v1/apps/issuer", headers={"Origin": "https://crew.apps.example.com"})
    assert "access-control-allow-origin" not in r.headers
