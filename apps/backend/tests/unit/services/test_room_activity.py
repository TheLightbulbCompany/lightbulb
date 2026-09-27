from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from core.services import live_events

ROOM = "22222222-2222-4222-8222-222222222222"
OWNER = "org_room"
HUMAN = {"kind": "human", "id": "user_a", "displayName": "Ann"}


def _members():
    return [
        {"kind": "human", "id": "user_a"},
        {"kind": "human", "id": "user_b"},
        {"kind": "human", "id": "user_vendor", "homeOwnerId": "org_vendor"},
        {"kind": "agent", "id": "ceo"},
    ]


@pytest.fixture
def wired(monkeypatch):
    publisher = live_events.LiveEventPublisher()
    calls = []
    monkeypatch.setattr(
        publisher,
        "publish_user",
        AsyncMock(side_effect=lambda owner, user, event: calls.append((owner, user, event)) or True),
    )
    monkeypatch.setattr(live_events, "get_live_event_publisher", lambda: publisher)
    monkeypatch.setattr(
        live_events.messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": OWNER})
    )
    monkeypatch.setattr(live_events.messaging_repo, "list_members", AsyncMock(return_value=_members()))
    return calls


@pytest.mark.asyncio
async def test_typing_reaches_every_other_human_in_their_home_workspace(wired):
    expires = datetime(2026, 9, 26, 12, 0, 3, tzinfo=UTC)
    await live_events.publish_room_activity(ROOM, HUMAN, "typing", expires, skip_user_id="user_a")
    assert [(owner, user) for owner, user, _ in wired] == [(OWNER, "user_b"), ("org_vendor", "user_vendor")]
    event = wired[0][2]
    assert event["type"] == "room.activity.changed"
    assert event["roomId"] == ROOM
    assert event["payload"] == {"roomId": ROOM, "actor": HUMAN, "state": "typing", "expiresAt": expires.isoformat()}


@pytest.mark.asyncio
async def test_working_and_idle_carry_no_expiry(wired):
    agent = {"kind": "agent", "id": "ceo", "displayName": "CEO"}
    await live_events.publish_room_activity(ROOM, agent, "working")
    assert len(wired) == 3
    assert "expiresAt" not in wired[0][2]["payload"]


@pytest.mark.asyncio
async def test_a_deleted_or_missing_room_publishes_nothing(wired, monkeypatch):
    monkeypatch.setattr(live_events.messaging_repo, "get_room", AsyncMock(return_value=None))
    await live_events.publish_room_activity(ROOM, HUMAN, "typing", datetime.now(UTC) + timedelta(seconds=3))
    assert wired == []


@pytest.mark.asyncio
async def test_one_failing_member_does_not_stop_the_rest(wired, monkeypatch):
    publisher = live_events.get_live_event_publisher()
    seen = []

    async def flaky(owner, user, event):
        if user == "user_b":
            raise live_events.LivePublishError("boom")
        seen.append(user)
        return True

    monkeypatch.setattr(publisher, "publish_user", flaky)
    await live_events.publish_room_activity(ROOM, HUMAN, "idle", skip_user_id="user_a")
    assert seen == ["user_vendor"]
