"""The conversation store's calls into the products (engine-service plan, Task 2): a room archived or a member removed
(`event`), an agent's post in a Meet's hidden room (`context("post_target")`), an agent's answer in a call room
(`event("line.posted")`). A plain room asks and tells the products nothing on the post path."""

# ruff: noqa: F811 -- `table` and `seen` are fixtures, imported and then requested by name.

import logging
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from core.voice import livekit_huddles
from core.voice.phone import calls
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, USER_A, USER_B, _member, repo, table  # noqa: F401

from .test_hooks import seen  # noqa: F401


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": FRIDAY, "name": "Friday"}]))
    monkeypatch.setattr(messaging, "_emit_reply_telemetry", AsyncMock())


async def _create(room_id, kind="channel", extra=None, members=None):
    await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=room_id,
        creator_user_id=USER_A,
        name="r",
        kind=kind,
        members=members
        or [
            _member("human", USER_A, "owner"),
            _member("human", USER_B, "member"),
            _member("agent", FRIDAY, listen="mentions", handle="friday"),
        ],
        req_hash=repo.request_hash(room_id),
        extra=extra or {},
    )


def _agent_post(room_id, event_id="evt-1", text="Three tasks open."):
    return messaging.AgentMessageRequest.model_validate(
        {
            "eventId": event_id,
            "roomId": room_id,
            "agentId": FRIDAY,
            "text": text,
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )


async def test_archive_emits_conversation_archived(seen, monkeypatch):
    from ..services.test_messaging_service_review_findings import _archive_fixture

    meta = {"roomId": "chan", "ownerId": OWNER, "kind": "channel", "revision": 2}
    _archive_fixture(monkeypatch, meta)
    closed = AsyncMock()
    monkeypatch.setattr(livekit_huddles, "close_room", closed)
    await messaging.patch_room(
        AuthContext(user_id=OWNER, human_session=True), "chan", messaging.PatchRoomRequest(archived=True)
    )
    [e] = seen["events"]
    assert (e.type, e.tenant_id, e.conversation_id) == ("conversation.archived", OWNER, "chan")
    closed.assert_awaited_once_with("chan")


async def test_archive_survives_an_unregistered_product(monkeypatch, caplog):
    """Best effort, as LiveKit was: the archive committed, so a failed event is logged with its traceback."""
    from core.engine import hooks

    from ..services.test_messaging_service_review_findings import _archive_fixture

    meta = {"roomId": "chan", "ownerId": OWNER, "kind": "channel", "revision": 2}
    _archive_fixture(monkeypatch, meta)
    monkeypatch.setattr(hooks, "_registered", None)
    with caplog.at_level(logging.WARNING, logger=messaging.logger.name):
        await messaging.patch_room(
            AuthContext(user_id=OWNER, human_session=True), "chan", messaging.PatchRoomRequest(archived=True)
        )
    [record] = [r for r in caplog.records if "conversation.archived" in r.getMessage()]
    assert record.levelno == logging.WARNING and record.exc_info[0] is hooks.HookNotRegistered


async def test_member_removal_emits_member_removed(seen, table, monkeypatch):
    removed = AsyncMock()
    monkeypatch.setattr(livekit_huddles, "remove_participant", removed)
    room_id = repo.deterministic_room_id(OWNER, USER_A, "c-room")
    await _create(room_id)

    await messaging.remove_member(AuthContext(user_id=OWNER, human_session=True), room_id, "human", USER_B)

    assert await repo.get_member(room_id, "human", USER_B) is None  # the member still loses room access
    [e] = seen["events"]
    assert (e.type, e.conversation_id, e.member_id) == ("member.removed", room_id, USER_B)
    removed.assert_awaited_once_with(room_id, USER_B)
    await messaging.remove_member(AuthContext(user_id=OWNER, human_session=True), room_id, "agent", FRIDAY)
    assert len(seen["events"]) == 1  # agents are never in the LiveKit room


async def test_meeting_post_goes_to_post_target(seen, table):
    default, hidden = messaging.default_room_id(OWNER), repo.deterministic_room_id(OWNER, USER_A, "meet")
    plain = repo.deterministic_room_id(OWNER, USER_A, "plain")
    await _create(default)
    await _create(hidden, extra={"meeting": {"uid": "a", "title": "Weekly sync"}, "hidden": True})
    await _create(plain)

    await messaging.create_agent_message(OWNER, _agent_post(plain, "evt-plain"))
    assert seen["asks"] == [] and seen["events"] == []  # a plain room asks the products nothing

    receipt = await messaging.create_agent_message(OWNER, _agent_post(hidden))
    [ask] = seen["asks"]
    assert (ask.purpose, ask.conversation_id, ask.agent_id) == ("post_target", hidden, FRIDAY)
    assert ask.annotations == {"meeting": {"uid": "a", "title": "Weekly sync"}}
    assert str(receipt.room_id) == default
    assert (await repo.get_room(hidden))["lastSeq"] == 0


async def test_meeting_post_not_seated_in_target_is_a_system_line(seen, table):
    """The fallback stays engine code: one system line naming the agent, keyed by the event id."""
    default, hidden = messaging.default_room_id(OWNER), repo.deterministic_room_id(OWNER, USER_A, "meet")
    await _create(default, members=[_member("human", USER_A, "owner")])
    await _create(hidden, extra={"meeting": {"uid": "a", "title": "Weekly sync"}, "hidden": True})
    first = await messaging.create_agent_message(OWNER, _agent_post(hidden, "live-late:run-9"))
    again = await messaging.create_agent_message(OWNER, _agent_post(hidden, "live-late:run-9"))
    assert str(first.room_id) == default and again.message_id == first.message_id
    line = await repo.get_message_by_id(default, str(first.message_id))
    assert line["author"]["kind"] == "system"
    assert line["text"] == "**Friday**, after **Weekly sync**:\n\nThree tasks open."


async def test_call_answer_emits_line_posted_best_effort(seen, table, monkeypatch, caplog):
    room_id = repo.deterministic_room_id(OWNER, USER_A, "call")
    await _create(room_id, kind="call")
    monkeypatch.setattr(calls, "after_call", AsyncMock(side_effect=RuntimeError("record write failed")))
    with caplog.at_level(logging.WARNING, logger=messaging.logger.name):
        receipt = await messaging.create_agent_message(OWNER, _agent_post(room_id))
    stored = await repo.get_message_by_id(room_id, str(receipt.message_id))
    assert stored["text"] == "Three tasks open."  # posted either way
    [e] = seen["events"]
    assert (e.type, e.conversation_id, e.message["messageId"]) == ("line.posted", room_id, stored["messageId"])
    calls.after_call.assert_awaited_once()
    [record] = [r for r in caplog.records if "late answer not kept" in r.getMessage()]
    assert record.exc_info[0] is RuntimeError
