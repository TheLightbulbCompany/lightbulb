import uuid
import asyncio
from unittest.mock import AsyncMock

import pytest
from core.containers.workspace import Workspace
from core.services import messaging
from routers.messaging import get_room_attachment

from core.auth import AuthContext
from core.services.messaging import (
    ApiProblem,
    CreateRoomMember,
    CreateRoomRequest,
    CreateMessageRequest,
    validate_image_bytes,
    create_room,
    create_human_message,
    get_agent_receipt,
    mark_room_unread,
)


@pytest.mark.asyncio
async def test_patch_room_allows_group_chat_admin_and_rejects_direct_message_rename(monkeypatch):
    auth = AuthContext(user_id="admin", org_id="org_1")
    group_meta = {"roomId": "group", "kind": "group-dm"}
    require_admin = AsyncMock(return_value=(group_meta, {"role": "admin"}))
    patch_name = AsyncMock(return_value={**group_meta, "name": "Release crew"})
    detail = AsyncMock(return_value="detail")
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "require_room_admin", require_admin)
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_name", patch_name)
    monkeypatch.setattr(messaging, "_room_detail", detail)
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)

    result = await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Release crew"))
    assert result == "detail"
    patch_name.assert_awaited_once_with("group", "Release crew")
    publish.assert_awaited_once_with({**group_meta, "name": "Release crew"})

    require_admin.return_value = ({"roomId": "dm", "kind": "dm"}, {"role": "owner"})
    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "dm", messaging.PatchRoomRequest(name="Private label"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert "cannot be renamed" in exc.value.error.message
    assert patch_name.await_count == 1


@pytest.mark.asyncio
async def test_patch_room_keeps_admin_permission_check_in_front_of_persistence(monkeypatch):
    auth = AuthContext(user_id="member", org_id="org_1")
    monkeypatch.setattr(
        messaging,
        "require_room_admin",
        AsyncMock(side_effect=ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")),
    )
    patch_name = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "patch_room_name", patch_name)

    with pytest.raises(ApiProblem) as exc:
        await messaging.patch_room(auth, "group", messaging.PatchRoomRequest(name="Denied"))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    patch_name.assert_not_awaited()


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
    create = AsyncMock(return_value=created)
    capture = AsyncMock()
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"kind": "channel"}, {})))
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
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({}, {})))
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
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({}, {})))
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

    mark.assert_awaited_once_with(room_id, "user_owner", joined_seq=7)
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
    assert validate_image_bytes("image/png", b"\x89PNG\r\n\x1a\nrest") == ".png"
    with pytest.raises(ValueError):
        validate_image_bytes("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(ValueError):
        validate_image_bytes("image/svg+xml", b"<svg/>")


def test_attachment_filename_matches_plugin_utf8_contract(tmp_path, monkeypatch):
    workspace = Workspace(str(tmp_path))
    monkeypatch.setattr(workspace, "_chown_for_access_point", lambda *_: None)
    monkeypatch.setattr(messaging, "get_workspace", lambda: workspace)
    attachment = messaging.store_image_attachment(
        "owner", str(uuid.uuid4()), "🙂" * 200 + "\x7f.png", "image/png", b"\x89PNG\r\n\x1a\nrest"
    )
    assert len(attachment.file_name.encode("utf-8")) <= 255
    assert "/" not in attachment.file_name and "\\" not in attachment.file_name and "\x7f" not in attachment.file_name


def test_attachment_load_rejects_metadata_id_mismatch(tmp_path, monkeypatch):
    workspace = Workspace(str(tmp_path))
    monkeypatch.setattr(workspace, "_chown_for_access_point", lambda *_: None)
    monkeypatch.setattr(messaging, "get_workspace", lambda: workspace)
    room_id = str(uuid.uuid4())
    attachment = messaging.store_image_attachment("owner", room_id, "proof.png", "image/png", b"\x89PNG\r\n\x1a\nrest")
    sidecar = tmp_path / "owner" / messaging.attachment_dir(room_id) / f"{attachment.id}.json"
    sidecar.write_text(sidecar.read_text().replace(str(attachment.id), str(uuid.uuid4())))
    with pytest.raises(ApiProblem):
        messaging.load_image_attachment("owner", room_id, str(attachment.id))


def test_attachment_store_rejects_symlink_to_another_room(tmp_path, monkeypatch):
    workspace = Workspace(str(tmp_path))
    monkeypatch.setattr(workspace, "_chown_for_access_point", lambda *_: None)
    monkeypatch.setattr(messaging, "get_workspace", lambda: workspace)
    room_id = str(uuid.uuid4())
    room_path = tmp_path / "owner" / messaging.attachment_dir(room_id)
    sibling_path = room_path.parent / str(uuid.uuid4())
    sibling_path.mkdir(parents=True)
    room_path.symlink_to(sibling_path, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes its room"):
        messaging.store_image_attachment("owner", room_id, "proof.png", "image/png", b"\x89PNG\r\n\x1a\nrest")
    assert list(sibling_path.iterdir()) == []


@pytest.mark.asyncio
async def test_attachment_download_checks_current_room_membership(monkeypatch):
    checked = False

    async def deny(owner_id, user_id, room_id):
        nonlocal checked
        checked = True
        raise ApiProblem("ROOM_FORBIDDEN", "No access")

    monkeypatch.setattr(messaging, "require_room_member", deny)
    monkeypatch.setattr(messaging, "load_image_attachment", lambda *_: pytest.fail("loaded before membership check"))
    with pytest.raises(ApiProblem):
        await get_room_attachment(str(uuid.uuid4()), str(uuid.uuid4()), AuthContext(user_id="user", org_id="owner"))
    assert checked
