"""A member texts the line: their message lands in the main room, and an agent's answer goes back by text."""

from unittest.mock import AsyncMock

import pytest

from core.services import messaging, text_relay
from tests.unit.services.test_agent_message_attachments import AGENT, OWNER, ROOM, _request, created  # noqa: F401
from tests.unit.services.test_run_events import RUN, STARTED_ROW, _event

KEY = "agents@walker.dev.thelightbulb.company/2026-10-19T03:25:49Z#m1"
TEXT_TRIGGER = {
    "messageId": "t1",
    "author": {"kind": "human", "id": "user_1", "displayName": "Dana"},
    "via": "text",
    "textReplyKey": KEY,
}
PROVIDER = {"space_id": "sp1", "line_user_id": "lu1", "message_id": "pm1"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    text_relay._KEYS.clear()


@pytest.fixture
def room(monkeypatch, created):  # noqa: F811
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"kind": "agent", "id": AGENT, "displayName": "CEO", "listenMode": "active"}]),
    )
    relay = AsyncMock(return_value="sent")
    monkeypatch.setattr(text_relay, "relay_reply", relay)

    def with_trigger(trigger: dict):
        monkeypatch.setattr(
            messaging.messaging_repo,
            "get_run",
            AsyncMock(return_value={"roomId": ROOM, "agentId": AGENT, "triggerMessageId": "t1"}),
        )
        monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=trigger))
        return relay

    return with_trigger


@pytest.mark.asyncio
async def test_an_agents_reply_to_a_texted_message_is_texted_back(room, created):  # noqa: F811
    relay = room(TEXT_TRIGGER)
    await messaging.create_agent_message(OWNER, _request("I'm the CEO, Dana.", runId=str(RUN)))
    relay.assert_awaited_once_with(OWNER, TEXT_TRIGGER, "I'm the CEO, Dana.")


@pytest.mark.asyncio
async def test_a_failed_relay_answers_503_and_the_retry_replay_sends_again(room, created):  # noqa: F811
    """The reply is stored either way; a failed text makes the container retry, and the replay relays again."""
    relay = room(TEXT_TRIGGER)
    relay.return_value = "send_failed"
    request = _request("I'm the CEO, Dana.", runId=str(RUN))
    with pytest.raises(messaging.ApiProblem) as failed:
        await messaging.create_agent_message(OWNER, request)
    assert failed.value.error.code == "TEXT_RELAY_UNAVAILABLE" and failed.value.status_code == 503
    created.return_value = (created.return_value[0], True)  # the retry replays the stored reply
    relay.return_value = "sent"
    await messaging.create_agent_message(OWNER, request)
    assert relay.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "extra"),
    [
        ({**TEXT_TRIGGER, "textReplyKey": None}, {}),  # a message typed in the app
        (TEXT_TRIGGER, {"progress": True}),  # a progress line is not the answer
    ],
)
async def test_nothing_is_texted_for_an_app_message_or_a_progress_line(room, created, trigger, extra):  # noqa: F811
    relay = room(trigger)
    await messaging.create_agent_message(OWNER, _request("working on it", runId=str(RUN), **extra))
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_sends_through_the_text_door_without_an_agent_fence(monkeypatch):
    from routers import agent_text

    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(agent_text, "send_reply", send)
    metric = []
    monkeypatch.setattr(text_relay, "put_metric", lambda name, **kw: metric.append((name, kw["dimensions"])))
    assert await text_relay.relay_reply(OWNER, TEXT_TRIGGER, "hello") == "sent"
    send.assert_awaited_once_with(owner_id=OWNER, reply_key=KEY, text="hello")  # no agent_id: the backend sends
    assert metric == [("text.member_reply_relay", {"status": "sent"})]
    send.reset_mock()
    assert await text_relay.relay_reply(OWNER, TEXT_TRIGGER, "   ") == "skipped"
    send.assert_not_awaited()


@pytest.fixture
def photon(monkeypatch):
    monkeypatch.setattr(text_relay.photon, "enabled", lambda: True)
    typing, read = AsyncMock(), AsyncMock()
    monkeypatch.setattr(text_relay.photon, "send_typing", typing)
    monkeypatch.setattr(text_relay.photon, "mark_read", read)
    monkeypatch.setattr(text_relay.messaging_repo, "get_message_by_id", AsyncMock(return_value={"textReplyKey": KEY}))
    row = {"owner_id": OWNER, "provider": PROVIDER}
    monkeypatch.setattr(text_relay.agent_mail_inbox_repo, "get_message", AsyncMock(return_value=row))
    return typing, read


@pytest.mark.asyncio
async def test_a_run_on_a_texted_message_shows_typing_and_marks_it_read(photon):
    typing, read = photon
    run = {"roomId": ROOM, "triggerMessageId": "t1", "runId": "r1"}
    await text_relay.signal_working(OWNER, run)
    typing.assert_awaited_once_with(sender_id="lu1", space_id="sp1")
    read.assert_awaited_once_with("pm1")
    typing.reset_mock(), read.reset_mock()
    # A heartbeat keeps the bubble up, and reads too: it may be what started a run whose run.started was lost.
    await text_relay.signal_working(OWNER, run)
    typing.assert_awaited_once()
    read.assert_awaited_once_with("pm1")
    assert text_relay.messaging_repo.get_message_by_id.await_count == 1  # the trigger is looked up once


@pytest.mark.asyncio
async def test_no_signal_for_an_app_message_or_another_owners_text(photon, monkeypatch):
    typing, read = photon
    monkeypatch.setattr(text_relay.messaging_repo, "get_message_by_id", AsyncMock(return_value={"text": "hi"}))
    await text_relay.signal_working(OWNER, {"roomId": ROOM, "triggerMessageId": "t2"})
    other = {"owner_id": "org_other", "provider": PROVIDER}
    monkeypatch.setattr(text_relay.messaging_repo, "get_message_by_id", AsyncMock(return_value={"textReplyKey": KEY}))
    monkeypatch.setattr(text_relay.agent_mail_inbox_repo, "get_message", AsyncMock(return_value=other))
    await text_relay.signal_working(OWNER, {"roomId": ROOM, "triggerMessageId": "t3"})
    typing.assert_not_awaited()
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_started_and_heartbeat_send_the_working_signal(monkeypatch):
    monkeypatch.setattr(messaging, "announce_run", AsyncMock())
    monkeypatch.setattr(messaging, "_publish_room_event", AsyncMock())
    signal = AsyncMock()
    monkeypatch.setattr(text_relay, "signal_working", signal)
    monkeypatch.setattr(messaging.messaging_repo, "start_run", AsyncMock(return_value=STARTED_ROW))
    monkeypatch.setattr(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=STARTED_ROW))
    await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))
    await messaging.handle_run_event("o", _event(type="run.heartbeat"))
    assert signal.await_count == 2
