"""Tests for the Teams Agents BFF (Task 6).

Spec §5: every agent-mutating endpoint MUST synthesize the openclaw_gateway
adapter config server-side and reject any client-supplied
``adapterType``/``adapterConfig``/``url``/``headers`` with 422 at the
FastAPI boundary.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1",
        org_id="o1",
        owner_id="o1",
        company_id="co_abc",
        paperclip_user_id="pcu_xyz",
        session_cookie="cookie-value",
    )


@pytest.fixture
def client(teams_ctx, monkeypatch):
    # Bypass auth + the upstream sign-in chain by overriding the
    # router-local _ctx Depends helper. Monkeypatching deps.resolve_teams_context
    # would not work because agents.py imports the name into its own module
    # namespace at import time.
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_agents_calls_upstream_with_user_session(client, monkeypatch):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value={"agents": [{"id": "a1"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get(
        "/api/v1/teams/agents",
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 200
    assert r.json() == {"agents": [{"id": "a1"}]}
    admin.list_agents.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie-value",
    )


def test_create_agent_synthesizes_adapter_config(client, monkeypatch):
    admin = MagicMock()
    admin.create_agent = AsyncMock(return_value={"id": "a_new"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    # _decrypt_service_token is async (reads DDB). _gateway_url_for_env is sync.
    monkeypatch.setattr(
        agents_mod,
        "_decrypt_service_token",
        AsyncMock(return_value="decrypted-token"),
    )
    monkeypatch.setattr(
        agents_mod,
        "_gateway_url_for_env",
        lambda: "wss://ws-dev.isol8.co",
    )

    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 200
    call = admin.create_agent.await_args
    assert call.kwargs["adapter_type"] == "openclaw_gateway"
    assert call.kwargs["adapter_config"] == {
        "url": "wss://ws-dev.isol8.co",
        "authToken": "decrypted-token",
        "sessionKeyStrategy": "issue",
        "sessionKey": "u1",
    }


def test_create_agent_rejects_client_supplied_adapter_type(client):
    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer", "adapterType": "process"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 422


def test_create_agent_rejects_client_supplied_url(client):
    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer", "url": "http://evil"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 422


def test_get_agent_strips_adapter_config(client, monkeypatch):
    """Defense in depth: even if Paperclip echoes adapterConfig (containing
    the user's service token) in the GET response, our BFF must strip it
    before sending to the browser. Three detail panels render JSON dumps."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(
        return_value={
            "id": "a1",
            "name": "Helper",
            "role": "engineer",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {
                "url": "wss://ws-dev.isol8.co",
                "authToken": "SENSITIVE_USER_TOKEN",
                "sessionKeyStrategy": "issue",
                "sessionKey": "u1",
            },
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/agents/a1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert "adapterConfig" not in body
    assert "adapterType" not in body
    assert body["name"] == "Helper"


def test_create_agent_returns_upstream_status_on_paperclip_error(client, monkeypatch):
    """When Paperclip returns a non-2xx (e.g. 403 because the membership
    role lacks ``agents:create``), the BFF must surface the upstream
    status to the caller instead of crashing into a generic 500. The
    global ``PaperclipApiError`` handler in ``main.py`` is what makes
    this work; this test guards it from regressing.
    """
    from core.services.paperclip_admin_client import PaperclipApiError
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.create_agent = AsyncMock(
        side_effect=PaperclipApiError(
            "POST /api/companies/co_abc/agents -> 403",
            status_code=403,
            body='{"error":"Missing permission: agents:create"}',
        )
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        agents_mod,
        "_decrypt_service_token",
        AsyncMock(return_value="decrypted-token"),
    )
    monkeypatch.setattr(
        agents_mod,
        "_gateway_url_for_env",
        lambda: "wss://ws-dev.isol8.co",
    )

    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["upstream_status"] == 403
    assert "403" in body["detail"]


def test_list_agents_strips_adapter_config_per_item(client, monkeypatch):
    admin = MagicMock()
    admin.list_agents = AsyncMock(
        return_value={
            "agents": [
                {"id": "a1", "name": "X", "adapterType": "openclaw_gateway", "adapterConfig": {"authToken": "SECRET"}},
                {"id": "a2", "name": "Y", "adapterConfig": {"authToken": "SECRET2"}},
            ],
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    for agent in r.json()["agents"]:
        assert "adapterConfig" not in agent
        assert "adapterType" not in agent
