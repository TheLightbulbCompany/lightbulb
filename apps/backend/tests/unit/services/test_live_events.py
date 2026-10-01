from unittest.mock import AsyncMock

import pytest

from core.services import live_events
from core.services.messaging import make_live_event

ROOM = "22222222-2222-4222-8222-222222222222"
KEY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_publish_room_uses_the_workspace_scoped_path(monkeypatch):
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    monkeypatch.setattr(
        live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY, "workspaceId": WS})
    )
    assert await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
    assert seen == [f"/ws/{WS}/rooms/{ROOM}/{KEY}"]


@pytest.mark.asyncio
async def test_publish_room_heals_a_room_without_a_workspace_id(monkeypatch):
    """A META row that predates workspaceId (roll-window rows, fixtures) is healed
    lazily rather than refused — see core.repositories.messaging_repo.workspace_id_for_meta."""
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    meta = {"deliveryKey": KEY, "ownerId": "org_1"}
    monkeypatch.setattr(live_events.messaging_repo, "get_room", AsyncMock(return_value=meta))
    heal = AsyncMock(return_value=WS)
    monkeypatch.setattr(live_events.messaging_repo, "workspace_id_for_meta", heal)
    assert await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
    assert seen == [f"/ws/{WS}/rooms/{ROOM}/{KEY}"]
    heal.assert_awaited_once_with(live_events.messaging_repo.room_pk(ROOM), "META", meta)


@pytest.mark.asyncio
async def test_publish_room_frames_uses_the_sibling_frames_path(monkeypatch):
    """AppSync caps a channel at 5 segments, so view frames go on /ws/<ws>/frames/<room>/<key>,
    the room channel with `rooms` swapped for `frames` — never appended to it."""
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    monkeypatch.setattr(
        live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY, "workspaceId": WS})
    )
    event = make_live_event("room.browser.frame", {"roomId": ROOM}, room_id=ROOM)
    assert await publisher.publish_room(ROOM, event, frames=True)
    assert seen == [f"/ws/{WS}/frames/{ROOM}/{KEY}"]


@pytest.mark.asyncio
async def test_post_reuses_one_keep_alive_client(monkeypatch):
    posts = []

    class FakeClient:
        def __init__(self, **kw):
            posts.append(("new", kw["timeout"]))

        async def post(self, endpoint, content, headers):
            posts.append("post")
            return "ok"

    monkeypatch.setattr(live_events, "_http_client", None)
    monkeypatch.setattr(live_events.httpx, "AsyncClient", FakeClient)
    publisher = live_events.LiveEventPublisher()
    assert await publisher._post("https://x", b"{}", {}) == "ok"
    assert await publisher._post("https://x", b"{}", {}) == "ok"
    assert posts == [("new", live_events._TIMEOUT), "post", "post"]


@pytest.mark.asyncio
async def test_user_channel_is_none_without_a_user_stream(monkeypatch):
    monkeypatch.setattr(live_events.messaging_repo, "get_user_stream", AsyncMock(return_value=None))
    assert await live_events.LiveEventPublisher().user_channel("org_a", "user_a") is None
