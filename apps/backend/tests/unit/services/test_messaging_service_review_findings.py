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


def test_zip_mime_aliases_and_safe_extension_fallback_canonicalize_but_mismatches_fail():
    for declared in ("application/zip", "application/x-zip-compressed", "application/octet-stream", ""):
        assert messaging.canonical_attachment_content_type("bundle.ZIP", declared) == "application/zip"
    with pytest.raises(ValueError, match="extension"):
        messaging.canonical_attachment_content_type("bundle.zip", "application/pdf")
    with pytest.raises(ValueError, match=".zip filename"):
        messaging.canonical_attachment_content_type("setup.exe", "application/zip")


@pytest.mark.asyncio
async def test_text_file_upload_reproduces_clear_api_400_at_attachment_validation(monkeypatch):
    auth = AuthContext(user_id="user", org_id="owner")
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"ownerId": "owner"}, {"kind": "human", "id": "user"})),
    )
    upload = UploadFile(filename="notes.txt", file=BytesIO(b"hello"), headers={"content-type": "text/plain"})

    with pytest.raises(ApiProblem) as exc_info:
        await upload_room_attachment("room-1", upload, auth)

    assert exc_info.value.status_code == 400
    assert exc_info.value.error.code == "INVALID_REQUEST"
    assert "PNG, JPEG, WebP, GIF, PDF, or ZIP" in exc_info.value.error.message


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
    monkeypatch.setattr(messaging, "require_room_admin", require_admin)
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
    monkeypatch.setattr(
        messaging,
        "require_room_admin",
        AsyncMock(side_effect=ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")),
    )
    patch_meta = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    with pytest.raises(ApiProblem):
        await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Denied"))
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

    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "member", "role": "owner"})),
    )
    with pytest.raises(ApiProblem) as exc:
        await messaging.leave_room(auth, "room-1")
    assert exc.value.error.code == "INVALID_REQUEST"
    assert remove.await_count == 1


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


def _archive_fixture(monkeypatch, meta):
    require_admin = AsyncMock(return_value=(meta, {"role": "admin"}))
    patch_meta = AsyncMock(side_effect=lambda room_id, updates: {**meta, **updates})
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "require_room_admin", require_admin)
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
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


@pytest.mark.parametrize("kind", ["dm", "group-dm"])
@pytest.mark.asyncio
async def test_patch_room_refuses_to_archive_a_dm(monkeypatch, kind):
    auth = AuthContext(user_id="admin", org_id="org_1")
    patch_meta, _ = _archive_fixture(monkeypatch, {"roomId": "r", "ownerId": "org_1", "kind": kind, "revision": 1})
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
    monkeypatch.setattr(
        messaging,
        "require_room_admin",
        AsyncMock(side_effect=ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")),
    )
    patch_meta = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_meta", patch_meta)
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
