"""An agent reply can carry its own attachments (Task 9 / design §2i).

The Hermes bridge parses `MEDIA:<path>` out of the reply text and sends the bytes
base64-inline on the reply POST it is already making; this is the receiving half. It stores
through the very same `store_attachment` the human upload route uses, so the room message
carries a real attachment the web and mobile clients already render.
"""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from core.services import messaging

OWNER = "org_1"
AGENT = str(uuid.UUID(int=1))
ROOM = str(uuid.UUID(int=9))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def owner_objects(monkeypatch):
    """An in-memory stand-in for the owner-state bucket, keyed by absolute S3 key."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(messaging.hermes_profiles, "put_owner_object", lambda key, body: store.__setitem__(key, body))
    monkeypatch.setattr(messaging.hermes_profiles, "get_owner_object", store.get)
    return store


@pytest.fixture
def created(monkeypatch) -> MagicMock:
    """Everything `create_agent_message` reads, stubbed; returns the create_message mock."""
    item = {
        "roomId": ROOM,
        "messageId": str(uuid.uuid4()),
        "messageSeq": 2,
        "createdAt": messaging.utc_now().isoformat(),
    }
    create = AsyncMock(return_value=(item, False))
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": OWNER, "kind": "channel"})
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value={"kind": "agent", "id": AGENT}))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(
        messaging.messaging_repo, "list_members", AsyncMock(return_value=[{"kind": "agent", "id": AGENT}])
    )
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging, "put_metric", MagicMock())
    monkeypatch.setattr(messaging, "_emit_reply_telemetry", AsyncMock())
    return create


def _request(text: str = "Screenshot:", **over) -> messaging.AgentMessageRequest:
    body = {
        "eventId": f"evt-{uuid.uuid4()}",
        "roomId": ROOM,
        "agentId": AGENT,
        "text": text,
        "replyToMessageId": None,
        "threadRootMessageId": None,
        **over,
    }
    return messaging.AgentMessageRequest.model_validate(body)


def _inline(data: bytes = PNG, name: str = "shot.png", content_type: str = "image/png") -> dict:
    return {"fileName": name, "contentType": content_type, "dataBase64": base64.b64encode(data).decode()}


@pytest.mark.asyncio
async def test_agent_message_stores_inline_attachments(owner_objects, created):
    await messaging.create_agent_message(OWNER, _request(attachments=[_inline()]))

    stored = created.await_args.kwargs["attachments"]
    assert len(stored) == 1
    assert stored[0]["contentType"] == "image/png"
    assert stored[0]["fileName"] == "shot.png"
    assert stored[0]["size"] == len(PNG)
    # Stored exactly as a human upload is -- readable back through the existing route.
    metadata, data = messaging.load_attachment(OWNER, ROOM, stored[0]["id"])
    assert data == PNG and metadata.content_type == "image/png"


@pytest.mark.asyncio
async def test_an_agent_message_without_attachments_is_unchanged(owner_objects, created):
    await messaging.create_agent_message(OWNER, _request("done"))
    assert created.await_args.kwargs["attachments"] == []
    assert owner_objects == {}


def test_more_than_four_attachments_is_rejected():
    with pytest.raises(ValidationError):
        _request(attachments=[_inline(name=f"{i}.png") for i in range(messaging.ATTACHMENT_MAX_COUNT + 1)])


def test_attachment_bytes_must_be_base64():
    with pytest.raises(ValidationError):
        _request(attachments=[{"fileName": "shot.png", "contentType": "image/png", "dataBase64": "not base64!!"}])


@pytest.mark.asyncio
async def test_bytes_that_do_not_match_the_declared_type_are_rejected(owner_objects, created):
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_agent_message(OWNER, _request(attachments=[_inline(b"%PDF-1.7 not a png")]))
    assert exc.value.error.code == "INVALID_REQUEST"
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_oversized_attachment_is_rejected(owner_objects, created):
    huge = PNG + b"0" * messaging.IMAGE_MAX_BYTES
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_agent_message(OWNER, _request(attachments=[_inline(huge)]))
    assert exc.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_a_reply_that_is_nothing_but_a_screenshot_needs_no_text(owner_objects, created):
    """Stripping the only `MEDIA:` line empties the text; the same rule the human path already
    has (`CreateMessageRequest.require_content`) lets it through as a pure attachment."""
    await messaging.create_agent_message(OWNER, _request("", attachments=[_inline()]))
    assert created.await_args.kwargs["text"] == ""

    with pytest.raises(ValidationError):
        _request("")  # ...but an empty reply with nothing attached is still nothing
