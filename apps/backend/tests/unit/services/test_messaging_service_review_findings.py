import uuid
import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from core.services import messaging
from routers.messaging import get_room_attachment, upload_room_attachment

from core.auth import AuthContext
from core.services.messaging import (
    ApiProblem,
    CreateRoomMember,
    CreateRoomRequest,
    CreateMessageRequest,
    MessageAttachment,
    validate_attachment_bytes,
    create_room,
    create_human_message,
    get_agent_receipt,
    mark_room_unread,
)


@pytest.fixture
def owner_objects(monkeypatch):
    """An in-memory stand-in for the owner-state bucket, keyed by absolute S3 key."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(messaging.hermes_profiles, "put_owner_object", lambda key, body: store.__setitem__(key, body))
    monkeypatch.setattr(messaging.hermes_profiles, "get_owner_object", store.get)
    return store


@pytest.mark.asyncio
async def test_first_message_keeps_normal_dispatch_then_schedules_onboarding_capture(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    room_id = str(uuid.uuid4())
    client_id = uuid.uuid4()
    members = [
        {"kind": "human", "id": auth.user_id},
        {"kind": "agent", "id": "ceo", "listenMode": "observe"},
        {"kind": "agent", "id": "specialist", "listenMode": "mentions"},
    ]
    created = {
        "roomId": room_id,
        "messageId": str(uuid.uuid4()),
        "messageSeq": 2,
        "roomRevision": 1,
        "author": {"kind": "human", "id": auth.user_id, "displayName": "Owner"},
        "text": "Build patient scheduling",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": "2026-09-11T00:00:00Z",
        "runId": None,
        "dispatchTargets": [{"agentId": "specialist", "eventKind": "user_request"}],
        "attachments": [],
    }
    create = AsyncMock(return_value=(created, False))
    capture = AsyncMock()
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=({"kind": "channel", "ownerId": "org_1"}, {}))
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=members[2]))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.container_repo, "touch_last_interactive", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", capture)

    await create_human_message(
        auth,
        room_id,
        CreateMessageRequest(
            clientMessageId=client_id,
            text="Build patient scheduling",
            mentions=[{"kind": "agent", "id": "specialist"}],
            replyToMessageId=None,
            threadRootMessageId=None,
        ),
    )
    await asyncio.sleep(0)

    assert create.await_args.kwargs["dispatch_targets"] == [{"agentId": "specialist", "eventKind": "user_request"}]
    capture.assert_awaited_once_with("org_1", created)


@pytest.mark.asyncio
async def test_a_human_message_opening_with_an_agents_name_is_dispatched_to_it(monkeypatch):
    """Prod 2026-09-27: 'Hey Friday, ...' without the @ was ambient, and Pulse answered it."""
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    members = [
        {"kind": "human", "id": auth.user_id},
        {"kind": "agent", "id": "friday", "displayName": "Friday", "listenMode": "active"},
        {"kind": "agent", "id": "pulse", "displayName": "Pulse", "listenMode": "active"},
    ]
    create = AsyncMock(return_value=({"messageSeq": 2}, True))  # replayed: skips telemetry
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=({"kind": "channel", "ownerId": "org_1"}, {}))
    )
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging, "canonical_message", lambda item: item)
    monkeypatch.setattr(messaging.container_repo, "touch_last_interactive", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", AsyncMock())
    monkeypatch.setattr(messaging.messaging_repo, "list_messages_before", AsyncMock(return_value=[]))
    # Routing v2 §3.1: "Friday" is a weekday, so no fast path; the addressing stage's Jev call labels it.
    monkeypatch.setattr(
        "core.services.jev.decide",
        AsyncMock(
            return_value={
                "complete": {"probability": 0.95},
                "anyone": {"probability": 0.1},
                "n0": {"choice": "addressed"},
            }
        ),
    )

    await create_human_message(
        auth,
        str(uuid.uuid4()),
        CreateMessageRequest(
            clientMessageId=uuid.uuid4(),
            text="Hey Friday, the browser should be live now",
            mentions=[],
            replyToMessageId=None,
            threadRootMessageId=None,
        ),
    )
    await asyncio.sleep(0)

    assert create.await_args.kwargs["dispatch_targets"] == [{"agentId": "friday", "eventKind": "user_request"}]


@pytest.mark.asyncio
async def test_reaction_rejects_foreign_room_message_and_invalid_emoji(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, {})))
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None))
    with pytest.raises(ApiProblem) as exc:
        await messaging.set_reaction(
            auth, str(uuid.uuid4()), str(uuid.uuid4()), messaging.SetReactionRequest(emoji="👍", active=True)
        )
    assert exc.value.error.code == "MESSAGE_NOT_FOUND"
    with pytest.raises(ValueError):
        messaging.SetReactionRequest(emoji="not-an-emoji", active=True)


@pytest.mark.asyncio
async def test_update_rejects_missing_message_and_invalid_text(monkeypatch):
    auth = AuthContext(user_id="author", org_id="org_1")
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, {})))
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None))
    request = messaging.UpdateMessageRequest(operationId=uuid.uuid4(), expectedRevision=0, text="edit")
    with pytest.raises(ApiProblem) as exc:
        await messaging.update_message(auth, str(uuid.uuid4()), str(uuid.uuid4()), request)
    assert exc.value.error.code == "MESSAGE_NOT_FOUND"
    with pytest.raises(ValueError):
        messaging.UpdateMessageRequest(operationId=uuid.uuid4(), expectedRevision=-1, text="edit")
    with pytest.raises(ValueError):
        messaging.UpdateMessageRequest(operationId=uuid.uuid4(), expectedRevision=0, text="é" * 50_001)


def test_public_tombstone_hides_original_attachment_and_preserves_thread_links():
    from core.services.openclaw_work import _public_message

    room_id, message_id, parent_id, attachment_id = [str(uuid.uuid4()) for _ in range(4)]
    item = {
        "roomId": room_id,
        "messageId": message_id,
        "messageSeq": 2,
        "roomRevision": 1,
        "author": {"kind": "human", "id": "author", "displayName": "Author"},
        "text": "private original",
        "mentions": [{"kind": "agent", "id": "agent"}],
        "replyToMessageId": parent_id,
        "threadRootMessageId": parent_id,
        "createdAt": "2026-09-10T00:00:00Z",
        "runId": None,
        "editedAt": "2026-09-10T00:01:00Z",
        "deletedAt": "2026-09-10T00:01:00Z",
        "editedText": None,
        "messageRevision": 1,
        "attachments": [{"id": attachment_id, "fileName": "photo.png", "contentType": "image/png", "size": 8}],
    }
    public = messaging.canonical_message(item).model_dump_json(by_alias=True)
    assert "private original" not in public and attachment_id not in public and "photo.png" not in public
    assert messaging.canonical_message(item).thread_root_message_id == uuid.UUID(parent_id)
    assert _public_message(item)["text"] == "private original"
    assert _public_message(item)["attachments"][0]["id"] == attachment_id


@pytest.mark.asyncio
async def test_mark_unread_requires_membership_and_uses_join_floor(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    room_id = str(uuid.uuid4())
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"roomId": room_id}, {"joinedSeq": 7})),
    )
    mark = AsyncMock(return_value=7)
    monkeypatch.setattr(messaging.messaging_repo, "mark_unread", mark)

    response = await mark_room_unread(auth, room_id)

    mark.assert_awaited_once_with(room_id, "user_owner", joined_seq=7, from_seq=None)
    assert response.last_read_seq == 7


@pytest.mark.asyncio
async def test_get_agent_receipt_maps_malformed_event_id_to_400_not_500():
    with pytest.raises(ApiProblem) as exc_info:
        await get_agent_receipt("user_owner", "bad/event/id")

    assert exc_info.value.status_code == 400
    assert exc_info.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_create_room_over_member_limit_returns_409_not_a_pydantic_400():
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    members = [CreateRoomMember(kind="human", id=f"user_{i}", role="member", listen_mode=None) for i in range(32)]
    request = CreateRoomRequest(client_room_id=uuid.uuid4(), name=None, kind="group-dm", members=members)

    with pytest.raises(ApiProblem) as exc_info:
        await create_room(auth, request)

    assert exc_info.value.status_code == 409
    assert exc_info.value.error.code == "MEMBER_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_create_room_refuses_a_dm_that_is_not_exactly_two_parties(count):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    members = [CreateRoomMember(kind="human", id=f"user_{i}", role="member", listen_mode=None) for i in range(count)]
    request = CreateRoomRequest(client_room_id=uuid.uuid4(), name=None, kind="dm", members=members)

    with pytest.raises(ApiProblem) as exc_info:
        await create_room(auth, request)

    assert exc_info.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_create_room_reuses_an_existing_dm_instead_of_making_a_duplicate(monkeypatch):
    """Regression: the generic create-conversation dialog (kind="dm") posted
    straight to this endpoint with no reuse check, unlike the roster-row click
    path's client-side `resolveDm` (packages/shared/src/messaging/useRooms.ts)
    -- so picking the same agent through both paths minted two ROOM# partitions
    that both rendered as an identical "Friday · Agent" row in Direct messages."""
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    existing_meta = {"roomId": "room-existing", "kind": "dm", "ownerId": "org_1"}
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_rooms_for_member",
        AsyncMock(return_value=[{"member": {"kind": "agent", "id": "friday"}, "meta": existing_meta}]),
    )
    monkeypatch.setattr(
        messaging.messaging_repo,
        "get_member",
        AsyncMock(return_value={"kind": "human", "id": auth.user_id}),
    )
    detail = object()
    room_detail = AsyncMock(return_value=detail)
    monkeypatch.setattr(messaging, "_room_detail", room_detail)
    create = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)

    request = CreateRoomRequest(
        client_room_id=uuid.uuid4(),
        name=None,
        kind="dm",
        members=[CreateRoomMember(kind="agent", id="friday", role="member", listen_mode="active")],
    )
    result = await create_room(auth, request)

    assert result is detail
    room_detail.assert_awaited_once_with(existing_meta, auth.user_id)
    create.assert_not_awaited()


def _dm_create_stubs(monkeypatch, *, live_member=True, kind="dm"):
    """Both racing creates miss the GSI; the repo's pair row lets only the first through."""
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(
        messaging, "_resolved_member", AsyncMock(side_effect=lambda _o, m, _r: {"kind": m.kind, "id": m.id})
    )
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value={"active": True}))
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(side_effect=lambda meta, _u, *a: meta["roomId"]))
    pairs: dict[str, str] = {}
    rooms: dict[str, dict] = {}

    async def fake_create(*, owner_id, room_id, dm_pair, dm_pair_replaces, kind, **_):
        await asyncio.sleep(0)
        if dm_pair in pairs and pairs[dm_pair] != dm_pair_replaces:
            raise messaging.messaging_repo.DmPairTaken(pairs[dm_pair])
        pairs[dm_pair] = room_id
        rooms[room_id] = {"roomId": room_id, "kind": kind}
        return rooms[room_id]

    monkeypatch.setattr(messaging.messaging_repo, "create_room", fake_create)
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: {**rooms[r], "kind": kind})
    )
    monkeypatch.setattr(
        messaging.messaging_repo, "get_member", AsyncMock(return_value={"role": "member"} if live_member else None)
    )
    return rooms


