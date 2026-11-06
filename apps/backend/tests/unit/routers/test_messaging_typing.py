"""The typing route also puts a compact activity hint on the room members'
user channels, so a Chats list can say "Ann is typing…"."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import TypingRequest
from routers import messaging as router

ROOM = "22222222-2222-4222-8222-222222222222"


def _auth(user_id: str):
    auth = MagicMock()
    auth.org_id = "org_1"
    auth.user_id = user_id
    return auth


MEMBERS = [
    {"kind": "human", "id": "user_a"},
    {"kind": "agent", "id": "ceo", "displayName": "CEO", "listenMode": "active"},
    {"kind": "agent", "id": "eng", "displayName": "Eng", "listenMode": "mentions"},
    {"kind": "agent", "id": "obs", "displayName": "Obs", "listenMode": "observe"},
]


async def _type(is_typing: bool, *, publish_room=None, activity=None, prewake=None, meta=None, clear=True):
    router._typing_last.clear()
    if clear:
        router._typing_prewaked.clear()
    publisher = MagicMock()
    publisher.publish_room = publish_room or AsyncMock(return_value=True)
    activity = activity or AsyncMock()
    with (
        patch("routers.messaging.resolve_owner_id", return_value="org_1"),
        patch.object(
            router.service, "require_room_member", AsyncMock(return_value=(meta or {"ownerId": "org_host"}, {}))
        ),
        patch.object(router.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(router.voice_calls, "prewake_owner", prewake or MagicMock()),
        patch.object(router.service, "human_display_name", AsyncMock(return_value="Ann")),
        patch("routers.messaging.get_live_event_publisher", return_value=publisher),
        patch("routers.messaging.publish_room_activity", activity),
    ):
        response = await router.typing(
            TypingRequest(room_id=uuid.UUID(ROOM), is_typing=is_typing), auth=_auth("user_a")
        )
    return response, activity


@pytest.mark.asyncio
async def test_typing_also_hints_the_members_user_channels():
    response, activity = await _type(True)
    assert response.status_code == 204
    args, kwargs = activity.await_args
    assert args[0] == ROOM
    assert args[1] == {"kind": "human", "id": "user_a", "displayName": "Ann"}
    assert args[2] == "typing"
    assert args[3] is not None
    assert kwargs["skip_user_id"] == "user_a"


@pytest.mark.asyncio
async def test_stopping_typing_hints_idle_without_expiry():
    _, activity = await _type(False)
    args, _ = activity.await_args
    assert args[2] == "idle" and args[3] is None


@pytest.mark.asyncio
async def test_a_failing_hint_never_fails_the_typing_call():
    response, _ = await _type(True, activity=AsyncMock(side_effect=RuntimeError("boom")))
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_typing_wakes_the_room_owners_runtime_for_agents_that_can_answer():
    prewake = MagicMock()
    await _type(True, prewake=prewake)
    # The ROOM's owner (a shared-channel guest types toward the host's agents); observe never runs;
    # the room id lets the bridge compact that room's session while the person types.
    prewake.assert_called_once_with("org_host", [{"id": "ceo", "name": "CEO"}, {"id": "eng", "name": "Eng"}], ROOM)


@pytest.mark.asyncio
async def test_typing_wakes_once_per_room_per_window():
    prewake = MagicMock()
    await _type(True, prewake=prewake)
    await _type(True, prewake=prewake, clear=False)
    assert prewake.call_count == 1


@pytest.mark.asyncio
async def test_stopping_typing_or_an_archived_room_wakes_nothing():
    prewake = MagicMock()
    await _type(False, prewake=prewake)
    await _type(True, prewake=prewake, meta={"ownerId": "org_host", "archivedAt": "2026-10-01"})
    prewake.assert_not_called()


@pytest.mark.asyncio
async def test_creating_a_room_with_agents_wakes_the_runtime_for_its_first_message():
    """Dev walk 16: the first answer in a new project took 54 s, 39 s of it the runtime's cold start."""
    member = lambda kind, mid, mode: MagicMock(kind=kind, id=mid, display_name=mid.upper(), listen_mode=mode)  # noqa: E731
    detail = MagicMock(
        room_id=uuid.UUID(ROOM),
        members=[member("human", "user_a", None), member("agent", "ceo", "active"), member("agent", "obs", "observe")],
    )
    prewake = MagicMock()
    with (
        patch("routers.messaging.resolve_owner_id", return_value="org_1"),
        patch.object(router.service, "create_room", AsyncMock(return_value=detail)),
        patch.object(router.voice_calls, "prewake_owner", prewake),
    ):
        assert await router.create_room(MagicMock(kind="channel"), caller=(_auth("user_a"), None)) is detail
        prewake.assert_called_once_with("org_1", [{"id": "ceo", "name": "CEO"}], ROOM)
        detail.members = [member("human", "user_a", None)]
        await router.create_room(MagicMock(kind="channel"), caller=(_auth("user_a"), None))
        prewake.assert_called_once()  # no agent to answer: nothing to wake
