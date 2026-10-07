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