def _dm_request():
    return CreateRoomRequest(
        client_room_id=uuid.uuid4(),
        name=None,
        kind="dm",
        members=[CreateRoomMember(kind="agent", id="friday", role="member", listen_mode="active")],
    )


@pytest.mark.asyncio
async def test_two_simultaneous_dm_creates_return_one_room(monkeypatch):
    rooms = _dm_create_stubs(monkeypatch)
    auth = AuthContext(user_id="user_owner", org_id="org_1")

    first, second = await asyncio.gather(create_room(auth, _dm_request()), create_room(auth, _dm_request()))

    assert first == second
    assert list(rooms) == [first]


@pytest.mark.asyncio
async def test_a_pair_row_naming_a_dm_that_became_a_group_dm_is_taken_over(monkeypatch):
    rooms = _dm_create_stubs(monkeypatch, kind="group-dm")
    auth = AuthContext(user_id="user_owner", org_id="org_1")

    first = await create_room(auth, _dm_request())
    second = await create_room(auth, _dm_request())

    assert first != second and set(rooms) == {first, second}


@pytest.mark.asyncio
async def test_find_existing_dm_never_reuses_a_dm_belonging_to_a_different_human(monkeypatch):
    """Two different humans in the same workspace can each DM the same agent --
    `list_rooms_for_member` is owner-scoped, not creator-scoped (discovery
    only), so a candidate DM must be rechecked against the CALLER's own
    strongly consistent MEMBER# row before it's trusted, or Human B's create
    would silently resolve to Human A's private DM with the agent."""
    other_humans_dm = {"roomId": "room-other-human", "kind": "dm", "ownerId": "org_1"}
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_rooms_for_member",
        AsyncMock(return_value=[{"member": {"kind": "agent", "id": "friday"}, "meta": other_humans_dm}]),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))

    found = await messaging._find_existing_dm(
        "org_1", "user_b", CreateRoomMember(kind="agent", id="friday", role="member", listen_mode="active")
    )

    assert found is None


def test_attachment_only_message_is_valid_and_empty_message_is_not():
    request = CreateMessageRequest(
        clientMessageId=uuid.uuid4(),
        text="",
        mentions=[],
        replyToMessageId=None,
        threadRootMessageId=None,
        attachmentIds=[uuid.uuid4()],
    )
    assert len(request.attachment_ids) == 1
    with pytest.raises(ValueError):
        CreateMessageRequest(
            clientMessageId=uuid.uuid4(),
            text="",
            mentions=[],
            replyToMessageId=None,
            threadRootMessageId=None,
            attachmentIds=[],
        )


