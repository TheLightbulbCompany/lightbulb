import io
import json
import zipfile
from unittest.mock import call, create_autospec, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.messaging import ApiProblem
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
    room_1 = "11111111-1111-4111-8111-111111111111"
    private_dm = "22222222-2222-4222-8222-222222222222"

    def message(seq: int, **extra):
        return {
            "messageId": f"33333333-3333-4333-8333-{seq:012d}",
            "roomId": room_1,
            "messageSeq": seq,
            "roomRevision": 1,
            "author": {"kind": "human", "id": "user_owner", "displayName": "Owner"},
            "createdAt": "2026-10-14T00:00:00+00:00",
            "pk": "internal",
            **extra,
        }

    page_1 = ([message(1, text="hello"), message(2, text="first draft", editedText="final")], 2)
    page_2 = (
        [
            message(
                3,
                text="secret",
                deletedAt="2026-10-14T00:01:00+00:00",
                attachments=[{"name": "secret.pdf"}],
            )
        ],
        None,
    )
    admin = create_autospec(PaperclipAdminClient, instance=True)
    first_task_page = [
        {"id": f"00000000-0000-0000-0000-{index:012d}", "title": f"Task {index}", "status": "done"}
        for index in range(500)
    ]
    final_task = {"id": "00000000-0000-0000-0001-000000000000", "title": "Retain records", "status": "done"}
    admin.list_issues.side_effect = [first_task_page, [final_task]]

    with (
        patch("routers.teams.workspace_export.messaging_repo.list_owner_room_ids", autospec=True) as list_room_ids,
        patch("routers.teams.workspace_export.messaging.require_room_member", autospec=True) as require_member,
        patch("routers.teams.workspace_export.messaging_repo.list_messages", autospec=True) as list_messages,
        patch("routers.teams.workspace_export._agents._admin", return_value=admin),
    ):
        list_room_ids.return_value = [room_1, private_dm]

        async def member_or_forbidden(owner_id, user_id, room_id):
            if room_id == private_dm:
                raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
            return {"roomId": room_1, "name": "General", "pk": "internal"}, {"role": "owner"}

        require_member.side_effect = member_or_forbidden
        list_messages.side_effect = [page_1, page_2]
        response = await export_workspace(ctx=CTX, auth=auth)

    assert response.media_type == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert set(archive.namelist()) == {"manifest.json", f"rooms/{room_1}.json", "board/tasks.json"}
    room = json.loads(archive.read(f"rooms/{room_1}.json"))
    assert room["room"] == {"roomId": room_1, "name": "General"}
    # Edited messages show the edit; a deleted one keeps neither text nor attachments.
    assert [m["text"] for m in room["messages"]] == ["hello", "final", ""]
    assert room["messages"][2]["attachments"] == []
    assert "secret" not in json.dumps(room)
    assert all("pk" not in m for m in room["messages"])
    assert json.loads(archive.read("manifest.json"))["roomCount"] == 1
    tasks = json.loads(archive.read("board/tasks.json"))["tasks"]
    assert len(tasks) == 501
    assert tasks[-1]["id"] == final_task["id"]
    assert require_member.await_args_list == [
        call("org_1", "user_owner", room_1),
        call("org_1", "user_owner", private_dm),
    ]
    assert list_messages.await_args_list == [
        call(room_1, 0, 100),
        call(room_1, 2, 100),
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
