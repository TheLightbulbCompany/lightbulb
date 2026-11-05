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
        messaging.messaging_repo,
        "get_room",
        AsyncMock(return_value={"roomId": ROOM, "ownerId": OWNER, "kind": "channel"}),
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


# ---- videos: uploaded straight to S3 by the runtime, adopted here (never inline) -------------
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


@pytest.fixture
def staged(owner_objects, monkeypatch):
    """`owner_objects` plus the HEAD/ranged-GET the adopt path uses instead of a full read."""
    monkeypatch.setattr(
        messaging.hermes_profiles,
        "owner_object_size",
        lambda key: len(owner_objects[key]) if key in owner_objects else None,
    )
    monkeypatch.setattr(messaging.hermes_profiles, "get_owner_object_head", lambda key, n: owner_objects[key][:n])
    return owner_objects


def _uploaded(attachment_id: str, name: str = "hero.mp4", content_type: str = "video/mp4") -> dict:
    return {"attachmentId": attachment_id, "fileName": name, "contentType": content_type}


@pytest.mark.asyncio
async def test_an_uploaded_video_is_adopted_by_head_and_sniff(staged, created):
    att = str(uuid.uuid4())
    staged[messaging.attachment_key(OWNER, ROOM, f"{att}.mp4")] = MP4
    await messaging.create_agent_message(OWNER, _request(text="", uploadedAttachments=[_uploaded(att)]))

    stored = created.await_args.kwargs["attachments"]
    assert stored == [{"id": att, "fileName": "hero.mp4", "contentType": "video/mp4", "size": len(MP4)}]
    assert messaging.attachment_key(OWNER, ROOM, f"{att}.json") in staged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type", "error"),
    [
        (None, "video/mp4", "not found"),
        (b"%PDF-1.7 not a video at all", "video/mp4", "do not match"),
        (MP4, "text/plain", "not found"),  # a text object sits at <id>.txt, not .mp4
    ],
)
async def test_an_uploaded_attachment_is_refused_unless_it_is_really_there_and_what_it_claims(
    staged, created, body, content_type, error
):
    att = str(uuid.uuid4())
    if body is not None:
        staged[messaging.attachment_key(OWNER, ROOM, f"{att}.mp4")] = body
    name = "hero.txt" if content_type == "text/plain" else "hero.mp4"
    with pytest.raises(messaging.ApiProblem, match=error):
        await messaging.create_agent_message(OWNER, _request(uploadedAttachments=[_uploaded(att, name, content_type)]))
    created.assert_not_awaited()


def test_an_uploaded_text_file_is_read_whole_and_must_be_utf8(staged, monkeypatch):
    """A Worker's .md/.csv result (task VMs upload every file): adopted when it is UTF-8, as inline text is.
    Read bounded by the HEAD's size (#4821 review), never with an unbounded GET."""
    monkeypatch.setattr(messaging.hermes_profiles, "get_owner_object", lambda key: pytest.fail("unbounded read"))
    good, bad = str(uuid.uuid4()), str(uuid.uuid4())
    staged[messaging.attachment_key(OWNER, ROOM, f"{good}.md")] = "# Agenda — 9:00".encode()
    staged[messaging.attachment_key(OWNER, ROOM, f"{bad}.csv")] = b"\xff\xfe not utf-8"
    adopted = messaging.adopt_uploaded_attachment(OWNER, ROOM, good, "agenda.md", "text/markdown")
    assert adopted.content_type == "text/markdown" and adopted.size == len("# Agenda — 9:00".encode())
    with pytest.raises(ValueError, match="do not match"):
        messaging.adopt_uploaded_attachment(OWNER, ROOM, bad, "data.csv", "text/csv")


def test_an_uploaded_video_over_its_cap_is_refused(staged, monkeypatch):
    att = str(uuid.uuid4())
    key = messaging.attachment_key(OWNER, ROOM, f"{att}.mp4")
    staged[key] = MP4
    monkeypatch.setattr(messaging.hermes_profiles, "owner_object_size", lambda k: messaging.VIDEO_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="at most"):
        messaging.adopt_uploaded_attachment(OWNER, ROOM, att, "hero.mp4", "video/mp4")


def test_inline_and_uploaded_attachments_share_the_count_of_four():
    ids = [str(uuid.uuid4()) for _ in range(2)]
    with pytest.raises(ValidationError):
        _request(attachments=[_inline()] * 3, uploadedAttachments=[_uploaded(i) for i in ids])


def test_video_signatures():
    assert messaging.validate_attachment_bytes("video/mp4", MP4) == ".mp4"
    assert messaging.validate_attachment_bytes("video/quicktime", b"\x00\x00\x00\x14ftypqt  ") == ".mov"
    assert messaging.validate_attachment_bytes("video/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 8) == ".webm"
    with pytest.raises(ValueError):
        messaging.validate_attachment_bytes("video/mp4", b"ftyp" + b"\x00" * 8)


def test_attachment_url_presigns_only_a_stored_attachment(staged, monkeypatch):
    calls = []
    monkeypatch.setattr(
        messaging.hermes_profiles, "presign_owner_object", lambda key, **kw: calls.append((key, kw)) or "https://signed"
    )
    att = str(uuid.uuid4())
    with pytest.raises(messaging.ApiProblem):
        messaging.attachment_url(OWNER, ROOM, att)
    staged[messaging.attachment_key(OWNER, ROOM, f"{att}.mp4")] = MP4
    messaging.adopt_uploaded_attachment(OWNER, ROOM, att, "hero.mp4", "video/mp4")

    assert messaging.attachment_url(OWNER, ROOM, att) == "https://signed"
    key, kw = calls[0]
    assert key == messaging.attachment_key(OWNER, ROOM, f"{att}.mp4")
    assert kw["content_type"] == "video/mp4" and kw["disposition"].startswith("inline;")

    # HTML never renders from the bucket host either: it downloads, as the byte route serves it.
    page = str(uuid.uuid4())
    staged[messaging.attachment_key(OWNER, ROOM, f"{page}.json")] = (
        messaging.MessageAttachment(id=uuid.UUID(page), file_name="report.html", content_type="text/html", size=12)
        .model_dump_json(by_alias=True)
        .encode()
    )
    messaging.attachment_url(OWNER, ROOM, page)
    key, kw = calls[-1]
    assert key == messaging.attachment_key(OWNER, ROOM, f"{page}.html")
    assert kw["content_type"] == "text/html" and kw["disposition"].startswith("attachment;")
