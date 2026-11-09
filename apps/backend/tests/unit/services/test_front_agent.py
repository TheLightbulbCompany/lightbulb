"""Stable existing-worker selection, without changing roster or private-room authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo
from core.services import task_conversation as tc


@pytest.fixture
def front(monkeypatch):
    roster = [
        {"id": "ceo-stable", "name": "Founder's custom name", "role": "ceo", "status": "active"},
        {"id": "other-ceo", "name": "CEO", "role": "ceo", "status": "active"},
        {"id": "worker", "name": "Friday", "role": "engineer", "status": "active"},
    ]
    config = {}
    company = SimpleNamespace(starter_agent_ids={"ceo": "ceo-stable"})
    monkeypatch.setattr(messaging_repo, "get_voice_agent", AsyncMock(side_effect=lambda ws: config))
    monkeypatch.setattr(tc.agent_registry, "roster", AsyncMock(side_effect=lambda ws: roster))
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row", AsyncMock(return_value=company)
    )
    return config, roster


async def test_stable_starter_uuid_beats_display_name_and_duplicate_ceo_role(front):
    assert await tc.front_agent_id("ws") == "ceo-stable"
    front[1][0]["name"] = "Renamed again"
    assert await tc.front_agent_id("ws") == "ceo-stable"


async def test_any_existing_worker_can_be_selected_without_roster_mutation(front):
    front[0]["frontAgentId"] = "worker"
    before = [dict(a) for a in front[1]]
    assert await tc.front_agent_id("ws") == "worker"
    assert front[1] == before


@pytest.mark.parametrize("status", ["paused", "deleted"])
async def test_unavailable_explicit_selection_does_not_silently_pick_ceo(front, status):
    front[0]["frontAgentId"] = "worker"
    front[1][2]["status"] = status
    with pytest.raises(tc.NoFrontAgent, match="Choose an active agent"):
        await tc.front_agent_id("ws")


async def test_unknown_explicit_selection_does_not_retarget(front):
    front[0]["frontAgentId"] = "other-workspace-agent"
    with pytest.raises(tc.NoFrontAgent):
        await tc.front_agent_id("ws")


async def test_existing_room_preserves_its_ordinary_active_member(front, monkeypatch):
    front[0]["frontAgentId"] = "worker"
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws"}))
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"id": "ceo-stable", "kind": "agent", "mentionHandle": "lightbulb", "role": "member"}]),
    )
    assert await tc.front_agent_id("ws", room_id="private") == "ceo-stable"
    assert await tc.front_agent_id("ws") == "worker"


async def test_foreign_room_cannot_select_an_agent(front, monkeypatch):
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "other"}))
    with pytest.raises(tc.NoFrontAgent):
        await tc.front_agent_id("ws", room_id="private")


async def test_multiple_front_seats_fail_instead_of_picking_one(front, monkeypatch):
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws"}))
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(
            return_value=[
                {"id": aid, "kind": "agent", "mentionHandle": "lightbulb", "role": "member"}
                for aid in ["worker", "ceo-stable"]
            ]
        ),
    )
    with pytest.raises(tc.NoFrontAgent, match="conflicting"):
        await tc.front_agent_id("ws", room_id="private")


async def test_legacy_founder_root_keeps_its_existing_identity(front, monkeypatch):
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row",
        AsyncMock(return_value=SimpleNamespace(starter_agent_ids=None)),
    )
    front[1][1]["reportsTo"] = "ceo-stable"
    assert await tc.front_agent_id("ws") == "ceo-stable"


async def test_foreign_member_handle_cannot_claim_front_identity(front, monkeypatch):
    front[0]["frontAgentId"] = "worker"
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws"}))
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(
            return_value=[
                {
                    "id": "foreign",
                    "kind": "agent",
                    "role": "member",
                    "mentionHandle": "lightbulb",
                    "homeOwnerId": "other",
                }
            ]
        ),
    )
    assert await tc.front_agent_id("ws", room_id="private") == "worker"


async def test_global_selection_does_not_grant_access_to_existing_private_room(front, monkeypatch):
    front[0]["frontAgentId"] = "worker"
    meta = {"ownerId": "ws", "roomId": "private"}
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"id": "ceo-stable", "kind": "agent", "role": "member", "mentionHandle": "lightbulb"}]),
    )
    await tc.messaging._require_lightbulb_voice("ws", meta, "ceo-stable")
    with pytest.raises(tc.messaging.ApiProblem):
        await tc.messaging._require_lightbulb_voice("ws", meta, "worker")


async def test_pinned_uuid_is_authoritative_after_a_handle_change(front, monkeypatch):
    front[0]["frontAgentId"] = "worker"
    monkeypatch.setattr(
        messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws", "frontAgentId": "ceo-stable"})
    )
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(
            return_value=[{"id": "ceo-stable", "kind": "agent", "role": "member", "mentionHandle": "edited-handle"}]
        ),
    )
    assert await tc.front_agent_id("ws", room_id="private") == "ceo-stable"


async def test_pinned_uuid_needs_ordinary_room_membership(front, monkeypatch):
    monkeypatch.setattr(
        messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws", "frontAgentId": "ceo-stable"})
    )
    monkeypatch.setattr(messaging_repo, "list_members", AsyncMock(return_value=[]))
    with pytest.raises(tc.NoFrontAgent, match="no longer a member"):
        await tc.front_agent_id("ws", room_id="private")