def test_image_validation_rejects_spoofed_and_svg_uploads():
    assert validate_attachment_bytes("image/png", b"\x89PNG\r\n\x1a\nrest") == ".png"
    with pytest.raises(ValueError):
        validate_attachment_bytes("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(ValueError):
        validate_attachment_bytes("image/svg+xml", b"<svg/>")


def test_pdf_validation_accepts_valid_bytes_rejects_spoofed_and_oversized():
    assert validate_attachment_bytes("application/pdf", b"%PDF-1.7\nrest") == ".pdf"
    with pytest.raises(ValueError):
        validate_attachment_bytes("application/pdf", b"not actually a pdf")
    oversized = b"%PDF-1.7\n" + b"0" * (messaging.PDF_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        validate_attachment_bytes("application/pdf", oversized)


def test_zip_validation_accepts_container_signatures_without_extracting_and_rejects_spoofing():
    for signature in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        assert validate_attachment_bytes("application/zip", signature + b"opaque") == ".zip"
    with pytest.raises(ValueError):
        validate_attachment_bytes("application/zip", b"MZ executable")
    oversized = b"PK\x03\x04" + b"0" * (messaging.ZIP_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        validate_attachment_bytes("application/zip", oversized)


@pytest.mark.parametrize(
    "content_type,suffix,max_bytes_attr",
    [
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx", "OFFICE_MAX_BYTES"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx", "OFFICE_MAX_BYTES"),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx", "OFFICE_MAX_BYTES"),
    ],
)
def test_office_validation_accepts_zip_signature_rejects_spoofed_and_oversized(content_type, suffix, max_bytes_attr):
    # Office (OOXML) formats are zip containers -- same signature check as
    # application/zip, own extension, same "outer container only" posture.
    for signature in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        assert validate_attachment_bytes(content_type, signature + b"opaque") == suffix
    with pytest.raises(ValueError):
        validate_attachment_bytes(content_type, b"not actually a zip")
    cap = getattr(messaging, max_bytes_attr)
    oversized = b"PK\x03\x04" + b"0" * (cap + 1)
    with pytest.raises(ValueError):
        validate_attachment_bytes(content_type, oversized)


@pytest.mark.parametrize(
    "content_type,suffix",
    [("text/csv", ".csv"), ("text/plain", ".txt"), ("text/markdown", ".md"), ("text/html", ".html")],
)
def test_text_validation_accepts_utf8_rejects_invalid_bytes_and_oversized(content_type, suffix):
    # No fixed byte signature for plain text -- the check is that the bytes
    # decode as UTF-8 at all, the natural analog of a signature for text.
    assert validate_attachment_bytes(content_type, "hello, world — 日本語".encode()) == suffix
    with pytest.raises(ValueError):
        validate_attachment_bytes(content_type, b"\xff\xfe not valid utf-8 \x80\x81")
    oversized = b"a" * (messaging.TEXT_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        validate_attachment_bytes(content_type, oversized)


def test_zip_mime_aliases_and_safe_extension_fallback_canonicalize_but_mismatches_fail():
    for declared in ("application/zip", "application/x-zip-compressed", "application/octet-stream", ""):
        assert messaging.canonical_attachment_content_type("bundle.ZIP", declared) == "application/zip"
    with pytest.raises(ValueError, match="extension"):
        messaging.canonical_attachment_content_type("bundle.zip", "application/pdf")
    with pytest.raises(ValueError, match=".zip filename"):
        messaging.canonical_attachment_content_type("setup.exe", "application/zip")


@pytest.mark.asyncio
async def test_unsupported_file_upload_reproduces_clear_api_400_at_attachment_validation(monkeypatch):
    # .txt used to be the unsupported example here; it's now an allowed type
    # (office/text attachment support), so this uses a genuinely unsupported
    # one instead -- the test's actual point (a clean 400 AT ATTACHMENT
    # VALIDATION, not some other error) is unchanged. Needs a real UUID
    # room_id: an unsupported type is now the ONLY thing that stops the
    # request before it reaches attachment_key's own UUID parse of room_id.
    auth = AuthContext(user_id="user", org_id="owner")
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"ownerId": "owner"}, {"kind": "human", "id": "user"})),
    )
    upload = UploadFile(filename="setup.exe", file=BytesIO(b"MZ"), headers={"content-type": "application/x-msdownload"})

    with pytest.raises(ApiProblem) as exc_info:
        await upload_room_attachment(str(uuid.uuid4()), upload, auth)

    assert exc_info.value.status_code == 400
    assert exc_info.value.error.code == "INVALID_REQUEST"
    assert (
        "PNG, JPEG, WebP, GIF, PDF, ZIP, XLSX, DOCX, PPTX, CSV, TXT, MD, HTML, MP4, WebM, or MOV"
        in exc_info.value.error.message
    )


def test_message_attachment_enforces_per_type_size_cap():
    MessageAttachment(
        id=uuid.uuid4(), file_name="doc.pdf", content_type="application/pdf", size=messaging.PDF_MAX_BYTES
    )
    with pytest.raises(ValueError):
        MessageAttachment(
            id=uuid.uuid4(), file_name="doc.pdf", content_type="application/pdf", size=messaging.PDF_MAX_BYTES + 1
        )
    with pytest.raises(ValueError):
        MessageAttachment(
            id=uuid.uuid4(), file_name="big.png", content_type="image/png", size=messaging.IMAGE_MAX_BYTES + 1
        )


def test_attachment_filename_matches_plugin_utf8_contract(owner_objects):
    attachment = messaging.store_attachment(
        "owner", str(uuid.uuid4()), "🙂" * 200 + "\x7f.png", "image/png", b"\x89PNG\r\n\x1a\nrest"
    )
    assert len(attachment.file_name.encode("utf-8")) <= 255
    assert "/" not in attachment.file_name and "\\" not in attachment.file_name and "\x7f" not in attachment.file_name


def test_attachment_load_rejects_metadata_id_mismatch(owner_objects):
    room_id = str(uuid.uuid4())
    attachment = messaging.store_attachment("owner", room_id, "proof.png", "image/png", b"\x89PNG\r\n\x1a\nrest")
    key = messaging.attachment_key("owner", room_id, f"{attachment.id}.json")
    owner_objects[key] = owner_objects[key].replace(str(attachment.id).encode(), str(uuid.uuid4()).encode())
    with pytest.raises(ApiProblem):
        messaging.load_attachment("owner", room_id, str(attachment.id))


def test_attachment_keys_are_scoped_to_the_owners_hermes_home_and_room(owner_objects):
    """The wipe deletes everything under ``<owner>/hermes/``, so attachments have to live
    there or they orphan; and every path segment is a parsed UUID, so one room's key can
    never name another's."""
    room_id = str(uuid.uuid4())
    attachment = messaging.store_attachment("owner", room_id, "proof.png", "image/png", b"\x89PNG\r\n\x1a\nrest")

    assert all(key.startswith(f"owner/hermes/attachments/{room_id}/") for key in owner_objects)
    assert set(owner_objects) == {
        messaging.attachment_key("owner", room_id, f"{attachment.id}.png"),
        messaging.attachment_key("owner", room_id, f"{attachment.id}.json"),
    }
    with pytest.raises(ValueError):
        messaging.attachment_key("owner", "../other", "x.png")


def test_pdf_attachment_round_trips_through_store_and_load(owner_objects):
    room_id = str(uuid.uuid4())
    pdf_bytes = b"%PDF-1.7\n" + b"content" * 100
    stored = messaging.store_attachment("owner", room_id, "contract.pdf", "application/pdf", pdf_bytes)
    assert stored.content_type == "application/pdf"

    loaded, data = messaging.load_attachment("owner", room_id, str(stored.id))
    assert data == pdf_bytes
    assert loaded.file_name == "contract.pdf"


