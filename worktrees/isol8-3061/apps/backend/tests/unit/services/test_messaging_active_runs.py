"""Room detail carries the room's in-progress runs.

A client learns a run exists from the live ``run_active`` frame, which is
published once at turn start — open or reload the room a minute later and there
is no "Working" bubble and no Stop/Steer. The room GET is the client's
reconcile-from-HTTP door, so it carries the ledger's answer.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from core.repositories import user_repo
from core.services import messaging

OWNER = "org_1"
ROOM = str(uuid.uuid4())
FORGE = str(uuid.UUID(int=1))
GONE = str(uuid.UUID(int=2))
META = {
    "roomId": ROOM,
    "ownerId": OWNER,
    "kind": "channel",
    "name": "general",
    "revision": 3,
    "lastSeq": 5,
    "updatedAt": "2026-09-16T00:00:00Z",
    "deliveryKey": str(uuid.UUID(int=8)),
    "createdAt": "2026-09-16T00:00:00Z",
}
MEMBERS = [
    {
        "kind": "human",
        "id": "user_a",
        "role": "owner",
        "displayName": "Aden",
        "mentionHandle": None,
        "listenMode": None,
        "joinedSeq": 0,
        "joinedAt": "2026-09-16T00:00:00Z",
    },
    {
        "kind": "agent",
        "id": FORGE,
        "role": "member",
        "displayName": "Forge",
        "mentionHandle": "forge",
        "listenMode": "active",
        "joinedSeq": 0,
        "joinedAt": "2026-09-16T00:00:00Z",
    },
]


def _run(agent_id: str) -> dict:
    return {
        "runId": str(uuid.uuid5(uuid.NAMESPACE_URL, agent_id)),
        "roomId": ROOM,
        "agentId": agent_id,
        "status": "started",
        "triggerMessageId": "trigger",
        "startedAt": "2026-09-16T18:03:00Z",
        "lastHeartbeatAt": "2026-09-16T18:22:00Z",
    }


@pytest.fixture
def open_runs(monkeypatch):
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS))
    monkeypatch.setattr(messaging.messaging_repo, "get_read_cursor", AsyncMock(return_value=0))
    monkeypatch.setattr(user_repo, "get", AsyncMock(return_value=None))
    stub = AsyncMock(return_value=[])
    monkeypatch.setattr(messaging.messaging_repo, "list_open_runs_for_room", stub)
    return stub


@pytest.mark.asyncio
async def test_room_detail_carries_the_rooms_open_runs(open_runs):
    open_runs.return_value = [_run(FORGE)]

    detail = await messaging._room_detail(META, "user_a", MEMBERS[0])

    open_runs.assert_awaited_once_with(OWNER, ROOM)
    assert detail.model_dump(by_alias=True, mode="json")["activeRuns"] == [
        {
            "runId": _run(FORGE)["runId"],
            "agentId": FORGE,
            "triggerMessageId": "trigger",
            "startedAt": "2026-09-16T18:03:00Z",
            "lastHeartbeatAt": "2026-09-16T18:22:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_a_run_whose_agent_left_the_room_is_not_reported(open_runs):
    # Same rule the run-status read applies: an agent that is no longer a member
    # is as over as a run that never existed.
    open_runs.return_value = [_run(GONE)]

    assert (await messaging._room_detail(META, "user_a", MEMBERS[0])).active_runs == []


@pytest.mark.asyncio
async def test_no_open_runs_is_an_empty_list(open_runs):
    assert (await messaging._room_detail(META, "user_a", MEMBERS[0])).active_runs == []
