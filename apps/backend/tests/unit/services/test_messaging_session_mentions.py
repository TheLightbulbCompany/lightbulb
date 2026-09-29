"""Human room messages can address sessions without making sessions room members."""

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import local_sessions, messaging

OWNER = "org_1"
USER = "user_owner"
ROOM = str(uuid.UUID(int=9))
SESSION = str(uuid.UUID(int=42))
FRIDAY = str(uuid.UUID(int=1))


def _member(kind="human", *, listen_mode=None):
    return {
        "kind": kind,
        "id": USER if kind == "human" else FRIDAY,
        "displayName": "Owner" if kind == "human" else "Friday",
        "mentionHandle": None if kind == "human" else "friday",
        "role": "owner" if kind == "human" else "member",
        "listenMode": listen_mode,
        "joinedSeq": 1,
    }


def _session(**changes):
    return {
        "sessionId": SESSION,
        "ownerId": OWNER,
        "humanUserId": USER,
        "generation": "gen-1",
        "status": "idle",
        "onlineUntil": int(time.time()) + 60,
        "capabilities": {"sendMode": "queue", "steer": False, "interrupt": False},
        "capabilitiesVerified": True,
        **changes,
    }


def _wire(monkeypatch, *, members=None, foreign=False, replayed=False):
    members = members or [_member()]
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"kind": "channel", "ownerId": OWNER}, _member())),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_member("agent")))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "has_foreign_members", AsyncMock(return_value=foreign))
    monkeypatch.setattr(messaging.messaging_repo, "_get", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    item = {
        "roomId": ROOM,
        "messageId": str(uuid.UUID(int=99)),
        "messageSeq": 1,
        "roomRevision": 1,
        "author": {"kind": "human", "id": USER, "displayName": "Owner"},
        "text": "tag the session",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": messaging.utc_now().isoformat(),
        "runId": None,
        "dispatchTargets": [],
        "attachments": [],
    }

    async def create_message(**kwargs):
        item.update(text=kwargs["text"], mentions=kwargs["mentions"], dispatchTargets=kwargs["dispatch_targets"])
        return {key: value for key, value in item.items() if replayed or key != "sessionSends"}, replayed

    async def set_sends(room_id, seq, sends):
        item["sessionSends"] = sends
        return item.copy()

    create = AsyncMock(side_effect=create_message)
    set_session_sends = AsyncMock(side_effect=set_sends)
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging.messaging_repo, "set_session_sends", set_session_sends)
    from core.services import live_events, local_session_relay

    monkeypatch.setattr(live_events, "get_live_event_publisher", lambda: SimpleNamespace(publish_room=AsyncMock()))
    dispatch = AsyncMock()
    monkeypatch.setattr(
        local_session_relay, "get_local_session_relay", lambda: SimpleNamespace(dispatch_session=dispatch)
    )
    monkeypatch.setattr(local_sessions, "authorize_session", AsyncMock(return_value=(_session(), {})))
    return create, set_session_sends, dispatch, item