def test_zip_attachment_round_trips_with_canonical_persisted_mime(owner_objects):
    room_id = str(uuid.uuid4())
    zip_bytes = b"PK\x03\x04" + b"opaque archive bytes"
    stored = messaging.store_attachment("owner", room_id, "bundle.zip", "application/x-zip-compressed", zip_bytes)
    assert stored.content_type == "application/zip"

    loaded, data = messaging.load_attachment("owner", room_id, str(stored.id))
    assert loaded.content_type == "application/zip"
    assert loaded.file_name == "bundle.zip"
    assert data == zip_bytes


def test_pdf_attachment_store_rejects_oversized_upload(owner_objects):
    room_id = str(uuid.uuid4())
    oversized = b"%PDF-1.7\n" + b"0" * (messaging.PDF_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        messaging.store_attachment("owner", room_id, "big.pdf", "application/pdf", oversized)


@pytest.mark.asyncio
async def test_attachment_download_checks_current_room_membership(monkeypatch):
    checked = False

    async def deny(owner_id, user_id, room_id):
        nonlocal checked
        checked = True
        raise ApiProblem("ROOM_FORBIDDEN", "No access")

    monkeypatch.setattr(messaging, "require_room_member", deny)
    monkeypatch.setattr(messaging, "load_attachment", lambda *_: pytest.fail("loaded before membership check"))
    with pytest.raises(ApiProblem):
        await get_room_attachment(str(uuid.uuid4()), str(uuid.uuid4()), AuthContext(user_id="user", org_id="owner"))
    assert checked


@pytest.mark.asyncio
async def test_zip_download_is_authorized_and_forced_to_safe_attachment_disposition(monkeypatch):
    room_id = str(uuid.uuid4())
    attachment_id = uuid.uuid4()
    attachment = MessageAttachment(
        id=attachment_id, file_name="quarterly report.zip", content_type="application/zip", size=8
    )
    require_member = AsyncMock(return_value=({"ownerId": "owner"}, {"kind": "human", "id": "user"}))
    monkeypatch.setattr(messaging, "require_room_member", require_member)
    monkeypatch.setattr(messaging, "load_attachment", lambda *_: (attachment, b"PK\x03\x04data"))

    response = await get_room_attachment(room_id, str(attachment_id), AuthContext(user_id="user", org_id="owner"))

    require_member.assert_awaited_once()
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''quarterly%20report.zip"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_patch_room_me_writes_only_named_prefs_and_notifies_the_caller_only(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 4}
    member = {"kind": "human", "id": "user_owner", "role": "owner", "joinedSeq": 0}
    set_prefs = AsyncMock(return_value={**member, "muted": True})
    detail = AsyncMock(return_value="detail")
    publish_user = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, member)))
    monkeypatch.setattr(messaging.messaging_repo, "set_member_prefs", set_prefs)
    monkeypatch.setattr(messaging, "_room_detail", detail)
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_user": publish_user})())

    result = await messaging.patch_room_me(auth, "room-1", messaging.MemberPrefsRequest(muted=True))

    assert result == "detail"
    set_prefs.assert_awaited_once_with("room-1", "human", "user_owner", {"muted": True})
    publish_user.assert_awaited_once()
    owner_arg, user_arg, event = publish_user.await_args.args
    assert (owner_arg, user_arg) == ("org_1", "user_owner")
    assert event["type"] == "room.member.updated"
    assert event["payload"] == {"roomId": "room-1"}


@pytest.mark.asyncio
async def test_patch_room_me_clears_a_section_with_an_explicit_null(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 4}
    member = {"kind": "human", "id": "user_owner", "role": "owner", "joinedSeq": 0}
    set_prefs = AsyncMock(return_value=member)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, member)))
    monkeypatch.setattr(messaging.messaging_repo, "set_member_prefs", set_prefs)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_user": AsyncMock()})())

    await messaging.patch_room_me(auth, "room-1", messaging.MemberPrefsRequest.model_validate({"sectionId": None}))
    set_prefs.assert_awaited_once_with("room-1", "human", "user_owner", {"sectionId": None})


@pytest.mark.asyncio
async def test_room_member_updated_is_a_recognised_user_live_event():
    assert "room.member.updated" in messaging.USER_LIVE_EVENT_TYPES
    assert "room.member.updated" in messaging.LIVE_EVENT_TYPES
    assert "room.member.updated" not in messaging.ROOM_LIVE_EVENT_TYPES


@pytest.mark.asyncio
async def test_put_sections_persists_and_returns_the_whole_sidebar(monkeypatch):
    auth = AuthContext(user_id="user_owner", org_id="org_1")
    section_id = "33333333-3333-4333-8333-333333333333"
    put = AsyncMock(return_value=[{"id": section_id, "name": "Ship"}])
    monkeypatch.setattr(messaging.messaging_repo, "put_user_sections", put)
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging.messaging_repo, "get_user_prefs", AsyncMock(return_value=None))

    result = await messaging.put_sections(
        auth, messaging.SectionsRequest(sections=[messaging.RoomSection(id=section_id, name="Ship")])
    )

    assert [str(section.id) for section in result.sections] == [section_id]
    assert result.rooms == []
    put.assert_awaited_once_with("org_1", "user_owner", [{"id": section_id, "name": "Ship"}])


def test_sections_request_rejects_too_many_sections_and_blank_names():
    section_id = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(ValueError):
        messaging.SectionsRequest(
            sections=[messaging.RoomSection(id=section_id, name="Ship")] * (messaging.MAX_SECTIONS + 1)
        )
    with pytest.raises(ValueError):
        messaging.RoomSection(id=section_id, name="")
    with pytest.raises(ValueError):
        messaging.RoomSection(id=section_id, name="   ")
    with pytest.raises(ValueError):
        messaging.RoomSection(id=section_id, name="x" * (messaging.SECTION_NAME_MAX_CHARS + 1))


def test_sections_request_rejects_duplicate_section_ids():
    section_id = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(ValueError):
        messaging.SectionsRequest(
            sections=[
                messaging.RoomSection(id=section_id, name="Ship"),
                messaging.RoomSection(id=section_id, name="Admin"),
            ]
        )


@pytest.mark.asyncio
async def test_patch_room_sets_topic_alone_and_rejects_renaming_a_dm(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    group_meta = {"roomId": "group", "ownerId": "org_1", "kind": "group-dm", "revision": 2}
    require_admin = AsyncMock(return_value=(group_meta, {"role": "admin"}))
    patch_meta = AsyncMock(return_value={**group_meta, "topic": "Ship v2"})
    monkeypatch.setattr(messaging, "require_room_member", require_admin)
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())

    assert await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(topic="Ship v2")) == "detail"
    patch_meta.assert_awaited_once_with("group", {"topic": "Ship v2"})

    require_admin.return_value = ({"roomId": "dm", "ownerId": "org_1", "kind": "dm"}, {"role": "owner"})
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "dm", messaging.PatchRoomRequest(name="Private label"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert "cannot be renamed" in exc.value.error.message
    assert patch_meta.await_count == 1


@pytest.mark.asyncio
async def test_patch_room_keeps_the_admin_check_in_front_of_persistence(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    group_meta = {"roomId": "group", "ownerId": "org_1", "kind": "group-dm", "revision": 1}
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(group_meta, {"role": "member"})))
    patch_meta = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Denied"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    # Archive + rename together is still a rename: the member exemption is archive-only.
    with pytest.raises(ApiProblem):
        await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Denied", archived=True))
    patch_meta.assert_not_awaited()


