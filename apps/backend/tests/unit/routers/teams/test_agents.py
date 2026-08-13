"""Tests for the Teams Agents BFF.

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
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))

    r = client.get(
        "/api/v1/teams/agents",
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 200
    # Every item is stamped with a ``seeded`` flag (False here — no seeded ids).
    assert r.json() == {"agents": [{"id": "a1", "seeded": False}]}
    admin.list_agents.assert_awaited_once_with(
        company_id="co_abc",
        session_cookie="cookie-value",
    )


def test_list_agents_stamps_seeded_flag(client, monkeypatch):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value={"agents": [{"id": "friday_id"}, {"id": "user_id"}]})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value={"friday_id"}))

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["agents"]}
    assert by_id["friday_id"]["seeded"] is True
    assert by_id["user_id"]["seeded"] is False


def test_create_agent_synthesizes_adapter_config(client, monkeypatch):
    admin = MagicMock()
    admin.create_agent = AsyncMock(return_value={"id": "a_new"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    # service_token.mint() is sync — patch the module attribute the route
    # imported at module load time so it returns a deterministic value.
    mint_mock = MagicMock(return_value="freshly-minted-token")
    monkeypatch.setattr(agents_mod.service_token, "mint", mint_mock)
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
    adapter_cfg = dict(call.kwargs["adapter_config"])
    # devicePrivateKeyPem is a freshly-minted ed25519 key (non-deterministic),
    # so assert it's a valid PEM separately and compare the stable fields.
    device_pem = adapter_cfg.pop("devicePrivateKeyPem")
    assert device_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert adapter_cfg == {
        "url": "wss://ws-dev.isol8.co?token=freshly-minted-token&clientId=paperclip-operator",
        "authToken": "freshly-minted-token",
        "sessionKeyStrategy": "issue",
        "sessionKey": "paperclip:u1",
        # Gateway run-timeout bounds so a long agent run isn't soft-timed-out.
        "timeoutSec": 600,
        "waitTimeoutMs": 600_000,
    }
    # Service token MUST be minted with owner_id
    # (== org_id for org members) so the WS authorizer routes Paperclip
    # wakes to the org's shared container, not the member's personal
    # owner. The fixture's TeamsContext has org_id="o1" / owner_id="o1".
    mint_mock.assert_called_once_with("o1")


def test_create_agent_response_redacts_device_private_key(client, monkeypatch):
    """The create response MUST NOT leak adapterConfig. Paperclip echoes the
    full config back on create — including the freshly-minted devicePrivateKeyPem
    (the operator device's signing key) and authToken — so anyone with the
    response/DOM could otherwise impersonate the operator device. The create path
    must redact like the read paths do."""
    from routers.teams import agents as agents_mod

    admin = MagicMock()
    admin.create_agent = AsyncMock(
        return_value={
            "id": "a_new",
            "name": "Helper",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {
                "url": "wss://ws-dev.isol8.co?token=t&clientId=paperclip-operator",
                "authToken": "SECRET-GATEWAY-TOKEN",
                "devicePrivateKeyPem": "-----BEGIN PRIVATE KEY-----\nLEAKME\n-----END PRIVATE KEY-----\n",
                "sessionKeyStrategy": "issue",
                "sessionKey": "paperclip:u1",
            },
        }
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod.service_token, "mint", MagicMock(return_value="tok"))
    monkeypatch.setattr(agents_mod, "_gateway_url_for_env", lambda: "wss://ws-dev.isol8.co")

    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "a_new"
    # Sensitive adapter fields stripped from the response.
    assert "adapterConfig" not in body
    assert "adapterType" not in body
    # Belt-and-suspenders: the private key / token text must not appear anywhere.
    assert "LEAKME" not in r.text
    assert "SECRET-GATEWAY-TOKEN" not in r.text


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
                "sessionKey": "paperclip:u1",
            },
        }
    )
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))

    r = client.get("/api/v1/teams/agents/a1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert "adapterConfig" not in body
    assert "adapterType" not in body
    assert body["name"] == "Helper"
    assert body["seeded"] is False


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
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    for agent in r.json()["agents"]:
        assert "adapterConfig" not in agent
        assert "adapterType" not in agent


def test_patch_agent_forwards_explicit_null_reports_to(client, monkeypatch):
    """Clearing a manager sends {"reportsTo": null}; the BFF must forward the
    null (exclude_unset, not exclude_none) so Paperclip makes the agent a root.
    Paperclip rejects "" (uuid().nullable())."""
    admin = MagicMock()
    admin.patch_agent = AsyncMock(return_value={"id": "a1", "reportsTo": None})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/agents/a1",
        json={"reportsTo": None},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    forwarded = admin.patch_agent.await_args.kwargs["body"]
    assert forwarded == {"reportsTo": None}


def test_patch_agent_drops_null_for_non_reports_to_fields(client, monkeypatch):
    """Only reportsTo is nullable upstream. An explicit null for other optional
    fields (title here) must still be dropped, not forwarded, while a
    concurrent reportsTo value passes through."""
    admin = MagicMock()
    admin.patch_agent = AsyncMock(return_value={"id": "a1"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/agents/a1",
        json={"title": None, "reportsTo": "mgr1"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    forwarded = admin.patch_agent.await_args.kwargs["body"]
    assert forwarded == {"reportsTo": "mgr1"}
    assert "title" not in forwarded


def test_delete_agent_rejects_seeded(client, monkeypatch):
    """The REST delete path must refuse seeded agents (Friday/Atlas) — same
    guard as the OpenClaw gateway path. 403, upstream delete NOT called."""
    admin = MagicMock()
    admin.delete_agent = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "is_seeded_agent", AsyncMock(return_value=True))

    r = client.delete("/api/v1/teams/agents/friday_id", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403
    admin.delete_agent.assert_not_awaited()


def test_delete_agent_allows_non_seeded(client, monkeypatch):
    admin = MagicMock()
    admin.delete_agent = AsyncMock(return_value={"ok": True})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "is_seeded_agent", AsyncMock(return_value=False))

    r = client.delete("/api/v1/teams/agents/a_user", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.delete_agent.assert_awaited_once()


def test_legacy_runs_alias_proxies_heartbeat_run(client, monkeypatch):
    """/runs/{id} is a legacy alias — it must proxy /api/heartbeat-runs/{id}.

    Upstream never had GET /api/runs/{id}; the frontend RunDetailPanel still
    reads this path as a fallback, so it must resolve, not 404.
    """
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "r1", "status": "failed"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/runs/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["id"] == "r1"
    admin.get_heartbeat_run.assert_awaited_once_with(
        run_id="r1",
        session_cookie="cookie-value",
    )
