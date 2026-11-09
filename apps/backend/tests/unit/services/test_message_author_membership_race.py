"""A sender can pass its membership pre-check and still lose the room before
``messaging_repo.create_message``'s transaction commits (removed mid-request).
The repo layer closes that race with a ConditionCheck on the author's own
MEMBER row and raises ``MessageForbidden``; this is the service layer's half
-- mapping that to the same ApiProblem code each path's own pre-check already
uses, one test per author kind.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import messaging_repo
from core.services import messaging
from tests.unit.services.test_agent_message_attachments import AGENT, ROOM, created  # noqa: F401

OWNER = "org_1"
USER = "user_a"
META = {"roomId": ROOM, "ownerId": OWNER, "kind": "channel", "revision": 1, "lastSeq": 3}


@pytest.mark.asyncio
async def test_human_send_maps_the_race_to_room_forbidden(monkeypatch):
    sender = {"kind": "human", "id": USER, "role": "member"}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, sender)))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[sender]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Ann"))
    monkeypatch.setattr(
        messaging.messaging_repo,
        "create_message",
        AsyncMock(side_effect=messaging_repo.MessageForbidden("gone")),
    )
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_human_message(
            AuthContext(user_id=USER, org_id=OWNER),
            ROOM,
            messaging.CreateMessageRequest(
                client_message_id=uuid.uuid4(),
                text="hi",
                mentions=[],
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
        )
    assert exc.value.error.code == "ROOM_FORBIDDEN"


@pytest.mark.asyncio
async def test_agent_send_maps_the_race_to_member_not_found(created):  # noqa: F811
    created.side_effect = messaging_repo.MessageForbidden("gone")
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": f"evt-{uuid.uuid4()}",
            "roomId": ROOM,
            "agentId": AGENT,
            "text": "reply",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_agent_message(OWNER, request)
    assert exc.value.error.code == "MEMBER_NOT_FOUND"