def test_room_topic_validation_trims_and_caps():
    assert messaging.validate_room_topic("  Ship v2  ") == "Ship v2"
    assert messaging.validate_room_topic("   ") is None
    assert messaging.validate_room_topic(None) is None
    with pytest.raises(ValueError):
        messaging.PatchRoomRequest(topic="x" * (messaging.ROOM_TOPIC_MAX_CHARS + 1))


@pytest.mark.asyncio
async def test_leave_room_removes_a_non_owner_human_and_refuses_the_owner(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 3}
    remove = AsyncMock(return_value=meta)
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"})),
    )
    monkeypatch.setattr(messaging, "_remove_member_with_notices", remove)

    await messaging.leave_room(auth, "room-1")
    remove.assert_awaited_once_with(meta, "human", "member")

    # A channel owner alone in the room must delete it instead.
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "owner"})),
    )
    members = [{"kind": "human", "id": "member", "role": "owner"}, {"kind": "agent", "id": "ceo", "role": "member"}]
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(side_effect=lambda _room: members))
    with pytest.raises(ApiProblem) as exc:
        await messaging.leave_room(auth, "room-1")
    assert exc.value.error.code == "INVALID_REQUEST"
    assert remove.await_count == 1

    # With other people there, the owner leaves through the same rotating removal.
    members.append({"kind": "human", "id": "late", "role": "member", "joinedAt": "2026-10-02"})
    await messaging.leave_room(auth, "room-1")
    remove.assert_awaited_with(meta, "human", "member")

    # A group DM's creator holds no powers there, so they may leave it -- through the same
    # rotating removal path as everyone else.
    group = {**meta, "kind": "group-dm"}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(group, {"kind": "human", "id": "member", "role": "owner"})),
    )
    await messaging.leave_room(auth, "room-1")
    remove.assert_awaited_with(group, "human", "member")


def test_a_channel_owner_hands_the_channel_to_an_admin_then_the_earliest_joiner():
    channel = {"kind": "channel"}
    owner = {"kind": "human", "id": "owner", "role": "owner"}
    late = {"kind": "human", "id": "late", "role": "member", "joinedAt": "2026-10-02"}
    early = {"kind": "human", "id": "early", "role": "member", "joinedAt": "2026-10-01"}
    admin = {"kind": "human", "id": "admin", "role": "admin", "joinedAt": "2026-10-03"}
    agent = {"kind": "agent", "id": "ceo", "role": "member"}
    assert messaging._channel_heir(channel, [owner, agent], "owner") is None
    assert messaging._channel_heir(channel, [owner, late, early], "owner") == "early"
    assert messaging._channel_heir(channel, [owner, late, early, admin], "owner") == "admin"
    assert messaging._channel_heir(channel, [owner, late], "late") is None  # not the owner
    assert messaging._channel_heir({"kind": "group-dm"}, [owner, late], "owner") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "others,heir", [([], None), ([{"kind": "human", "id": "bob", "role": "member", "joinedAt": "1"}], "bob")]
)
async def test_removing_a_channel_owner_hands_it_on_or_still_removes_them_alone(monkeypatch, others, heir):
    # A workspace removal reaches this seam without leave_room's refusal: it must still
    # remove the owner, with the key rotation, whether or not anyone can take the channel.
    meta = {"roomId": "11111111-1111-4111-8111-111111111111", "ownerId": "org_1", "kind": "channel", "revision": 3}
    members = [{"kind": "human", "id": "owner", "role": "owner"}, {"kind": "agent", "id": "ceo"}, *others]
    remove = AsyncMock(return_value=meta)
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="key"))
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", remove)
    monkeypatch.setattr(messaging.livekit_huddles, "remove_participant", AsyncMock())

    await messaging._remove_member_with_notices(meta, "human", "owner")

    kwargs = remove.await_args.kwargs
    assert kwargs["member_id"] == "owner" and kwargs["new_delivery_key"]
    assert kwargs["new_owner_id"] == heir


@pytest.mark.asyncio
async def test_set_pin_allows_any_member_validates_the_message_and_publishes_metadata(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 2}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"})),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"messageId": "m-1"}))
    pin = AsyncMock(return_value={**meta, "pinnedMessageIds": ["m-1"], "revision": 3})
    publish = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "set_pin", pin)
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))

    assert await messaging.set_pin(auth, "room-1", "m-1", True) == "detail"
    pin.assert_awaited_once_with("room-1", "m-1", True)
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_pin_rejects_a_foreign_message_and_maps_the_cap_to_a_problem(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 2}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"})),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    with pytest.raises(ApiProblem) as missing:
        await messaging.set_pin(auth, "room-1", "m-9", True)
    assert missing.value.error.code == "MESSAGE_NOT_FOUND"

    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"messageId": "m-1"}))
    monkeypatch.setattr(
        messaging.messaging_repo,
        "set_pin",
        AsyncMock(side_effect=messaging.messaging_repo.PinLimitExceeded("too many")),
    )
    with pytest.raises(ApiProblem) as capped:
        await messaging.set_pin(auth, "room-1", "m-1", True)
    assert capped.value.error.code == "PIN_LIMIT"


@pytest.mark.asyncio
async def test_set_pin_publishes_only_when_the_repo_actually_changed_pins(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "room-1", "ownerId": "org_1", "kind": "channel", "revision": 2}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "member"})),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"messageId": "m-1"}))
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))

    # Already pinned: the repo returns META unchanged (same revision) -- no live event.
    monkeypatch.setattr(
        messaging.messaging_repo, "set_pin", AsyncMock(return_value={**meta, "pinnedMessageIds": ["m-1"]})
    )
    assert await messaging.set_pin(auth, "room-1", "m-1", True) == "detail"
    publish.assert_not_awaited()

    # A new pin: the repo bumps the revision -- publish once.
    monkeypatch.setattr(
        messaging.messaging_repo,
        "set_pin",
        AsyncMock(return_value={**meta, "pinnedMessageIds": ["m-1", "m-2"], "revision": 3}),
    )
    assert await messaging.set_pin(auth, "room-1", "m-2", True) == "detail"
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_room_unread_threads_from_seq_and_defaults_to_the_whole_room(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    room_id = str(uuid.uuid4())
    member = {"kind": "human", "id": "member", "role": "member", "joinedSeq": 2}
    mark = AsyncMock(return_value=4)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": room_id}, member)))
    monkeypatch.setattr(messaging.messaging_repo, "mark_unread", mark)

    await messaging.mark_room_unread(auth, room_id, messaging.MarkUnreadRequest(from_seq=5))
    assert mark.await_args.kwargs == {"joined_seq": 2, "from_seq": 5}

    await messaging.mark_room_unread(auth, room_id)
    assert mark.await_args.kwargs == {"joined_seq": 2, "from_seq": None}


def test_mark_unread_request_rejects_a_non_positive_sequence():
    with pytest.raises(ValueError):
        messaging.MarkUnreadRequest(from_seq=0)


def _archive_fixture(monkeypatch, meta, role="admin"):
    require_member = AsyncMock(return_value=(meta, {"role": role}))
    patch_meta = AsyncMock(side_effect=lambda room_id, updates: {**meta, **updates})
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "require_room_member", require_member)
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=False))  # no Lightbulb seated
    monkeypatch.setattr(messaging.messaging_repo, "pop_paused_ask", AsyncMock(return_value=None))
    return patch_meta, publish


