from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import ApiProblem, SteerRunRequest
from routers.messaging import list_room_subagents, steer_subagent, stop_subagent, tail_subagent

ROOM, CEO = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


ROUTES = [
    lambda: list_room_subagents(ROOM, auth=_auth()),
    lambda: tail_subagent(ROOM, CEO, "sa-1", auth=_auth()),
    lambda: steer_subagent(ROOM, CEO, "sa-1", SteerRunRequest(text="go"), auth=_auth()),
    lambda: stop_subagent(ROOM, CEO, "sa-1", auth=_auth()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ROUTES)
async def test_non_member_is_refused_on_every_route(call):
    denied = ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    with (
        patch("routers.messaging.service.require_room_member", AsyncMock(side_effect=denied)),
        patch("routers.messaging.subagents.container_rpc.call", AsyncMock()) as rpc,
    ):
        with pytest.raises(ApiProblem):
            await call()
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_tail_of_another_agents_subagent_is_404():
    with (
        patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, {}))),
        patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ApiProblem) as exc:
            await tail_subagent(ROOM, CEO, "sa-1", auth=_auth())
    assert exc.value.error.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_steer_passes_the_chat_gate_and_marks_interactive():
    with (
        patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, {}))),
        patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value={"id": CEO})),
        patch("routers.messaging.chat_gate.gate_chat", AsyncMock(return_value={"blocked": False})) as gate,
        patch("routers.messaging.container_repo.touch_last_interactive", AsyncMock()) as touch,
        patch("routers.messaging.subagents.steer", AsyncMock(return_value={"state": "queued"})),
    ):
        assert await steer_subagent(ROOM, CEO, "sa-1", SteerRunRequest(text="go"), auth=_auth()) == {"state": "queued"}
    gate.assert_awaited_once()
    touch.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_workspace_cannot_stop():
    with (
        patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, {}))),
        patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value={"id": CEO})),
        patch(
            "routers.messaging.chat_gate.gate_chat", AsyncMock(return_value={"blocked": True, "message": "Trial ended"})
        ),
        patch("routers.messaging.subagents.stop", AsyncMock()) as stop,
    ):
        with pytest.raises(ApiProblem):
            await stop_subagent(ROOM, CEO, "sa-1", auth=_auth())
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_in_a_shared_room_reaches_the_room_owners_runtime():
    """Caller is org_1; the room belongs to org_vendor. Every runtime touch goes to org_vendor."""
    with (
        patch(
            "routers.messaging.service.require_room_member",
            AsyncMock(return_value=({"ownerId": "org_vendor"}, {})),
        ) as member,
        patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value={"id": CEO})),
        patch("routers.messaging.chat_gate.gate_chat", AsyncMock(return_value={"blocked": False})) as gate,
        patch("routers.messaging.container_repo.touch_last_interactive", AsyncMock()) as touch,
        patch("routers.messaging.subagents._warm", AsyncMock(return_value=True)),
        patch("routers.messaging.subagents.container_rpc.call", AsyncMock(return_value={"found": True})) as rpc,
    ):
        assert await stop_subagent(ROOM, CEO, "sa-1", auth=_auth()) == {"state": "stopping"}
    assert member.await_args.args[0] == "org_1"
    assert gate.await_args.kwargs["owner_id"] == "org_vendor"
    touch.assert_awaited_once_with("org_vendor")
    assert rpc.await_args.args[0] == "org_vendor"
