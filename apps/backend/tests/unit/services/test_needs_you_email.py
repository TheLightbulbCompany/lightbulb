"""T15 away email: seen/away/quiet-hours/per-room rules, the copy, and the one event."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import settings
from core.services import needs_you_email as away

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 11:00 in New York
EPOCH = int(NOW.timestamp())
ROW = {
    "pk": "AWAY#user_b",
    "sk": "M#room1#m1",
    "sweep": "away",
    "kind": "dm",
    "userId": "user_b",
    "ownerId": "org_1",
    "roomId": "room1",
    "messageId": "m1",
    "seq": 7,
    "createdAt": "2026-10-14T14:54:00+00:00",
}
MESSAGE = {"author": {"kind": "human", "displayName": "Alice"}, "text": "can you look at the deck?"}
USER = {"user_id": "user_b", "email": "b+clerk_test@x.co", "timezone": "America/New_York"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "k")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example")
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api.example")


@pytest.fixture
def world(monkeypatch):
    repo = away.messaging_repo
    w = MagicMock()
    w.cursor = AsyncMock(return_value=6)
    w.finish = AsyncMock()
    w.defer = AsyncMock()
    w.send = AsyncMock(return_value="ses-1")
    w.capture = MagicMock()
    w.slot = AsyncMock(return_value=True)
    w.user = dict(USER)
    monkeypatch.setattr(repo, "get_read_cursor", w.cursor)
    monkeypatch.setattr(repo, "get_message_by_id", AsyncMock(return_value=MESSAGE))
    w.room = AsyncMock(return_value={"kind": "dm"})
    monkeypatch.setattr(repo, "get_room", w.room)
    w.member = AsyncMock(return_value={"id": "user_b"})
    monkeypatch.setattr(repo, "get_member", w.member)
    monkeypatch.setattr(repo, "finish_away_check", w.finish)
    monkeypatch.setattr(repo, "defer_away_check", w.defer)
    monkeypatch.setattr(away.user_repo, "get", AsyncMock(side_effect=lambda _id: w.user))
    monkeypatch.setattr(away.email_service, "send_email", w.send)
    monkeypatch.setattr(away, "capture_product_event", w.capture)
    monkeypatch.setattr(away, "_claim_room_slot", w.slot)
    monkeypatch.setattr(away, "put_metric", lambda *a, **k: None)
    return w


@pytest.mark.asyncio
async def test_an_unread_dm_while_away_is_emailed_once_with_the_event(world):
    assert await away.check_one(ROW, NOW) is True
    kw = world.send.await_args.kwargs
    # A notification of where something waits, never the message itself (owner ruling 2026-10-19).
    assert kw["subject"] == "You have unread messages in Lightbulb"
    assert "New messages are waiting for you in your DM with Alice." in kw["text"]
    assert "can you look at the deck" not in kw["text"] and "can you look at the deck" not in kw["html"]
    assert "panel=chat&room=room1&message=m1&from=away_email" in kw["text"]
    assert kw["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    world.finish.assert_awaited_once_with("AWAY#user_b", "M#room1#m1")
    event = world.capture.call_args.kwargs
    assert event["event"] == "needs_you_email_sent" and event["distinct_id"] == "user_b"
    assert event["properties"] == {"workspace_id": "org_1", "kind": "dm", "delay_s": 360}


@pytest.mark.asyncio
async def test_a_failed_send_is_retried_not_marked_sent(world, monkeypatch):
    world.send.return_value = None
    table = MagicMock()
    monkeypatch.setattr(away.webhook_dedup, "_table", lambda: table)
    assert await away.check_one(ROW, NOW) is False
    world.finish.assert_not_awaited()
    world.capture.assert_not_called()
    world.defer.assert_awaited_once_with("AWAY#user_b", "M#room1#m1", EPOCH + away._TICK_SECONDS)
    table.delete_item.assert_called_once_with(Key={"event_id": "away-slot:user_b:room1"})


@pytest.mark.asyncio
async def test_a_read_message_sends_nothing(world):
    world.cursor.return_value = 7
    assert await away.check_one(ROW, NOW) is False
    world.send.assert_not_awaited()
    world.finish.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_member_removed_from_the_room_is_never_sent_its_messages(world):
    world.member.return_value = None
    assert await away.check_one(ROW, NOW) is False
    world.send.assert_not_awaited()
    world.member.assert_awaited_once_with("room1", "human", "user_b")


@pytest.mark.asyncio
async def test_a_deleted_room_sends_nothing(world):
    world.room.return_value = {"kind": "channel", "deletedAt": "2026-10-14T14:58:00+00:00"}
    assert await away.check_one(ROW, NOW) is False
    world.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_neither_the_original_nor_an_edit_reaches_the_email(world, monkeypatch):
    edited = {**MESSAGE, "editedText": "never mind"}
    monkeypatch.setattr(away.messaging_repo, "get_message_by_id", AsyncMock(return_value=edited))
    assert await away.check_one(ROW, NOW) is True
    kw = world.send.await_args.kwargs
    for body in (kw["text"], kw["html"], kw["subject"]):
        assert "never mind" not in body and "deck" not in body


@pytest.mark.asyncio
async def test_a_person_in_the_app_is_deferred_not_emailed(world):
    world.user["last_active_at"] = EPOCH - 30
    assert await away.check_one(ROW, NOW) is False
    world.send.assert_not_awaited()
    world.defer.assert_awaited_once_with("AWAY#user_b", "M#room1#m1", EPOCH + away.PRESENT_S)


@pytest.mark.asyncio
async def test_quiet_hours_the_setting_and_the_room_cap_each_stop_it(world):
    world.user["timezone"] = "Asia/Tokyo"  # midnight there
    assert await away.check_one(ROW, NOW) is False
    world.user["timezone"] = "America/New_York"
    world.user["away_email"] = False
    assert await away.check_one(ROW, NOW) is False
    world.user["away_email"] = True
    world.slot.return_value = False
    assert await away.check_one(ROW, NOW) is False
    world.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_queued_paperclip_approval_is_dropped_unsent(world):
    """Nothing can answer a Paperclip approval since Phase 1 task 7, so its queued row never emails."""
    row = {**ROW, "sk": "A#co#ap1", "kind": "approval", "approvalId": "ap1", "title": "Growth · LB-12"}
    assert await away.check_one(row, NOW) is False
    world.send.assert_not_awaited()
    world.finish.assert_awaited_once()


def test_mention_copy_names_the_room():
    out = away.render({**ROW, "kind": "mention"}, author="Alice", room={"kind": "channel", "name": "gtm"}, sent=1)
    assert out["subject"] == "You were mentioned in Lightbulb"
    assert out["lines"][0] == "Alice mentioned you in #gtm."
    group = away.render(ROW, author="Friday", room={"kind": "group-dm", "name": "Humans + Friday"}, sent=1)
    assert group["lines"][0] == "New messages are waiting for you in Humans + Friday."


def test_unsubscribe_token_is_scoped_to_this_email():
    token = away.mint_unsubscribe_token("user_b")
    assert away.verify_unsubscribe_token(token) == "user_b"
    from core.services import daily_digest

    assert daily_digest.verify_unsubscribe_token(token) is None
