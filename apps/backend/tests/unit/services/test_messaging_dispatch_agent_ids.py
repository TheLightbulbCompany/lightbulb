"""``dispatchAgentIds`` on the public message: the backend's own dispatch
decision, not a client-side @mention guess (Codex review on PR #3345). A
message that opens by naming an agent ("Hey Friday, ...") or that an
`active` listener picks up carries no @mention at all, so a client can never
derive "Delivered" from `mentions` alone -- it needs the real dispatch
targets `select_dispatch_targets` already computed and `create_message`
already stored.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging

OWNER = "org_1"
USER = "user_owner"
FRIDAY = str(uuid.UUID(int=1))
ROOM = str(uuid.UUID(int=9))


def _agent_member(agent_id: str = FRIDAY, *, listen_mode: str = "mentions") -> dict:
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": "Friday",
        "mentionHandle": "friday",
        "role": "member",
        "listenMode": listen_mode,
        "joinedSeq": 1,
    }


def _human_member() -> dict:
    return {
        "kind": "human",
        "id": USER,
        "displayName": "Owner",
        "mentionHandle": None,
        "role": "owner",
        "listenMode": None,
        "joinedSeq": 1,
    }


def _wire(monkeypatch, members: list[dict]) -> None:
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"kind": "channel", "ownerId": OWNER}, _human_member())),
    )
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_agent_member()))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))

    async def create_message(**kwargs):
        # Plays back exactly what messaging_repo.create_message stores: the real
        # dispatch_targets this request's own select_dispatch_targets computed.
        return {
            "roomId": kwargs["room_id"],
            "messageId": str(uuid.uuid4()),
            "messageSeq": 1,
            "roomRevision": 1,
            "author": kwargs["author"],
            "text": kwargs["text"],
            "mentions": kwargs["mentions"],
            "replyToMessageId": kwargs.get("reply_to_message_id"),
            "threadRootMessageId": kwargs.get("thread_root_message_id"),
            "createdAt": messaging.utc_now().isoformat(),
            "runId": kwargs.get("run_id"),
            "dispatchTargets": kwargs["dispatch_targets"],
            "attachments": kwargs.get("attachments") or [],
        }, False

    monkeypatch.setattr(messaging.messaging_repo, "create_message", AsyncMock(side_effect=create_message))


async def _send(text: str, mentions: list[dict] | None = None) -> messaging.CanonicalMessage:
    return await messaging.create_human_message(
        AuthContext(user_id=USER, org_id=OWNER),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(),
            text=text,
            mentions=mentions or [],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_a_message_opening_with_an_agents_name_carries_its_id(monkeypatch):
    _wire(monkeypatch, [_human_member(), _agent_member()])
    result = await _send("Hey Friday, can you check the deploy?")
    assert result.dispatch_agent_ids == [FRIDAY]


@pytest.mark.asyncio
async def test_an_at_mentioned_message_carries_the_agents_id(monkeypatch):
    _wire(monkeypatch, [_human_member(), _agent_member()])
    result = await _send("please take a look", mentions=[{"kind": "agent", "id": FRIDAY}])
    assert result.dispatch_agent_ids == [FRIDAY]


@pytest.mark.asyncio
async def test_a_message_with_no_dispatch_targets_carries_an_empty_list(monkeypatch):
    _wire(monkeypatch, [_human_member(), _agent_member(listen_mode="mentions")])
    result = await _send("just chatting, nobody named")
    assert result.dispatch_agent_ids == []


def test_canonical_message_reads_dispatch_targets_off_the_stored_row():
    """The read side, isolated from the send flow: an updated/reacted-to row
    (update_message/set_reaction preserve dispatchTargets by merging over the
    stored row) still exposes the same ids canonical_message ever did."""
    item = {
        "messageId": str(uuid.uuid4()),
        "roomId": ROOM,
        "messageSeq": 1,
        "roomRevision": 1,
        "author": {"kind": "human", "id": USER, "displayName": "Owner"},
        "text": "hi",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": messaging.utc_now().isoformat(),
        "runId": None,
        "dispatchTargets": [{"agentId": FRIDAY, "eventKind": "user_request"}],
    }
    assert messaging.canonical_message(item).dispatch_agent_ids == [FRIDAY]
    assert messaging.canonical_message({**item, "dispatchTargets": []}).dispatch_agent_ids == []
    assert (
        messaging.canonical_message({k: v for k, v in item.items() if k != "dispatchTargets"}).dispatch_agent_ids == []
    )