@pytest.mark.asyncio
async def test_patch_room_archives_and_unarchives_a_channel_idempotently(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 2}
    patch_meta, publish = _archive_fixture(monkeypatch, meta)

    assert await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=True)) == "detail"
    updates = patch_meta.await_args.args[1]
    assert updates["archivedBy"] == "admin" and updates["archivedAt"]
    publish.assert_awaited_once()

    # Already archived: a no-op 200, nothing written, nothing published.
    meta["archivedAt"] = updates["archivedAt"]
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=True))
    assert patch_meta.await_count == 1 and publish.await_count == 1

    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=False))
    assert patch_meta.await_args.args[1] == {"archivedAt": None, "archivedBy": None}


@pytest.mark.asyncio
async def test_archiving_a_channel_ends_its_huddle(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 2}
    _archive_fixture(monkeypatch, meta)
    closed = AsyncMock()
    monkeypatch.setattr(messaging.livekit_huddles, "close_room", closed)
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=True))
    closed.assert_awaited_once_with("chan")
    meta["archivedAt"] = "x"
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=False))
    closed.assert_awaited_once()  # unarchive leaves LiveKit alone


@pytest.mark.asyncio
async def test_any_group_dm_member_archives_and_unarchives_it(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    meta = {"roomId": "group", "ownerId": "org_1", "kind": "group-dm", "revision": 2}
    patch_meta, publish = _archive_fixture(monkeypatch, meta, role="member")

    assert await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(archived=True)) == "detail"
    updates = patch_meta.await_args.args[1]
    assert updates["archivedBy"] == "member" and updates["archivedAt"]
    publish.assert_awaited_once()

    meta["archivedAt"] = updates["archivedAt"]
    await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(archived=False))
    assert patch_meta.await_args.args[1] == {"archivedAt": None, "archivedBy": None}


@pytest.mark.asyncio
async def test_patch_room_refuses_to_archive_a_dm(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    patch_meta, _ = _archive_fixture(
        monkeypatch, {"roomId": "r", "ownerId": "org_1", "kind": "dm", "revision": 1}, "owner"
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "r", messaging.PatchRoomRequest(archived=True))
    assert exc.value.error.code == "ROOM_NOT_ARCHIVABLE" and exc.value.status_code == 409
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_room_refuses_to_archive_general_and_the_shared_vendor_channel(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    general = {"roomId": messaging.default_room_id("org_1"), "ownerId": "org_1", "kind": "channel", "revision": 1}
    patch_meta, _ = _archive_fixture(monkeypatch, general)
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, general["roomId"], messaging.PatchRoomRequest(archived=True))
    assert exc.value.error.code == "ROOM_NOT_ARCHIVABLE"

    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "org_vendor")
    vendor = {"roomId": messaging.messaging_repo.vendor_room_id("org_1"), "ownerId": "org_1", "kind": "channel"}
    _archive_fixture(monkeypatch, {**vendor, "revision": 1})
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, vendor["roomId"], messaging.PatchRoomRequest(archived=True))
    assert exc.value.error.code == "ROOM_NOT_ARCHIVABLE"
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_room_archive_keeps_the_admin_check_in_front(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    patch_meta, _ = _archive_fixture(monkeypatch, {"roomId": "chan", "ownerId": "org_1", "kind": "channel"}, "member")
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(archived=True))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    patch_meta.assert_not_awaited()


def test_room_summary_carries_archived_fields():
    meta = {
        "roomId": str(uuid.uuid4()),
        "ownerId": "org_1",
        "kind": "channel",
        "revision": 3,
        "lastSeq": 0,
        "updatedAt": "2026-09-23T00:00:00+00:00",
        "archivedAt": "2026-09-23T00:00:00+00:00",
        "archivedBy": "admin",
    }
    fields = messaging._room_summary_fields(meta, {"joinedSeq": 0}, 0)
    summary = messaging.RoomSummary(**fields).model_dump(mode="json", by_alias=True)
    assert summary["archivedAt"].startswith("2026-09-23") and summary["archivedBy"] == "admin"


@pytest.mark.asyncio
async def test_human_and_agent_posts_to_an_archived_room_are_a_409(monkeypatch):
    """Rule: once archived, NOTHING new lands -- a human send or an agent reply, even one
    answering a turn that started before the archive. The bridge treats a 4xx as final."""
    room = str(uuid.uuid4())
    meta = {"roomId": room, "ownerId": "org_1", "kind": "channel", "revision": 1}
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, {"role": "member"})))
    monkeypatch.setattr(messaging, "_validate_mentions", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "_validate_message_links", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Ann"))
    monkeypatch.setattr(
        messaging.messaging_repo, "create_message", AsyncMock(side_effect=messaging.messaging_repo.RoomArchived(room))
    )
    auth = AuthContext(user_id="u1", org_id="org_1")
    request = messaging.CreateMessageRequest.model_validate(
        {"clientMessageId": str(uuid.uuid4()), "text": "hi", "replyToMessageId": None, "threadRootMessageId": None}
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.create_human_message(auth, room, request)
    assert exc.value.error.code == "ROOM_ARCHIVED" and exc.value.status_code == 409
    assert "Unarchive it to post" in exc.value.error.message

    agent = str(uuid.uuid4())
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value={"id": agent}))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": agent, "name": "CEO"}]))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    reply = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": str(uuid.uuid4()),
            "roomId": room,
            "agentId": agent,
            "text": "late reply",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.create_agent_message("org_1", reply)
    assert exc.value.error.code == "ROOM_ARCHIVED"


@pytest.mark.asyncio
async def test_follow_up_into_an_archived_room_is_a_409(monkeypatch):
    room = str(uuid.uuid4())
    meta = {"roomId": room, "ownerId": "org_1", "kind": "channel", "archivedAt": "2026-09-23T00:00:00+00:00"}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    create = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "create_follow_up", create)
    request = messaging.FollowUpRequest.model_validate(
        {
            "roomId": room,
            "agentId": str(uuid.uuid4()),
            "followUpId": str(uuid.uuid4()),
            "triggerMessageId": str(uuid.uuid4()),
            "eventKind": "user_request",
            "reason": "delegation_completed",
            "text": "done",
        }
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.create_follow_up("org_1", request)
    assert exc.value.error.code == "ROOM_ARCHIVED"
    create.assert_not_awaited()


# --- UX slice 6: project pause, icon and colour, Instructions --------------------------------------


@pytest.mark.asyncio
async def test_pause_and_resume_a_project_are_audited(monkeypatch):
    from core.services import questions

    auth = AuthContext(user_id="admin", org_id="org_1")
    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 2}
    patch_meta, publish = _archive_fixture(monkeypatch, meta)
    audit = AsyncMock()
    monkeypatch.setattr(questions, "audit", audit)

    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(paused=True))
    updates = patch_meta.await_args.args[1]
    assert updates["pausedBy"] == "admin" and updates["pausedAt"]
    audit.assert_awaited_once_with("admin", "org_1", "project.paused", {"roomId": "chan"})
    publish.assert_awaited_once()

    meta["pausedAt"] = updates["pausedAt"]
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(paused=True))  # already paused: no-op
    assert patch_meta.await_count == 1 and audit.await_count == 1

    from core.services import admission

    answer = AsyncMock()
    monkeypatch.setattr(admission, "answer_paused_ask", answer)
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(paused=False))
    assert patch_meta.await_args.args[1] == {"pausedAt": None, "pausedBy": None}
    assert audit.await_args.args[2] == "project.resumed"
    answer.assert_awaited_once_with("chan")  # W18-8: the question asked while paused is answered now


