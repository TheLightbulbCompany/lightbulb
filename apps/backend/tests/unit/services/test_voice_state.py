"""The Router's state at a talk's start: recent chat, the tasks the caller keeps, memory; bounded; fails soft."""

from unittest.mock import AsyncMock, patch

from core.services import voice_state

ROOM = "7f1d2c3b-0000-4000-8000-000000000002"
NOW = "2099-01-01T00:00:00+00:00"


def _rows(n: int, text: str = "hello") -> list[dict]:
    return [{"author": {"kind": "human", "displayName": f"P{i}"}, "text": f"{text} {i}"} for i in range(n)]


async def _tasks_by_status(ws, status, limit, cursor=None):
    rows = {
        "working": [{"taskId": "k-1", "status": "working", "audience": {"roomId": ROOM}, "updatedAt": NOW}],
        "idle": [
            {"taskId": "k-2", "status": "idle", "audience": {"roomId": "other"}, "updatedAt": NOW},
            {"taskId": "k-3", "status": "idle", "audience": {"roomId": ROOM}, "updatedAt": "2020-01-01T00:00:00Z"},
        ],
    }
    return rows.get(status, []), None


async def test_context_has_chat_tasks_and_memory_and_keeps_only_what_keep_takes():
    view = lambda ws, t, viewer, room: {"taskId": t["taskId"], "title": "Post", "status": "working", "result": ""}  # noqa: E731
    with (
        patch.object(voice_state.messaging_repo, "list_messages_before", AsyncMock(return_value=_rows(2))) as lm,
        patch.object(voice_state.tasks_repo, "list_tasks_by_status", side_effect=_tasks_by_status),
        patch.object(voice_state.tasks_access, "can_view", AsyncMock(return_value=True)),
        patch.object(voice_state, "task_view", AsyncMock(side_effect=view)),
        patch.object(voice_state.memory_api, "recall_page", AsyncMock(return_value={"results": [{"content": "Closed Dec 25"}]})) as rp,
    ):  # fmt: skip
        keep = lambda t: t["audience"]["roomId"] == ROOM  # noqa: E731
        text = await voice_state.context("org_1", "user_maya", ROOM, keep, "user_maya", ROOM)
    assert "P0: hello 0\nP1: hello 1" in text
    assert "- [k-1] Post: working" in text
    assert "k-2" not in text  # another room's task: keep refused it
    assert "k-3" not in text  # finished long ago: history, not state
    assert "- Closed Dec 25" in text
    assert lm.await_args.args[0] == ROOM and rp.await_args.args[:2] == ("org_1", "user_maya")


async def test_a_failed_part_is_left_out_not_the_talk():
    with (
        patch.object(voice_state.messaging_repo, "list_messages_before", AsyncMock(return_value=_rows(1))),
        patch.object(voice_state.tasks_repo, "list_tasks_by_status", AsyncMock(side_effect=RuntimeError("down"))),
        patch.object(voice_state.memory_api, "recall_page", AsyncMock(side_effect=TimeoutError())),
    ):
        text = await voice_state.context("org_1", "u", ROOM, lambda t: True, None, ROOM)
    assert text == "Recent chat, oldest first:\nP0: hello 0"


def test_render_drops_the_oldest_messages_to_fit():
    lines = [f"P{i}: " + "x" * 270 for i in range(40)]
    tasks = [{"taskId": "k-1", "title": "Post", "status": "needs_you", "result": "Drafted the post."}]
    text = voice_state.render(lines, tasks, ["fact"])
    assert len(text) <= voice_state.CONTEXT_MAX
    assert "P39:" in text and "P0:" not in text  # the newest kept
    assert "- [k-1] Post: needs you (Drafted the post.)" in text and "- fact" in text


def test_task_status_says_what_the_voice_says():
    s = lambda status, group="working", watching=False: {"status": status, "shownGroup": group, "watching": watching}  # noqa: E731
    assert voice_state.task_status(s("waiting", "waiting_on_you")) == "needs_you"
    assert voice_state.task_status(s("waiting", "ready_for_review")) == "needs_you"
    assert voice_state.task_status(s("idle", "idle")) == "done"
    assert voice_state.task_status(s("idle", "idle", watching=True)) == "working"
    assert voice_state.task_status(s("failed", "idle")) == "failed"
    assert voice_state.task_status(s("working")) == "working"
