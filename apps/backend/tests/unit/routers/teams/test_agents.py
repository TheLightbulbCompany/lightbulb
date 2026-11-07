"""Tests for the Teams Agents BFF: registry-backed routes; client-supplied adapter fields are 422."""

from unittest.mock import AsyncMock

import pytest
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


def _update(monkeypatch):
    from core.services import agent_registry

    update = AsyncMock(side_effect=lambda ws, aid, fields: {"id": aid, "status": "active", **fields})
    monkeypatch.setattr(agent_registry, "update", update)
    return update


def test_patch_agent_writes_explicit_null_reports_to(client, monkeypatch):
    """Clearing a manager sends {"reportsTo": null}; the null is written (the agent becomes a root)."""
    _roster(monkeypatch, {"id": "a1", "role": "engineer"})
    update = _update(monkeypatch)
    r = client.patch("/api/v1/teams/agents/a1", json={"reportsTo": None}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    update.assert_awaited_once_with("o1", "a1", {"reportsTo": None})


def test_patch_agent_drops_null_for_non_reports_to_fields(client, monkeypatch):
    _roster(monkeypatch, {"id": "a1", "role": "engineer"})
    update = _update(monkeypatch)
    r = client.patch(
        "/api/v1/teams/agents/a1", json={"title": None, "reportsTo": "mgr1"}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 200
    update.assert_awaited_once_with("o1", "a1", {"reportsTo": "mgr1"})


def test_patch_agent_rejects_moving_final_root_ceo_under_a_manager(client, monkeypatch):
    _roster(monkeypatch, {"id": "root", "name": "Renamed CEO", "role": "ceo", "reportsTo": None})
    update = _update(monkeypatch)
    r = client.patch("/api/v1/teams/agents/root", json={"reportsTo": "manager"}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Lightbulb's coordinator cannot be deleted or demoted."
    update.assert_not_awaited()


def test_patch_agent_allows_moving_root_ceo_when_another_root_exists(client, monkeypatch):
    _roster(
        monkeypatch,
        {"id": "old-root", "role": "ceo", "reportsTo": None},
        {"id": "new-root", "role": "ceo", "reportsTo": None},
    )
    _update(monkeypatch)
    r = client.patch(
        "/api/v1/teams/agents/old-root", json={"reportsTo": "new-root"}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 200
    assert r.json()["reportsTo"] == "new-root"


def test_delete_agent_maps_final_root_conflict_to_409(client, monkeypatch):
    from routers.teams import agents as agents_mod
    from core.services.agent_lifecycle import AgentLifecycleConflict

    _roster(monkeypatch, {"id": "friday_id"})
    delete_everywhere = AsyncMock(
        side_effect=AgentLifecycleConflict("Lightbulb's coordinator cannot be deleted or demoted.")
    )
    monkeypatch.setattr(agents_mod, "delete_agent_everywhere", delete_everywhere)
    r = client.delete("/api/v1/teams/agents/friday_id", headers={"Authorization": "Bearer x"})
    assert r.status_code == 409
    delete_everywhere.assert_awaited_once_with(owner_id="o1", agent_id="friday_id")


def test_delete_unknown_agent_is_404(client, monkeypatch):
    _roster(monkeypatch, {"id": "a1"})
    assert client.delete("/api/v1/teams/agents/nope", headers={"Authorization": "Bearer x"}).status_code == 404


def test_list_and_get_agent_name_the_front_agent_lightbulb(client, monkeypatch):
    """Dev walk R6: Paperclip calls the front agent "CEO"; under the switch the Team API says "Lightbulb"."""
    from routers.teams import agents as agents_mod
    from core.services import messaging

    _roster(monkeypatch, {"id": "ceo", "name": "CEO"}, {"id": "g", "name": "Growth"})
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