@pytest.mark.asyncio
async def test_a_workspace_admin_may_pause_but_a_plain_member_may_not(monkeypatch):
    from core.services import questions

    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 2}
    patch_meta, _ = _archive_fixture(monkeypatch, meta, role="member")
    monkeypatch.setattr(questions, "audit", AsyncMock())
    member = AuthContext(user_id="m", org_id="org_1", org_role="org:member")
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(member, "chan", messaging.PatchRoomRequest(paused=True))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    ws_admin = AuthContext(user_id="wa", org_id="org_1", org_role="org:admin")
    with pytest.raises(ApiProblem):  # the workspace-admin pass covers pause alone, not other fields
        await messaging.patch_room(ws_admin, "chan", messaging.PatchRoomRequest(paused=True, icon="rocket"))
    patch_meta.assert_not_awaited()
    await messaging.patch_room(ws_admin, "chan", messaging.PatchRoomRequest(paused=True))
    assert patch_meta.await_args.args[1]["pausedBy"] == "wa"


@pytest.mark.asyncio
async def test_only_a_project_pauses_or_takes_an_icon(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    patch_meta, _ = _archive_fixture(monkeypatch, {"roomId": "d", "ownerId": "org_1", "kind": "dm", "revision": 1})
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "d", messaging.PatchRoomRequest(paused=True))
    assert exc.value.error.code == "ROOM_NOT_PAUSABLE"
    with pytest.raises(ApiProblem):
        await messaging.patch_room(auth, "d", messaging.PatchRoomRequest(color="project-2"))
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_icon_and_color_set_and_clear(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 2}
    patch_meta, _ = _archive_fixture(monkeypatch, meta)
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(icon="rocket", color="project-3"))
    assert patch_meta.await_args.args[1] == {"icon": "rocket", "color": "project-3"}
    await messaging.patch_room(auth, "chan", messaging.PatchRoomRequest(icon=None))
    assert patch_meta.await_args.args[1] == {"icon": None}


def test_icon_color_and_topic_are_validated():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        messaging.PatchRoomRequest(icon="skull")
    with pytest.raises(ValidationError):
        messaging.PatchRoomRequest(color="#ff0000")
    assert messaging.PatchRoomRequest(topic="x" * 2000).topic == "x" * 2000
    with pytest.raises(ValidationError):
        messaging.CreateRoomRequest(client_room_id=uuid.uuid4(), name="p", kind="channel", members=[], topic="x" * 2001)
    req = messaging.CreateRoomRequest(
        client_room_id=uuid.uuid4(), name="p", kind="channel", members=[], icon="leaf", color="project-6"
    )
    assert (req.icon, req.color) == ("leaf", "project-6")


def test_room_summary_carries_project_fields():
    meta = {
        "roomId": str(uuid.uuid4()),
        "ownerId": "org_1",
        "kind": "channel",
        "revision": 3,
        "lastSeq": 0,
        "updatedAt": "2026-11-06T00:00:00+00:00",
        "icon": "rocket",
        "color": "project-3",
        "pausedAt": "2026-11-06T00:00:00+00:00",
        "pausedBy": "admin",
    }
    summary = messaging.RoomSummary(**messaging._room_summary_fields(meta, {"joinedSeq": 0}, 0)).model_dump(
        mode="json", by_alias=True
    )
    assert summary["icon"] == "rocket" and summary["color"] == "project-3"
    assert summary["pausedAt"].startswith("2026-11-06") and summary["pausedBy"] == "admin"


@pytest.mark.asyncio
async def test_create_project_stores_topic_icon_and_color(monkeypatch):
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    create = AsyncMock(return_value={"kind": "channel"})
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    await messaging.create_room(
        AuthContext(user_id="u1", org_id="org_1"),
        messaging.CreateRoomRequest(
            client_room_id=uuid.uuid4(),
            name="Spring sale",
            kind="channel",
            members=[],
            topic="  Sell 200 loaves  ",
            icon="rocket",
            color="project-3",
        ),
    )
    assert create.await_args.kwargs["extra"] == {"topic": "Sell 200 loaves", "icon": "rocket", "color": "project-3"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group-dm", "dm"])
@pytest.mark.parametrize("field", [{"topic": "x"}, {"icon": "rocket"}, {"color": "project-1"}])
async def test_create_refuses_project_fields_off_a_channel(monkeypatch, kind, field):
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=None))  # no Lightbulb seated
    create = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)
    member = messaging.CreateRoomMember(kind="human", id="u2", role="member", listen_mode=None)
    with pytest.raises(ApiProblem) as exc:
        await messaging.create_room(
            AuthContext(user_id="u1", org_id="org_1"),
            messaging.CreateRoomRequest(client_room_id=uuid.uuid4(), name=None, kind=kind, members=[member], **field),
        )
    assert exc.value.error.code == "INVALID_REQUEST"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_memory_lists_for_members_and_only_owners_or_admins_forget(monkeypatch):
    """W15-5: Settings › Memory lists what Lightbulb noted; Forget is the project's owner/admin's."""
    auth = AuthContext(user_id="member", org_id="org_1")
    room_id, mid = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [{"messageId": mid, "fact": "Classes cap at 12 kids.", "notedAt": "2026-11-06T13:40:00+00:00"}]
    member = AsyncMock(return_value=({"ownerId": "org_1", "kind": "channel"}, {"role": "member"}))
    monkeypatch.setattr(messaging, "require_room_member", member)
    monkeypatch.setattr(messaging.messaging_repo, "list_noted", AsyncMock(return_value=rows))
    delete = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "delete_noted", delete)

    listed = await messaging.list_room_memory(auth, room_id)
    assert listed.model_dump(by_alias=True) == {
        "items": [{"messageId": mid, "fact": "Classes cap at 12 kids.", "notedAt": "2026-11-06T13:40:00+00:00"}]
    }
    with pytest.raises(ApiProblem) as exc:
        await messaging.forget_room_memory(auth, room_id, mid)
    assert exc.value.error.code == "ROOM_FORBIDDEN" and delete.await_count == 0

    member.return_value = ({"ownerId": "org_1", "kind": "channel"}, {"role": "admin"})
    await messaging.forget_room_memory(auth, room_id, mid)
    delete.assert_awaited_once_with(room_id, mid)


@pytest.mark.asyncio
async def test_room_memory_is_closed_to_non_members(monkeypatch):
    async def deny(*_args):
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")

    monkeypatch.setattr(messaging, "require_room_member", deny)
    auth = AuthContext(user_id="stranger", org_id="org_1")
    with pytest.raises(ApiProblem):
        await messaging.list_room_memory(auth, str(uuid.uuid4()))
    with pytest.raises(ApiProblem):
        await messaging.forget_room_memory(auth, str(uuid.uuid4()), str(uuid.uuid4()))


