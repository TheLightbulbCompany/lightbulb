"""Tests for the Teams Agents BFF.

Spec §5: every agent-mutating endpoint MUST synthesize the openclaw_gateway
adapter config server-side and reject any client-supplied
``adapterType``/``adapterConfig``/``url``/``headers`` with 422 at the
FastAPI boundary.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from core.config import apex_domain, settings as _settings, url_host
from main import app
from routers.teams.deps import TeamsContext

# The adapter allowlist derives from settings.GATEWAY_RELAY_URL; derive the
# fixture host from the same place so it cannot drift out of the allowlist.
GW_APEX = apex_domain(url_host(_settings.GATEWAY_RELAY_URL))


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

    monkeypatch.setattr(agents_mod, "_load_agent_sidecars", AsyncMock(return_value={}))
    monkeypatch.setattr(agents_mod, "get_starter_agent_ids", AsyncMock(return_value=set()))
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="u1", org_id="o1")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)
        app.dependency_overrides.pop(get_current_user, None)


def _roster(monkeypatch, *agents):
    """``GET /teams/agents`` reads the registry: seed its roster (status active unless given)."""
    from core.services import agent_registry

    rows = [{"status": "active", **a} for a in agents]
    monkeypatch.setattr(agent_registry, "roster", AsyncMock(return_value=rows))


def test_list_agents_stamps_seeded_flag(client, monkeypatch):
    from routers.teams import agents as agents_mod

    _roster(monkeypatch, {"id": "friday_id"}, {"id": "user_id"})
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value={"friday_id"}))

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["agents"]}
    assert by_id["friday_id"]["seeded"] is True
    assert by_id["friday_id"]["origin"] == "Seed"
    assert by_id["user_id"]["seeded"] is False
    assert by_id["user_id"]["origin"] == "Custom"


def test_list_agents_stamps_starter_without_seed_protection(client, monkeypatch):
    from routers.teams import agents as agents_mod

    _roster(monkeypatch, {"id": "agent-ceo"})
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(agents_mod, "get_starter_agent_ids", AsyncMock(return_value={"agent-ceo"}))

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer test"})

    assert r.json() == {"agents": [{"id": "agent-ceo", "status": "idle", "seeded": False, "origin": "Starter"}]}


def test_list_agents_stamps_complete_marketplace_provenance(client, monkeypatch):
    from routers.teams import agents as agents_mod

    _roster(monkeypatch, {"id": "agent_market"})
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        agents_mod,
        "_load_agent_sidecars",
        AsyncMock(
            return_value={
                "agent_market": {
                    "listing_id": "listing-123",
                    "template_slug": "forge",
                    "template_version": 4,
                    "seller_id": "seller-456",
                }
            }
        ),
    )

    r = client.get("/api/v1/teams/agents", headers={"Authorization": "Bearer x"})

    assert r.status_code == 200
    assert r.json()["agents"][0] == {
        "id": "agent_market",
        "status": "idle",
        "seeded": False,
        "origin": "Marketplace",
        "marketplace": {
            "listingId": "listing-123",
            "slug": "forge",
            "version": 4,
            "seller": "seller-456",
        },
    }


def test_create_agent_on_agentcore_owner_gets_the_native_hermes_adapter(client, monkeypatch):
    """AgentCore owners use Paperclip's native polling adapter through our backend."""
    admin = MagicMock()
    admin.create_agent = AsyncMock(return_value={"id": "a_new"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"runtime_host": "agentcore", "hook_token": "hook_abc"}),
    )
    monkeypatch.setattr(
        "core.services.paperclip_adapter_config.settings.PUBLIC_API_BASE", "https://api-dev.thelightbulb.company"
    )
    # M6-R59: an agentcore create now also writes the agent's Hermes profile (and 502s if it
    # can't). Its own behaviour is pinned in test_teams_agents_pairing.py; stub it out here.
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 200
    call = admin.create_agent.await_args
    assert call.kwargs["adapter_type"] == "hermes_gateway"
    assert call.kwargs["adapter_config"]["apiKey"] == "hook_abc"
    assert call.kwargs["adapter_config"]["apiBaseUrl"] == (
        "https://api-dev.thelightbulb.company/api/v1/internal/paperclip/hermes/o1"
    )
    write_profile.assert_awaited_once()


