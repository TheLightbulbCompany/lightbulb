import io
import json
import zipfile
from unittest.mock import call, create_autospec, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.services.paperclip_admin_client import PaperclipAdminClient
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
    with patch("routers.teams.workspace_export.messaging_repo.list_owner_room_ids", autospec=True) as rooms:
        with pytest.raises(HTTPException) as exc:
            await export_workspace(ctx=CTX, auth=auth)
    assert exc.value.status_code == 403
    rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_zip_contains_all_room_messages_and_board_tasks():
    auth = AuthContext(user_id="user_owner", org_id="org_1", org_role="org:admin")
    page_1 = ([{"messageId": "m1", "messageSeq": 1, "body": "hello", "pk": "internal"}], 1)
    page_2 = ([{"messageId": "m2", "messageSeq": 2, "body": "goodbye", "sk": "internal"}], None)
    admin = create_autospec(PaperclipAdminClient, instance=True)
    first_task_page = [
        {"id": f"00000000-0000-0000-0000-{index:012d}", "title": f"Task {index}", "status": "done"}
        for index in range(500)
    ]
    final_task = {"id": "00000000-0000-0000-0001-000000000000", "title": "Retain records", "status": "done"}
    admin.list_issues.side_effect = [first_task_page, [final_task]]

    with (
        patch("routers.teams.workspace_export.messaging_repo.list_owner_room_ids", autospec=True) as list_room_ids,
        patch("routers.teams.workspace_export.messaging_repo.get_room", autospec=True) as get_room,
        patch("routers.teams.workspace_export.messaging_repo.list_messages", autospec=True) as list_messages,
        patch("routers.teams.workspace_export._agents._admin", return_value=admin),
    ):
        list_room_ids.return_value = ["room-1"]
        get_room.return_value = {"roomId": "room-1", "name": "General", "pk": "internal"}
        list_messages.side_effect = [page_1, page_2]
        response = await export_workspace(ctx=CTX, auth=auth)

    assert response.media_type == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert set(archive.namelist()) == {"manifest.json", "rooms/room-1.json", "board/tasks.json"}
    room = json.loads(archive.read("rooms/room-1.json"))
    assert room["room"] == {"roomId": "room-1", "name": "General"}
    assert [message["body"] for message in room["messages"]] == ["hello", "goodbye"]
    assert all("pk" not in message and "sk" not in message for message in room["messages"])
    tasks = json.loads(archive.read("board/tasks.json"))["tasks"]
    assert len(tasks) == 501
    assert tasks[-1]["id"] == final_task["id"]
    get_room.assert_awaited_once_with("room-1")
    assert list_messages.await_args_list == [
        call("room-1", 0, 100),
        call("room-1", 1, 100),
    ]
    assert admin.list_issues.await_args_list == [
        call(
            company_id="company_1",
            session_cookie="session=opaque",
            params={"limit": "500", "sortField": "id", "sortDir": "asc"},
        ),
        call(
            company_id="company_1",
            session_cookie="session=opaque",
            params={
                "limit": "500",
                "sortField": "id",
                "sortDir": "asc",
                "afterId": "00000000-0000-0000-0000-000000000499",
            },
        ),
    ]