# --- Lightbulb everywhere (founder ruling, 6 Nov 2026): a DM or group chat with Lightbulb is a project --------------


def _chat_project(monkeypatch, kind, role="member", seated=True):
    meta = {"roomId": "c", "ownerId": "org_1", "kind": kind, "revision": 1}
    patch_meta, _ = _archive_fixture(monkeypatch, meta, role=role)
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=seated))
    from core.services import questions

    monkeypatch.setattr(questions, "audit", AsyncMock())
    return patch_meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_fields", [{"paused": True}, {"topic": "Ship the site"}, {"icon": "rocket", "color": "project-2"}]
)
async def test_any_group_chat_member_pauses_and_edits_its_instructions_and_icon(monkeypatch, request_fields):
    patch_meta = _chat_project(monkeypatch, "group-dm")
    await messaging.patch_room(
        AuthContext(user_id="m", org_id="org_1"), "c", messaging.PatchRoomRequest(**request_fields)
    )
    patch_meta.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_group_chat_member_still_cannot_rename_it(monkeypatch):
    patch_meta = _chat_project(monkeypatch, "group-dm")
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(AuthContext(user_id="m", org_id="org_1"), "c", messaging.PatchRoomRequest(name="x"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_dm_person_pauses_their_lightbulb_dm(monkeypatch):
    patch_meta = _chat_project(monkeypatch, "dm")
    await messaging.patch_room(AuthContext(user_id="me", org_id="org_1"), "c", messaging.PatchRoomRequest(paused=True))
    assert patch_meta.await_args.args[1]["pausedBy"] == "me"


@pytest.mark.asyncio
async def test_a_workspace_admin_never_pauses_someones_dm(monkeypatch):
    """Membership already keeps a non-member admin out; this is the rule itself, for a member without the DM role."""
    meta = {"roomId": "c", "ownerId": "org_1", "kind": "dm", "revision": 1}
    patch_meta, _ = _archive_fixture(monkeypatch, meta, role="member")
    ws_admin = AuthContext(user_id="wa", org_id="org_1", org_role="org:admin")
    with pytest.raises(ApiProblem) as exc:  # no Lightbulb seated: not a project, and the admin pass skips DMs
        await messaging.patch_room(ws_admin, "c", messaging.PatchRoomRequest(paused=True))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_chat_without_lightbulb_is_not_a_project(monkeypatch):
    patch_meta = _chat_project(monkeypatch, "group-dm", role="owner", seated=False)
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(
            AuthContext(user_id="o", org_id="org_1"), "c", messaging.PatchRoomRequest(paused=True)
        )
    assert exc.value.error.code == "ROOM_NOT_PAUSABLE"
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_channel_keeps_the_owner_admin_rule(monkeypatch):
    meta = {"roomId": "chan", "ownerId": "org_1", "kind": "channel", "revision": 1}
    patch_meta, _ = _archive_fixture(monkeypatch, meta, role="member")
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(
            AuthContext(user_id="m", org_id="org_1"), "chan", messaging.PatchRoomRequest(topic="x")
        )
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    patch_meta.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group-dm", "dm"])
async def test_create_a_chat_with_lightbulb_takes_project_fields(monkeypatch, kind):
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value="lb"))
    seated = [messaging.CreateRoomMember(kind="agent", id="lb", role="member", listen_mode="active")]
    if kind == "group-dm":
        seated.append(messaging.CreateRoomMember(kind="human", id="u2", role="member", listen_mode=None))
    monkeypatch.setattr(messaging, "_lightbulb_only", AsyncMock(return_value=seated))
    monkeypatch.setattr(messaging, "_find_existing_dm", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        messaging, "_resolved_member", AsyncMock(side_effect=lambda _o, m, _r: {"kind": m.kind, "id": m.id})
    )
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    create = AsyncMock(return_value={"kind": kind})
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    await messaging.create_room(
        AuthContext(user_id="u1", org_id="org_1"),
        messaging.CreateRoomRequest(
            client_room_id=uuid.uuid4(), name=None, kind=kind, members=seated, icon="rocket", topic="Plan the trip"
        ),
    )
    assert create.await_args.kwargs["extra"] == {"topic": "Plan the trip", "icon": "rocket"}


def _noted_fixture(monkeypatch, kind, joined, role="member"):
    meta = {"ownerId": "org_1", "kind": kind}
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, {"role": role, "joinedAt": joined}))
    )
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        {"messageId": new, "fact": "Group plan: Lisbon.", "notedAt": "2026-11-06T12:00:00+00:00"},
        {"messageId": old, "fact": "My salary is 90k.", "notedAt": "2026-11-01T09:00:00+00:00"},
    ]
    monkeypatch.setattr(messaging.messaging_repo, "list_noted", AsyncMock(return_value=rows))
    delete = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "delete_noted", delete)
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=True))  # Lightbulb seated
    return old, new, delete


@pytest.mark.asyncio
async def test_a_dm_turned_group_chat_hides_its_dm_era_notes_from_the_new_member(monkeypatch):
    """Privacy: Bob joins Ann's Lightbulb DM on 5 Nov. He never reads (or forgets) what she told it on 1 Nov."""
    old, new, delete = _noted_fixture(monkeypatch, "group-dm", "2026-11-05T00:00:00+00:00")
    bob = AuthContext(user_id="bob", org_id="org_1")
    listed = await messaging.list_room_memory(bob, str(uuid.uuid4()))
    assert [i.message_id for i in listed.items] == [new]
    room = str(uuid.uuid4())
    await messaging.forget_room_memory(bob, room, old)
    delete.assert_not_awaited()
    await messaging.forget_room_memory(bob, room, new)  # any group-chat member may forget what they can see
    delete.assert_awaited_once_with(room, new)


@pytest.mark.asyncio
async def test_the_original_dm_person_still_sees_every_note(monkeypatch):
    old, new, _ = _noted_fixture(monkeypatch, "group-dm", "2026-10-01T00:00:00+00:00")
    listed = await messaging.list_room_memory(AuthContext(user_id="ann", org_id="org_1"), str(uuid.uuid4()))
    assert [i.message_id for i in listed.items] == [new, old]


@pytest.mark.asyncio
async def test_a_channel_lists_every_note_to_a_late_joiner(monkeypatch):
    _, _, _ = _noted_fixture(monkeypatch, "channel", "2026-11-05T00:00:00+00:00")
    listed = await messaging.list_room_memory(AuthContext(user_id="late", org_id="org_1"), str(uuid.uuid4()))
    assert len(listed.items) == 2


@pytest.mark.asyncio
async def test_a_member_cannot_forget_once_lightbulb_left_the_group_chat(monkeypatch):
    _, new, delete = _noted_fixture(monkeypatch, "group-dm", "2026-10-01T00:00:00+00:00")
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=False))
    with pytest.raises(ApiProblem) as exc:
        await messaging.forget_room_memory(AuthContext(user_id="m", org_id="org_1"), str(uuid.uuid4()), new)
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    delete.assert_not_awaited()
