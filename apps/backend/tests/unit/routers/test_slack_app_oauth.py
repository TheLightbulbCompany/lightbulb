"""The Slack OAuth install rail: start (signed-in admin) → Slack → callback."""

import os
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402

ADMIN = AuthContext(user_id="user_1", org_id="org_1", org_role="org:admin")


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _app_store():
    return patch(
        "core.services.credentials.platform_oauth_app_store.get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"}),
    )


def _billing(status="active"):
    return patch(
        "routers.slack_app.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None if status is None else {"subscription_status": status}),
    )


def test_start_returns_authorize_url_and_stores_state(client):
    with _app_store(), _billing(), patch("routers.slack_app.oauth_state_repo.put", AsyncMock()) as put:
        r = client.get("/api/v1/slack/oauth/start?agent_id=agent_f")
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "slack.com" and url.path == "/oauth/v2/authorize"
    assert q["client_id"] == ["cid"]
    assert "chat:write" in q["scope"][0].split(",")
    assert q["redirect_uri"][0].endswith("/api/v1/slack/oauth/callback")
    state = q["state"][0]
    put.assert_awaited_once()
    assert put.call_args.args[0] == state
    assert put.call_args.kwargs["owner_id"] == "org_1"
    assert put.call_args.kwargs["extra"] == {"agent_id": "agent_f", "member_id": "user_1"}


def test_start_refuses_a_trial_owner_with_402(client):
    with _app_store(), _billing("trial_local"), patch("routers.slack_app.oauth_state_repo.put", AsyncMock()) as put:
        r = client.get("/api/v1/slack/oauth/start?agent_id=agent_f")
    assert r.status_code == 402
    assert r.json()["detail"] == "slack_requires_paid_plan"
    put.assert_not_called()


def test_start_requires_org_admin(client):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_2", org_id="org_1", org_role="org:member"
    )
    with _app_store():
        r = client.get("/api/v1/slack/oauth/start?agent_id=agent_f")
    assert r.status_code == 403


class _SlackToken(httpx.AsyncBaseTransport):
    def __init__(self, ok=True):
        self.ok, self.seen = ok, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        if not self.ok:
            return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-token",
                "scope": "chat:write,app_mentions:read",
                "bot_user_id": "U_BOT",
                "app_id": "A1",
                "team": {"id": "T1", "name": "Acme"},
                "authed_user": {"id": "U_ADMIN"},
            },
        )


def test_callback_exchanges_code_applies_install_and_redirects(client):
    t = _SlackToken()
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
        patch(
            "routers.slack_app.slack_install_service.apply_install", AsyncMock(return_value={"team_id": "T1"})
        ) as apply,
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c0de&state=st", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith(
        "/chat?panel=agent-inspector&agent=agent_f&inspectorTab=channels&slack=connected"
    )
    form = parse_qs(t.seen[0].content.decode())
    assert form["code"] == ["c0de"] and form["client_id"] == ["cid"] and form["client_secret"] == ["cs"]
    kw = apply.call_args.kwargs
    assert kw["owner_id"] == "org_1" and kw["agent_id"] == "agent_f" and kw["team_id"] == "T1"
    assert kw["bot_token"] == "xoxb-token" and kw["scopes"] == ["chat:write", "app_mentions:read"]
    assert kw["installer_slack_user_id"] == "U_ADMIN" and kw["installer_member_id"] == "user_1"


def test_callback_with_bad_state_redirects_with_error(client):
    with _app_store(), patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=None)):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=bad", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:invalid_state" in r.headers["location"]


def test_callback_with_slack_error_redirects_with_error(client):
    t = _SlackToken(ok=False)
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:invalid_code" in r.headers["location"]


def test_callback_conflict_redirects_with_error(client):
    from core.services.slack_install_service import SlackInstallConflict

    t = _SlackToken()
    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=t)),
        patch(
            "routers.slack_app.slack_install_service.apply_install", AsyncMock(side_effect=SlackInstallConflict("T1"))
        ),
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:workspace_already_connected" in r.headers["location"]


def test_callback_unexpected_error_redirects_with_internal(client):
    """Slack answered ok:true but without a team object — a bare KeyError must
    never become a 500 for the human browser Slack just redirected."""

    class _Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"ok": True, "access_token": "xoxb-token"})  # no "team"

    state_row = {"state": "st", "owner_id": "org_1", "provider": "slack", "agent_id": "agent_f", "member_id": "user_1"}
    with (
        _app_store(),
        patch("routers.slack_app.oauth_state_repo.consume", AsyncMock(return_value=state_row)),
        patch("routers.slack_app._client_factory", lambda: httpx.AsyncClient(transport=_Broken())),
    ):
        r = client.get("/api/v1/slack/oauth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 302 and "slack=error:internal" in r.headers["location"]


def test_list_installs_for_owner(client):
    rows = [
        {
            "team_id": "T1",
            "team_name": "Acme",
            "agent_id": "agent_f",
            "status": "active",
            "installed_at": "2026-09-04T00:00:00+00:00",
            "bot_user_id": "U",
        }
    ]
    with patch("routers.slack_app.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)):
        r = client.get("/api/v1/slack/install")
    assert r.status_code == 200
    assert r.json() == {
        "installs": [
            {
                "team_id": "T1",
                "team_name": "Acme",
                "agent_id": "agent_f",
                "status": "active",
                "installed_at": "2026-09-04T00:00:00+00:00",
            }
        ]
    }


def test_delete_install_revokes_only_own_team(client):
    with (
        patch(
            "routers.slack_app.slack_installs_repo.get", AsyncMock(return_value={"team_id": "T1", "owner_id": "org_1"})
        ),
        patch("routers.slack_app.slack_install_service.revoke_install", AsyncMock(return_value=True)) as rev,
    ):
        r = client.delete("/api/v1/slack/install/T1")
    assert r.status_code == 200 and r.json() == {"status": "revoked"}
    rev.assert_awaited_once_with("T1")
    with patch(
        "routers.slack_app.slack_installs_repo.get", AsyncMock(return_value={"team_id": "T1", "owner_id": "org_other"})
    ):
        r = client.delete("/api/v1/slack/install/T1")
    assert r.status_code == 404
