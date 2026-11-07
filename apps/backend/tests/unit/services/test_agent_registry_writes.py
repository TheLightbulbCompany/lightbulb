"""Phase 1 task 8b.1: agent create/rename/delete/pause are registry writes; no door calls Paperclip."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from core import dynamodb
from core.auth import AuthContext, get_current_user
from core.services import agent_registry
from tests.unit.super_agent.registry.conftest import _create_tasks_table

WS = "org_1"
H = {"Authorization": "Bearer test"}


@pytest.fixture
def ddb(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        _create_tasks_table(resource)
        yield


@pytest.fixture
def no_paperclip(monkeypatch):
    from core.services.paperclip_admin_client import PaperclipAdminClient

    def boom(*a, **k):
        raise AssertionError("Paperclip called")

    monkeypatch.setattr(PaperclipAdminClient, "__init__", boom)


@pytest.fixture
def side_effects(monkeypatch):
    """Everything an agent write touches besides the registry: the S3 profile, facts, network, META."""
    from core.services import hermes_profiles, hermes_provisioning, network_member, owner_facts, voice_calls
    from routers.teams import agents as agents_mod

    fx = SimpleNamespace(
        write_profile=AsyncMock(),
        refresh_soul=AsyncMock(),
        delete_profile=MagicMock(),
        meta=MagicMock(remove_starter_agent_id=AsyncMock(), resolve_owner_row=AsyncMock(return_value=None)),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profile_for_agent", fx.write_profile)
    monkeypatch.setattr(hermes_provisioning, "refresh_agent_soul", fx.refresh_soul)
    monkeypatch.setattr(hermes_profiles, "delete_profile", fx.delete_profile)
    monkeypatch.setattr(hermes_profiles, "read_voice_notes", MagicMock(return_value=""))
    monkeypatch.setattr(hermes_profiles, "upload_voice_notes", MagicMock())
    monkeypatch.setattr(voice_calls, "send_note_saved", AsyncMock())
    monkeypatch.setattr(owner_facts, "write_owner_facts", AsyncMock())
    monkeypatch.setattr(network_member, "publish_sessions", AsyncMock())
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo", MagicMock(return_value=fx.meta))
    monkeypatch.setattr(agents_mod, "_repo", lambda: fx.meta)
    monkeypatch.setattr(agents_mod, "get_seeded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(agents_mod, "get_starter_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(agents_mod, "_load_agent_sidecars", AsyncMock(return_value={}))
    monkeypatch.setattr(agents_mod, "_front_named", AsyncMock(side_effect=lambda owner, agents: agents))
    return fx


@pytest.fixture
def client(ddb, no_paperclip, side_effects):
    from main import app

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_1", org_id=WS)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _live(ws=WS):
    return await agent_registry.roster(ws)


# --- agent_registry writes ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mints_uuid_and_lists(ddb):
    made = await agent_registry.create(WS, name="Ana")
    (row,) = await agent_registry.roster(WS)
    assert row["name"] == "Ana" and len(row["id"]) == 36 and row == made
    assert {"id", "name", "role", "title", "status", "reportsTo", "capabilities", "metadata", "companyId"} <= set(row)


@pytest.mark.asyncio
async def test_create_replay_of_same_id_returns_existing(ddb):
    first = await agent_registry.create(WS, name="Ana", agent_id="a1")
    assert await agent_registry.create(WS, name="Other", agent_id="a1") == first


@pytest.mark.asyncio
async def test_update_renames(ddb):
    agent = await agent_registry.create(WS, name="Ana")
    assert (await agent_registry.update(WS, agent["id"], {"name": "Bo"}))["name"] == "Bo"
    assert (await agent_registry.get(WS, agent["id"]))["name"] == "Bo"


@pytest.mark.asyncio
async def test_update_missing_or_deleted_is_key_error_and_bad_field_refused(ddb):
    with pytest.raises(KeyError):
        await agent_registry.update(WS, "nope", {"name": "x"})
    agent = await agent_registry.create(WS, name="Ana")
    with pytest.raises(ValueError):
        await agent_registry.update(WS, agent["id"], {"companyId": "co"})
    await agent_registry.delete(WS, agent["id"])
    with pytest.raises(KeyError):
        await agent_registry.update(WS, agent["id"], {"name": "x"})


@pytest.mark.asyncio
async def test_delete_hides_from_roster(ddb):
    agent = await agent_registry.create(WS, name="Ana")
    await agent_registry.delete(WS, agent["id"])
    await agent_registry.delete(WS, agent["id"])  # idempotent
    assert await agent_registry.roster(WS) == []


# --- /teams/agents -----------------------------------------------------------------------------------


def test_teams_create_rolls_back_registry_when_profile_write_fails(client, side_effects):
    side_effects.write_profile.side_effect = RuntimeError("s3 down")
    r = client.post("/api/v1/teams/agents", json={"name": "Ana", "role": "general"}, headers=H)
    assert r.status_code == 502
    import asyncio

    assert asyncio.run(_live()) == []


def test_teams_delete_last_root_is_409(client):
    import asyncio

    ceo = asyncio.run(agent_registry.create(WS, name="Lightbulb", role="ceo"))
    assert client.delete(f"/api/v1/teams/agents/{ceo['id']}", headers=H).status_code == 409
    assert [a["id"] for a in asyncio.run(_live())] == [ceo["id"]]


def test_teams_routes_never_call_paperclip(client, side_effects):
    import asyncio

    asyncio.run(agent_registry.create(WS, name="Lightbulb", role="ceo"))
    r = client.post("/api/v1/teams/agents", json={"name": "Ana", "role": "general"}, headers=H)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    side_effects.write_profile.assert_awaited_once()
    base = f"/api/v1/teams/agents/{aid}"
    assert client.get("/api/v1/teams/agents", headers=H).status_code == 200
    assert client.get(base, headers=H).json()["name"] == "Ana"
    assert client.patch(base, json={"name": "Bo", "capabilities": "Writes"}, headers=H).json()["name"] == "Bo"
    assert (asyncio.run(agent_registry.get(WS, aid)))["persona"] == "Writes"
    side_effects.refresh_soul.assert_awaited_once()
    assert client.post(f"{base}/pause", headers=H).json()["status"] == "paused"
    assert client.post(f"{base}/resume", headers=H).json()["status"] == "idle"
    assert client.post(f"{base}/reset-session", json={}, headers=H).json()["id"] == aid
    assert client.post(f"{base}/clear-error", headers=H).json()["id"] == aid
    assert client.get(f"{base}/voice-notes", headers=H).json()["name"] == "Bo"
    assert client.put(f"{base}/voice-notes", json={"notes": "hi"}, headers=H).json()["notes"] == "hi"
    assert client.delete(base, headers=H).json()["deleted"] is True
    assert client.get(base, headers=H).status_code == 404
    assert [a["name"] for a in asyncio.run(_live())] == ["Lightbulb"]


def test_teams_agent_of_another_workspace_is_404(client):
    import asyncio

    other = asyncio.run(agent_registry.create("org_2", name="Theirs"))
    assert client.get(f"/api/v1/teams/agents/{other['id']}", headers=H).status_code == 404


# --- runtime_rpc agents.* and the voice rename ---------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_rpc_agents_create_update_delete_native(ddb, no_paperclip, side_effects, monkeypatch):
    from core.services import runtime_rpc as rpc

    monkeypatch.setattr(rpc, "_invalidate", AsyncMock())
    created = await rpc._create_agent(WS, "user_1", {"name": "Ana", "params": {"paperclip": {"title": "Writer"}}}, {})
    aid = created["agentId"]
    assert created["agent"]["title"] == "Writer"
    await rpc._mirror_agent_update(WS, {"agentId": aid, "name": "Bo"}, agentcore=True, by="user_1")
    assert (await agent_registry.get(WS, aid))["name"] == "Bo"
    side_effects.refresh_soul.assert_awaited_once()
    await rpc.execute_sync(
        owner_id=WS, user_id="user_1", request_id="r", method="agents.delete", params={"agentId": aid}
    )
    assert await agent_registry.roster(WS) == []


@pytest.mark.asyncio
async def test_runtime_rpc_create_rolls_back_when_profile_write_fails(ddb, side_effects, monkeypatch):
    from core.services import runtime_rpc as rpc

    side_effects.write_profile.side_effect = RuntimeError("s3 down")
    with pytest.raises(RuntimeError):
        await rpc._create_agent(WS, "user_1", {"name": "Ana"}, {})
    assert await agent_registry.roster(WS) == []


@pytest.mark.asyncio
async def test_voice_profile_rename_updates_registry(ddb, no_paperclip, side_effects, monkeypatch):
    from core.repositories import messaging_repo
    from core.services import voice_calls

    agent = await agent_registry.create(WS, name="Ana")
    monkeypatch.setattr(
        messaging_repo,
        "get_voice_run",
        AsyncMock(return_value={"agentId": agent["id"], "roomId": "room_1", "speakerUserId": "user_1"}),
    )
    monkeypatch.setattr(messaging_repo, "get_member", AsyncMock(return_value={"home": WS}))
    monkeypatch.setattr(messaging_repo, "member_home", lambda member, owner: WS)
    monkeypatch.setattr(messaging_repo, "count_voice_edit", AsyncMock(return_value=True))
    out = await voice_calls.edit_voice_profile(
        WS, agent_id=agent["id"], run_id="run_1", action="rename", name="Bo", text=None
    )
    assert out["name"] == "Bo"
    assert (await agent_registry.get(WS, agent["id"]))["name"] == "Bo"


@pytest.mark.asyncio
async def test_delete_keeps_the_row_when_the_profile_delete_fails(ddb, side_effects):
    """The profile goes first: a failed S3 delete leaves the agent visible so a retry can finish."""
    from core.services.agent_lifecycle import delete_agent_everywhere

    agent = await agent_registry.create(WS, name="Ana")
    side_effects.delete_profile.side_effect = RuntimeError("s3 down")
    with pytest.raises(RuntimeError):
        await delete_agent_everywhere(owner_id=WS, agent_id=agent["id"])
    assert [a["id"] for a in await agent_registry.roster(WS)] == [agent["id"]]
