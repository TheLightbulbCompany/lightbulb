"""/api/v1/company/slug-availability and /slug/hold -- the agent-email slug at "Create your workspace"."""

import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_slug as mod  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    # No org on the token: the caller is creating their first workspace.
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_new")
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "dev.thelightbulb.company")
    yield TestClient(app)
    app.dependency_overrides.clear()


def _reason(monkeypatch, reason):
    monkeypatch.setattr(mod, "slug_unavailable_reason", AsyncMock(return_value=reason))


def test_availability_reports_the_domain_for_the_preview(client, monkeypatch):
    _reason(monkeypatch, None)
    r = client.get("/api/v1/company/slug-availability", params={"slug": "bench-club"})
    assert r.status_code == 200
    assert r.json() == {"slug": "bench-club", "available": True, "reason": None, "domain": "dev.thelightbulb.company"}


def test_availability_says_why_not(client, monkeypatch):
    _reason(monkeypatch, "taken")
    assert client.get("/api/v1/company/slug-availability", params={"slug": "acme"}).json()["reason"] == "taken"


@pytest.mark.parametrize("reason", ["invalid", "reserved"])
def test_hold_refuses_a_slug_that_can_never_be_used(client, monkeypatch, reason):
    _reason(monkeypatch, reason)
    hold = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.company_slug_repo, "hold", hold)
    r = client.post("/api/v1/company/slug/hold", json={"slug": "support"})
    assert r.status_code == 400 and r.json()["detail"] == f"slug_{reason}"
    hold.assert_not_awaited()


def test_hold_409s_a_lost_race_and_holds_a_free_slug(client, monkeypatch):
    _reason(monkeypatch, None)
    monkeypatch.setattr(mod.company_slug_repo, "hold", AsyncMock(return_value=False))
    r = client.post("/api/v1/company/slug/hold", json={"slug": "bench-club"})
    assert r.status_code == 409 and r.json()["detail"] == "slug_taken"

    hold = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.company_slug_repo, "hold", hold)
    assert client.post("/api/v1/company/slug/hold", json={"slug": "bench-club"}).json() == {"slug": "bench-club"}
    hold.assert_awaited_once_with(slug="bench-club", user_id="user_new")
