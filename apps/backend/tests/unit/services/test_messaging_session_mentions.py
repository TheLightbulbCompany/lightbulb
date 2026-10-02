"""A session chip in a room message is a reference, like a document link (owner ruling
2026-10-02): it is stored with a readable label and never wakes or messages the session."""

import time
import uuid
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
    # A Codex session on an older Mac (no roomReplyRequestId): a reference needs no reply support.
    return {
        "sessionId": SESSION,
        "provider": "codex",
        "ownerId": OWNER,
        "humanUserId": USER,
        "generation": "gen-1",
        "status": "offline",
        "onlineUntil": int(time.time()) - 60,
        "capabilities": {"sendMode": "queue", "steer": False, "interrupt": False},
        "capabilitiesVerified": True,
        **changes,
    }


def _wire(monkeypatch, *, members=None, foreign=False, kind="channel"):
    members = members or [_member()]
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"kind": kind, "ownerId": OWNER}, _member())),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_member("agent")))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "has_foreign_members", AsyncMock(return_value=foreign))
    monkeypatch.setattr(messaging.messaging_repo, "_get", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(
        messaging, "address", AsyncMock(return_value={"agents": [], "humans": [], "anyone": False, "path": "none"})
    )

    async def create_message(**kwargs):
        item = {
            "roomId": ROOM,
            "messageId": str(uuid.UUID(int=99)),
            "messageSeq": 1,
            "roomRevision": 1,
            "author": kwargs["author"],
            "text": kwargs["text"],
            "mentions": kwargs["mentions"],
            "createdAt": messaging.utc_now().isoformat(),
            "dispatchTargets": kwargs["dispatch_targets"],
        }
        if kwargs.get("session_mentions"):
            item["sessionMentions"] = kwargs["session_mentions"]
        if kwargs.get("session_target_labels"):
            item["sessionTargetLabels"] = kwargs["session_target_labels"]
        return item, False

    create = AsyncMock(side_effect=create_message)
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(local_sessions, "authorize_session", AsyncMock(return_value=(_session(), {})))
    submit = AsyncMock()
    monkeypatch.setattr(local_sessions, "submit_command", submit)
    return create, submit


async def _send(text="look at this", *, sessions=None, mentions=None):
    return await messaging.create_human_message(
        AuthContext(user_id=USER, org_id=OWNER),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(),
            text=text,
            mentions=mentions or [],
            session_mentions=sessions or [],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_a_session_mention_is_stored_with_its_label_and_never_sent(monkeypatch):
    create, submit = _wire(monkeypatch)
    local_sessions.authorize_session.return_value = (_session(displayTitle="Readable name", title="Native"), {})
    result = await _send(sessions=[SESSION, SESSION])
    label = "Codex · Readable name · Owner"
    assert create.await_args.kwargs["session_mentions"] == [SESSION]
    assert create.await_args.kwargs["session_target_labels"] == {SESSION: label}
    assert result.session_mentions == [SESSION] and result.session_target_labels == {SESSION: label}
    submit.assert_not_awaited()
    # A reference checks visibility, not control: any workspace member may link a session.
    assert "owner_only" not in local_sessions.authorize_session.await_args.kwargs


@pytest.mark.asyncio
async def test_a_session_mention_leaves_agent_dispatch_unchanged(monkeypatch):
    create, _ = _wire(monkeypatch, members=[_member(), _member("agent", listen_mode="active")])
    await _send(sessions=[SESSION])
    assert create.await_args.kwargs["dispatch_targets"] == [{"agentId": FRIDAY, "eventKind": "room_event"}]
    _wire(monkeypatch, members=[_member(), _member("agent", listen_mode="active")], kind="dm")
    dm = await _send(sessions=[SESSION])
    assert dm.addressed_agent_ids == [FRIDAY]


@pytest.mark.asyncio
async def test_a_session_the_room_cannot_see_is_refused(monkeypatch):
    create, _ = _wire(monkeypatch)
    local_sessions.authorize_session.side_effect = messaging.ApiProblem("LOCAL_SESSION_NOT_FOUND", "gone")
    with pytest.raises(messaging.ApiProblem, match="gone"):
        await _send(sessions=[SESSION])
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"unshared": True}, {"kind": "background"}])
async def test_private_or_background_session_cannot_be_referenced(monkeypatch, changes):
    create, _ = _wire(monkeypatch)
    local_sessions.authorize_session.return_value = (_session(**changes), {})
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send(sessions=[SESSION])
    assert exc.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_owner_must_belong_to_the_room(monkeypatch):
    create, _ = _wire(monkeypatch, members=[_member("agent", listen_mode="active")])
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send(sessions=[SESSION])
    assert exc.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_room_shared_with_another_org_cannot_reference_sessions(monkeypatch):
    create, _ = _wire(monkeypatch, foreign=True)
    with pytest.raises(messaging.ApiProblem) as exc:
        await _send(sessions=[SESSION])
    assert exc.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_ids_participate_in_message_idempotency_hash(monkeypatch):
    create, _ = _wire(monkeypatch)
    await _send(sessions=[SESSION])
    first = create.await_args.kwargs["req_hash"]
    await _send(sessions=[str(uuid.UUID(int=43))])
    assert create.await_args.kwargs["req_hash"] != first


def test_session_mentions_are_absent_when_empty_and_derived_from_legacy_labels():
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
    wire = messaging.canonical_message(item).model_dump(mode="json", by_alias=True)
    assert "sessionMentions" not in wire and "sessionTargetLabels" not in wire
    legacy = {
        **item,
        "sessionTargetLabels": {SESSION: "Codex · Launch · Owner"},
        "sessionSends": [{"sessionId": SESSION}],
    }
    wire = messaging.canonical_message(legacy).model_dump(mode="json", by_alias=True)
    assert wire["sessionMentions"] == [SESSION] and "sessionSends" not in wire


@pytest.mark.asyncio
async def test_agent_ingress_names_referenced_sessions_the_room_may_see(monkeypatch):
    from core.services import openclaw_work

    monkeypatch.setattr(openclaw_work.messaging_repo, "list_messages_before", AsyncMock(return_value=[]))
    hidden = str(uuid.UUID(int=43))
    live = {"sessionId": SESSION, "provider": "codex", "title": "Launch", "status": "idle", "cwdName": "app"}
    body = await openclaw_work.build_ingress_body(
        meta={"roomId": ROOM, "name": "general", "kind": "channel", "revision": 1},
        target_agent_id=FRIDAY,
        event_kind="user_request",
        message={
            "messageId": str(uuid.UUID(int=99)),
            "messageSeq": 4,
            "roomId": ROOM,
            "roomRevision": 1,
            "author": {"kind": "human", "id": USER, "displayName": "Owner"},
            "text": "see this",
            "mentions": [],
            "createdAt": "2026-10-02T00:00:00Z",
            "sessionMentions": [SESSION, hidden],
        },
        members=[_member()],
        sessions=[live],
    )
    assert body["referencedSessions"] == [
        {"sessionId": SESSION, "provider": "codex", "title": "Launch", "status": "idle"}
    ]