async def _send(text="tag the session", *, sessions=None, mentions=None, client_id=None):
    return await messaging.create_human_message(
        AuthContext(user_id=USER, org_id=OWNER),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=client_id or uuid.uuid4(),
            text=text,
            mentions=mentions or [],
            session_mentions=sessions or [],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_tagged_session_gets_one_roomless_command_and_receipt(monkeypatch):
    create, set_sends, dispatch, _ = _wire(monkeypatch)
    submit = AsyncMock(return_value=SimpleNamespace(request_id="receipt-1"))
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    result = await _send(sessions=[SESSION, SESSION])
    assert create.await_count == 1 and submit.await_count == 1
    assert submit.await_args.args[1:3] == (None, SESSION)
    assert submit.await_args.kwargs["origin_room_id"] == ROOM
    assert submit.await_args.kwargs["origin_message_id"] == str(uuid.UUID(int=99))
    assert set_sends.await_args.args[2] == [{"sessionId": SESSION, "requestId": "receipt-1"}]
    dispatch.assert_awaited_once_with(OWNER, SESSION)
    assert result.session_sends == [messaging.SessionSend(session_id=SESSION, request_id="receipt-1")]


@pytest.mark.asyncio
async def test_busy_immediate_session_gets_its_active_turn_id(monkeypatch):
    _wire(monkeypatch)
    local_sessions.authorize_session.return_value = (
        _session(
            status="busy",
            activeTurnId="turn-7",
            capabilities={"sendMode": "immediate", "steer": True, "interrupt": False},
        ),
        {},
    )
    submit = AsyncMock(return_value=SimpleNamespace(request_id="receipt-1"))
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    await _send(sessions=[SESSION])
    assert submit.await_args.args[3].expected_turn_id == "turn-7"


@pytest.mark.asyncio
async def test_unauthorized_or_offline_session_posts_nothing(monkeypatch):
    create, _, _, _ = _wire(monkeypatch)
    local_sessions.authorize_session.side_effect = messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", "denied")
    with pytest.raises(messaging.ApiProblem, match="denied"):
        await _send(sessions=[SESSION])
    create.assert_not_awaited()
    local_sessions.authorize_session.side_effect = None
    local_sessions.authorize_session.return_value = (_session(status="offline"), {})
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send(sessions=[SESSION])
    assert exc.value.error.code == "LOCAL_SESSION_OFFLINE"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_room_and_oversize_command_post_nothing(monkeypatch):
    create, _, _, _ = _wire(monkeypatch, foreign=True)
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send(sessions=[SESSION])
    assert exc.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    create.assert_not_awaited()
    messaging.messaging_repo.has_foreign_members.return_value = False
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send("x" * local_sessions.MAX_COMMAND_BYTES, sessions=[SESSION])
    assert exc.value.error.code == "INVALID_REQUEST"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_becoming_foreign_after_message_write_sends_no_session_command(monkeypatch):
    create, set_sends, _, _ = _wire(monkeypatch)
    messaging.messaging_repo.has_foreign_members.side_effect = [False, True]
    submit = AsyncMock()
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    await _send(sessions=[SESSION])
    create.assert_awaited_once()
    submit.assert_not_awaited()
    assert set_sends.await_args.args[2] == [{"sessionId": SESSION, "error": "LOCAL_SESSION_FORBIDDEN"}]


@pytest.mark.asyncio
async def test_session_tag_mutes_ambient_agent_but_keeps_explicit_agent_mention(monkeypatch):
    _wire(monkeypatch, members=[_member(), _member("agent", listen_mode="active")])
    monkeypatch.setattr(local_sessions, "submit_command", AsyncMock(return_value=SimpleNamespace(request_id="r1")))
    ambient = await _send(sessions=[SESSION])
    assert ambient.addressed_agent_ids == []
    assert messaging.messaging_repo.create_message.await_args.kwargs["dispatch_targets"] == []
    explicit = await _send(sessions=[SESSION], mentions=[{"kind": "agent", "id": FRIDAY}])
    assert explicit.addressed_agent_ids == [FRIDAY]


@pytest.mark.asyncio
async def test_retry_reuses_recorded_sends_without_a_second_command(monkeypatch):
    _, set_sends, _, item = _wire(monkeypatch, replayed=True)
    item["sessionSends"] = [{"sessionId": SESSION, "requestId": "receipt-1"}]
    messaging.messaging_repo._get.return_value = {"requestHash": "already committed"}
    submit = AsyncMock()
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    result = await _send(sessions=[SESSION])
    assert result.session_sends[0].request_id == "receipt-1"
    submit.assert_not_awaited()
    set_sends.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_message_commit_reuses_the_existing_command(monkeypatch):
    _, set_sends, _, item = _wire(monkeypatch, replayed=True)
    messaging.messaging_repo._get.return_value = {"requestHash": "already committed"}
    from core.repositories import local_sessions_repo

    monkeypatch.setattr(
        local_sessions_repo,
        "get_command",
        AsyncMock(return_value={"originRoomId": ROOM, "originMessageId": item["messageId"]}),
    )
    submit = AsyncMock()
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    result = await _send(sessions=[SESSION])
    assert result.session_sends[0].request_id == f"m-{item['messageId']}-{SESSION}"
    submit.assert_not_awaited()
    assert set_sends.await_count == 1


@pytest.mark.asyncio
async def test_session_ids_participate_in_message_idempotency_hash(monkeypatch):
    create, _, _, _ = _wire(monkeypatch)
    monkeypatch.setattr(local_sessions, "submit_command", AsyncMock(return_value=SimpleNamespace(request_id="r1")))
    await _send(sessions=[SESSION])
    first = create.await_args.kwargs["req_hash"]
    await _send(sessions=[str(uuid.UUID(int=43))])
    assert create.await_args.kwargs["req_hash"] != first


def test_session_sends_are_absent_when_empty_and_optional_fields_are_omitted():
    item = {
        "roomId": ROOM,
        "messageId": str(uuid.UUID(int=99)),
        "messageSeq": 1,
        "roomRevision": 1,
        "author": {"kind": "human", "id": USER, "displayName": "Owner"},
        "text": "hello",
        "mentions": [],
        "createdAt": messaging.utc_now().isoformat(),
    }
    assert "sessionSends" not in messaging.canonical_message(item).model_dump(mode="json", by_alias=True)
    with_sends = messaging.canonical_message({**item, "sessionSends": [{"sessionId": SESSION, "error": "offline"}]})
    assert with_sends.model_dump(mode="json", by_alias=True)["sessionSends"] == [
        {"sessionId": SESSION, "error": "offline"}
    ]
