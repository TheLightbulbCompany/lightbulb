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


async def _type(is_typing: bool, *, publish_room=None, activity=None, prewake=None, meta=None):
    router._typing_last.clear()
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
async def test_typing_never_wakes_the_runtime():
    """Phase 2 (8 Nov 2026): only a real call, meeting or ask wakes a workspace session."""
    prewake = MagicMock()
    await _type(True, prewake=prewake)
    prewake.assert_not_called()
