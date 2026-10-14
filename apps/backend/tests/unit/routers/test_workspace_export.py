import io
import json
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers.teams.deps import TeamsContext
from routers.teams.workspace_export import export_workspace


CTX = TeamsContext(
    user_id="user_owner",
    org_id="org_1",
    owner_id="org_1",
    company_id="company_1",
    paperclip_user_id="paperclip_user_1",
    session_cookie="session=opaque",
)


@pytest.mark.asyncio
async def test_export_rejects_non_owner_before_reading_records():
    auth = AuthContext(user_id="user_member", org_id="org_1", org_role="org:member")
    with patch("routers.teams.workspace_export.messaging_repo.list_owner_room_ids", new=AsyncMock()) as rooms:
        with pytest.raises(HTTPException) as exc:
            await export_workspace(ctx=CTX, auth=auth)
    assert exc.value.status_code == 403
    rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_zip_contains_all_room_messages_and_board_tasks():
    auth = AuthContext(user_id="user_owner", org_id="org_1", org_role="org:admin")
    page_1 = ([{"messageId": "m1", "messageSeq": 1, "body": "hello", "pk": "internal"}], 1)
    page_2 = ([{"messageId": "m2", "messageSeq": 2, "body": "goodbye", "sk": "internal"}], None)
    admin = AsyncMock()
    admin.list_issues.return_value = [{"id": "i1", "title": "Retain records", "status": "done"}]

    with (
        patch("routers.teams.workspace_export.messaging_repo.list_owner_room_ids", new=AsyncMock(return_value=["room-1"])),
        patch("routers.teams.workspace_export.messaging_repo.get_room", new=AsyncMock(return_value={"roomId": "room-1", "name": "General", "pk": "internal"})),
        patch("routers.teams.workspace_export.messaging_repo.list_messages", new=AsyncMock(side_effect=[page_1, page_2])) as list_messages,
        patch("routers.teams.workspace_export._agents._admin", return_value=admin),
    ):
        response = await export_workspace(ctx=CTX, auth=auth)

    assert response.media_type == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert set(archive.namelist()) == {"manifest.json", "rooms/room-1.json", "board/tasks.json"}
    room = json.loads(archive.read("rooms/room-1.json"))
    assert room["room"] == {"roomId": "room-1", "name": "General"}
    assert [message["body"] for message in room["messages"]] == ["hello", "goodbye"]
    assert all("pk" not in message and "sk" not in message for message in room["messages"])
    assert json.loads(archive.read("board/tasks.json"))["tasks"][0]["id"] == "i1"
    assert list_messages.await_count == 2
    admin.list_issues.assert_awaited_once_with(
        company_id="company_1",
        session_cookie="session=opaque",
        params={"limit": "500"},
    )
