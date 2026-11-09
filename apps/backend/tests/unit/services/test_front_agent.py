"""Stable existing-worker selection, without changing roster or private-room authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo
from core.services import task_conversation as tc
from tests.unit.super_agent.orchestration.test_tasks_repo import table  # noqa: F401


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
    with pytest.raises(tc.NoFrontAgent, match="no authorized"):
        await tc.front_agent_id("ws", room_id="private")


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


@pytest.mark.parametrize("kind", ["dm", "call", "meeting"])
@pytest.mark.usefixtures("table")
async def test_router_profile_uses_existing_conversation_agent(front, monkeypatch, kind):
    from core import tasks_schema as ts
    from core.temporal import activities
    from core.temporal.types import RouterTurnIn

    front[0]["frontAgentId"] = "worker"
    meta = {"ownerId": "ws", "frontAgentId": "ceo-stable", "kind": kind}
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"id": "ceo-stable", "kind": "agent", "role": "member"}]),
    )
    monkeypatch.setattr(activities, "_cheap_model", AsyncMock(return_value="router-model"))
    trigger = {"ref": {"roomId": "canonical-room"}}
    scope = ts.trigger_scope(trigger)
    identity = await activities._router_identity("ws", RouterTurnIn("ws", "shared", "batch"), scope)
    assert identity.agent_id == "ceo-stable"
    assert identity.room == ts.router_room_key("canonical-room")


@pytest.mark.usefixtures("table")
async def test_generic_batch_and_late_finish_keep_first_agent_and_model(front, monkeypatch):
    from core import tasks_schema as ts
    from core.temporal import activities
    from core.temporal.types import RouterTurnIn

    model = AsyncMock(return_value="first-model")
    monkeypatch.setattr(activities, "_cheap_model", model)
    room_lookup = AsyncMock(side_effect=AssertionError("an outside scope is not a room"))
    monkeypatch.setattr(messaging_repo, "get_room", room_lookup)
    scope = ts.trigger_scope({"source": "mail", "triggerId": "mail-1", "ref": {"from": "sender"}})
    assert scope.startswith("r:")
    inp = RouterTurnIn("ws", "shared", "outside-batch")
    initial = await activities._router_identity("ws", inp, scope)
    front[0]["frontAgentId"] = "worker"
    front[1][0]["status"] = "deleted"
    model.return_value = "new-config-model"
    assert await activities._router_identity("ws", inp, scope) == initial
    poll = AsyncMock(return_value=SimpleNamespace(status="completed"))
    monkeypatch.setattr(activities, "_poll_run", poll)
    monkeypatch.setattr(activities, "_meter_router", AsyncMock())
    await activities._finish_router_run("ws", inp, scope, None, 0)
    assert poll.await_args.args[1] == initial
    assert initial.agent_id == "ceo-stable" and initial.model == "first-model"
    model.assert_awaited_once()
    room_lookup.assert_not_awaited()


@pytest.mark.usefixtures("table")
async def test_router_identity_claim_is_atomic_first_writer_wins():
    import asyncio
    from core.repositories import tasks_repo

    async def claim(agent, model):
        return await tasks_repo.claim_router_identity(
            "ws", "race", agent_id=agent, lane="shared", scope="r:outside", model=model, call=False
        )

    first, second = await asyncio.gather(claim("one", "model-one"), claim("two", "model-two"))
    assert first == second == await tasks_repo.get_router_identity("ws", "race")
    assert (first["agentId"], first["model"]) in {("one", "model-one"), ("two", "model-two")}
    assert not {"prompt", "grant", "token"} & first.keys()


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize(
    "field,value", [("ownerId", "other"), ("lane", "other"), ("scope", "other"), ("runId", "other"), ("call", True)]
)
async def test_router_identity_rejects_changed_binding(front, monkeypatch, field, value):
    from core.repositories import tasks_repo
    from core.temporal import activities
    from core.temporal.types import RouterTurnIn

    row = await tasks_repo.claim_router_identity(
        "ws", "batch", agent_id="ceo-stable", lane="shared", scope="r:outside", model="old", call=False
    )
    row[field] = value
    tasks_repo._table().put_item(Item=row)
    with pytest.raises(RuntimeError, match="authorized scope"):
        await activities._router_identity("ws", RouterTurnIn("ws", "shared", "batch"), "r:outside")


async def test_unspecialized_existing_task_uses_its_seated_agent(front, monkeypatch):
    from core.temporal import activities

    front[0]["frontAgentId"] = "worker"
    monkeypatch.setattr(
        messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "ws", "frontAgentId": "ceo-stable"})
    )
    monkeypatch.setattr(
        messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"id": "ceo-stable", "kind": "agent", "role": "member"}]),
    )
    assert await activities._agent_for("ws", {"taskId": "task", "specialist": None}) == "ceo-stable"


async def test_persisted_attempt_assignment_survives_global_front_unavailability(front, monkeypatch):
    from core.temporal import activities

    monkeypatch.setattr(tc, "front_agent_id", AsyncMock(side_effect=tc.NoFrontAgent("unavailable")))
    assert await activities._agent_for("ws", {"taskId": "task"}, prior_agent_id="original-worker") == "original-worker"
    tc.front_agent_id.assert_not_awaited()


def test_trigger_scope_distinguishes_room_ids_from_outside_conversations():
    from core import tasks_schema as ts

    room = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert ts.trigger_scope({"ref": {"roomId": room}}) == room
    assert ts.trigger_scope({"ref": {"deliverRoomId": room}}) == room
    outside = ts.trigger_scope({"source": "mail", "triggerId": "mail-1", "ref": {"sessionKey": room}})
    assert outside.startswith("r:") and outside != room
