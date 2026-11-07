"""The tasks board for super-agent turns (wave2-M Task 7): visible rows only, no Paperclip."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import container_auth, memory_links  # noqa: E402
from main import app  # noqa: E402

NOW = datetime.now(UTC)


def _task(tid, status, *, room=None, days_ago=0, hidden=False):
    return {
        "taskId": tid,
        "identifier": f"T-{tid}",
        "title": f"Task {tid}",
        "status": status,
        "hidden": hidden,
        "audience": {"kind": "room", "roomId": room} if room else {"kind": "workspace"},
        "updatedAt": (NOW - timedelta(days=days_ago)).isoformat(),
    }


ROWS = {
    "working": [_task("1", "working"), _task("2", "working", room="r2"), _task("3", "working", hidden=True)],
    "waiting": [_task("4", "waiting", room="r1")],
    "resolved": [_task("5", "resolved", days_ago=2), _task("6", "resolved", days_ago=30)],
}


async def _by_status(ws, status, *, limit=50, cursor=None):
    return ROWS.get(status, []), None


async def test_board_route_filters_by_audience():
    with patch.object(memory_links.tasks_repo, "list_tasks_by_status", _by_status):
        text = await memory_links.board_text("org_1", {"kind": "room", "roomId": "r1"})
        assert "In progress: T-1 Task 1\n" in text  # T-2 is r2's, T-3 hidden
        assert "Waiting: T-4 Task 4\n" in text
        assert f"Done in the last 14 days: T-5 Task 5 ({(NOW - timedelta(days=2)).date()})" in text
        assert "T-6" not in text and "T-2" not in text and "T-3" not in text
        assert "T-4" not in await memory_links.board_text("org_1", {"kind": "workspace"})


def test_board_route_needs_a_room():
    client = TestClient(app)
    with (
        patch.object(container_auth, "_require_hook_token", AsyncMock()),
        patch.object(memory_links, "board_text", AsyncMock(return_value="Task board ...")) as board,
    ):
        url = "/api/v1/internal/memory/board/org_1"
        assert client.post(url, json={"audience": {"kind": "room"}}).status_code == 422
        r = client.post(url, json={"audience": {"kind": "room", "roomId": "r1"}})
        assert r.json() == {"text": "Task board ..."}
        board.assert_awaited_once_with("org_1", {"kind": "room", "roomId": "r1"})
