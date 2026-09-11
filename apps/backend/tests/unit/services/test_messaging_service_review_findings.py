import uuid
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
    get_agent_receipt,
)


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
async def test_get_agent_receipt_maps_malformed_event_id_to_400_not_500():
    with pytest.raises(ApiProblem) as exc_info:
        await get_agent_receipt("user_owner", "bad/event/id")

    assert exc_info.value.status_code == 400
    assert exc_info.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_create_room_over_member_limit_returns_409_not_a_pydantic_400():
    auth = AuthContext(user_id="user_owner")
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