def test_create_agent_sends_can_create_agents_false(client, monkeypatch):
    """Paperclip 916 defaults a new agent's canCreateAgents to TRUE; a Teams-created agent must
    reach Paperclip with an explicit false (through the real admin-client funnel)."""
    import json

    import httpx

    from core.services.paperclip_admin_client import PaperclipAdminClient
    from routers.teams import agents as agents_mod

    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.read().decode()))
        return httpx.Response(201, json={"id": "a_new"})

    admin = PaperclipAdminClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://pc.test")
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "h"}))
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", AsyncMock())
    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value={"plan": "standard"}))

    r = client.post(
        "/api/v1/teams/agents", json={"name": "Helper", "role": "ceo"}, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200
    assert bodies[0]["permissions"] == {"canCreateAgents": False}


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
                "url": f"wss://bv-dev.{GW_APEX}/api/v1/gateway/ws?token=t&clientId=paperclip-operator",
                "authToken": "SECRET-GATEWAY-TOKEN",
                "devicePrivateKeyPem": "-----BEGIN PRIVATE KEY-----\nLEAKME\n-----END PRIVATE KEY-----\n",
                "sessionKeyStrategy": "issue",
                "sessionKey": "paperclip:u1",
            },
        }
    )
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"hook_token": "hook-tok"}),
    )
    # host_for is pinned to AgentCore fleet-wide (deletion PR-1) regardless of the
    # (missing) container row above, so the post-create wiring goes through the
    # Hermes profile write rather than container pairing. Stub it out so the
    # request completes instead of rolling back on a real S3/Paperclip call.
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", AsyncMock())

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
            "companyId": "co_abc",
            "name": "Helper",
            "role": "engineer",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {
                "url": f"wss://bv-dev.{GW_APEX}/api/v1/gateway/ws",
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
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"hook_token": "hook-tok"}),
    )

    r = client.post(
        "/api/v1/teams/agents",
        json={"name": "Helper", "role": "engineer"},
        headers={"Authorization": "Bearer test"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["upstream_status"] == 403
    # Paperclip's own reason, not the call summary (audit 2026-09-24 F5).
    assert body["detail"] == "Missing permission: agents:create"


def test_patch_agent_forwards_explicit_null_reports_to(client, monkeypatch):
    """Clearing a manager sends {"reportsTo": null}; the BFF must forward the
    null (exclude_unset, not exclude_none) so Paperclip makes the agent a root.
    Paperclip rejects "" (uuid().nullable())."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "co_abc"})
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
    agent = {
        "id": "a1",
        "companyId": "co_abc",
        "name": "Specialist",
        "role": "engineer",
        "reportsTo": None,
    }
    admin.get_agent = AsyncMock(return_value=agent)
    admin.list_agents = AsyncMock(return_value={"agents": [agent]})
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


def test_patch_agent_rejects_moving_final_root_ceo_under_a_manager(client, monkeypatch):
    admin = MagicMock()
    root = {
        "id": "root",
        "companyId": "co_abc",
        "name": "Renamed CEO",
        "role": "ceo",
        "reportsTo": None,
    }
    admin.get_agent = AsyncMock(return_value=root)
    admin.list_agents = AsyncMock(return_value={"agents": [root]})
    admin.patch_agent = AsyncMock(return_value={**root, "reportsTo": "manager"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/agents/root",
        json={"reportsTo": "manager"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 409
    assert r.json()["detail"] == "Lightbulb's coordinator cannot be deleted or demoted."
    admin.patch_agent.assert_not_awaited()


def test_patch_agent_allows_moving_root_ceo_when_another_root_exists(client, monkeypatch):
    admin = MagicMock()
    retiring_root = {
        "id": "old-root",
        "companyId": "co_abc",
        "name": "Ada",
        "role": "ceo",
        "reportsTo": None,
    }
    surviving_root = {
        "id": "new-root",
        "companyId": "co_abc",
        "name": "Bea",
        "role": "ceo",
        "reportsTo": None,
    }
    updated = {**retiring_root, "reportsTo": "new-root"}
    admin.get_agent = AsyncMock(return_value=retiring_root)
    admin.list_agents = AsyncMock(return_value={"agents": [retiring_root, surviving_root]})
    admin.patch_agent = AsyncMock(return_value=updated)
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.patch(
        "/api/v1/teams/agents/old-root",
        json={"reportsTo": "new-root"},
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200
    assert r.json()["reportsTo"] == "new-root"


def test_delete_agent_maps_final_root_conflict_to_409(client, monkeypatch):
    from routers.teams import agents as agents_mod
    from core.services.paperclip_agent_lifecycle import AgentLifecycleConflict

    delete_everywhere = AsyncMock(
        side_effect=AgentLifecycleConflict("Lightbulb's coordinator cannot be deleted or demoted.")
    )
    monkeypatch.setattr(agents_mod, "delete_agent_everywhere", delete_everywhere)
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "x", "companyId": "co_abc"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.delete("/api/v1/teams/agents/friday_id", headers={"Authorization": "Bearer x"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Lightbulb's coordinator cannot be deleted or demoted."
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="friday_id")


def test_delete_agent_allows_non_seeded(client, monkeypatch):
    from routers.teams import agents as agents_mod

    delete_everywhere = AsyncMock(return_value={"deleted": True})
    monkeypatch.setattr(agents_mod, "delete_agent_everywhere", delete_everywhere)
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "x", "companyId": "co_abc"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.delete("/api/v1/teams/agents/a_user", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="a_user")


def test_legacy_runs_alias_proxies_heartbeat_run(client, monkeypatch):
    """/runs/{id} is a legacy alias — it must proxy /api/heartbeat-runs/{id}.

    Upstream never had GET /api/runs/{id}; the frontend RunDetailPanel still
    reads this path as a fallback, so it must resolve, not 404.
    """
    admin = MagicMock()
    admin.get_heartbeat_run = AsyncMock(return_value={"id": "r1", "companyId": "co_abc", "status": "failed"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)

    r = client.get("/api/v1/teams/runs/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["id"] == "r1"
    admin.get_heartbeat_run.assert_awaited_once_with(
        run_id="r1",
        session_cookie="cookie-value",
    )


def test_list_and_get_agent_name_the_front_agent_lightbulb(client, monkeypatch):
    """Dev walk R6: Paperclip calls the front agent "CEO"; under the switch the Team API says "Lightbulb"."""
    from routers.teams import agents as agents_mod
    from core.services import messaging

    _roster(monkeypatch, {"id": "ceo", "name": "CEO"}, {"id": "g", "name": "Growth"})
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "ceo", "name": "CEO", "companyId": "co_abc"})
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value="ceo"))

    names = {a["id"]: a["name"] for a in client.get("/api/v1/teams/agents").json()["agents"]}
    assert names == {"ceo": "Lightbulb", "g": "Growth"}
    assert client.get("/api/v1/teams/agents/ceo").json()["name"] == "Lightbulb"

    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=None))  # switch off: Paperclip's name
    assert client.get("/api/v1/teams/agents/ceo").json()["name"] == "CEO"


def test_list_agents_carries_each_agents_handle_under_the_switch(client, monkeypatch):
    """Dev walk 7 (W7-7): Team › Ask drafted nothing. The page took a specialist's @handle from the open room's
    member rows, and a super-agent room seats only Lightbulb. Each agent now carries the handle rooms use."""
    from routers.teams import agents as agents_mod
    from core.services import messaging

    ceo, growth = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    _roster(monkeypatch, {"id": ceo, "name": "CEO"}, {"id": growth, "name": "Growth"})
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=ceo))
    monkeypatch.setattr(
        messaging,
        "load_agent_roster",
        AsyncMock(return_value=[{"id": ceo, "name": "Lightbulb"}, {"id": growth, "name": "Growth"}]),
    )
    handles = {a["id"]: a.get("mentionHandle") for a in client.get("/api/v1/teams/agents").json()["agents"]}
    assert handles == {ceo: "lightbulb", growth: "growth"}

    monkeypatch.setattr(
        messaging, "load_agent_roster", AsyncMock(side_effect=messaging.ApiProblem("RUNTIME_UNAVAILABLE", "down"))
    )
    assert client.get("/api/v1/teams/agents").status_code == 200  # no handles beats no Team page
