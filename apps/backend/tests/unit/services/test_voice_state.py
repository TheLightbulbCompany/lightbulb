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
    def view(ws, t, viewer, room, people=None):
        return {"taskId": t["taskId"], "title": "Post", "status": "working", "result": ""}

    with (
        patch.object(voice_state.messaging_repo, "list_messages_before", AsyncMock(return_value=_rows(2))) as lm,
        patch.object(voice_state.tasks_repo, "list_tasks_by_status", side_effect=_tasks_by_status),
        patch.object(voice_state.tasks_access, "can_view", AsyncMock(return_value=True)),
        patch.object(voice_state, "task_view", AsyncMock(side_effect=view)),
        patch.object(voice_state.memory_api, "recall_page", AsyncMock(return_value={"results": [{"content": "Closed Dec 25"}]})) as rp,
    ):  # fmt: skip
        keep = lambda t: t["audience"]["roomId"] == ROOM  # noqa: E731
        text = await voice_state.context("org_1", "user_maya", ROOM, keep, "user_maya", ROOM, ["user_maya"])
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
        text = await voice_state.context("org_1", "u", ROOM, lambda t: True, None, ROOM, ["u"])
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


async def test_a_result_line_is_judged_for_everyone_present():
    summary = {"taskId": "k-1", "title": "Post", "status": "idle", "shownGroup": "idle", "watching": False}
    with (
        patch.object(voice_state.tasks_repo, "get_task_tree", AsyncMock(return_value={"task": {"taskId": "k-1"}})),
        patch.object(voice_state.tasks_access, "summarize", return_value=summary),
        patch.object(voice_state.tasks_access, "result_line", return_value="Drafted.") as rl,
    ):
        out = await voice_state.task_view("org_1", {"taskId": "k-1"}, "u", ROOM, ["u", "sam"])
    assert out == {"taskId": "k-1", "title": "Post", "status": "done", "result": "Drafted."}
    assert rl.call_args.args[1:] == (ROOM, ["u", "sam"])


async def test_events_are_changes_since_that_need_someone_or_finished():
    t = lambda i, s, at, room=ROOM: {"taskId": i, "status": s, "updatedAt": at, "audience": {"roomId": room}}  # noqa: E731
    pages = {
        "waiting": [t("k-q", "waiting", "2026-11-08T10:05:00Z"), t("k-w", "waiting", "2026-11-08T10:06:00Z")],
        "idle": [t("k-d", "idle", "2026-11-08T10:04:00Z"), t("k-old", "idle", "2026-11-08T09:00:00Z"),
                 t("k-x", "idle", "2026-11-08T10:07:00Z", room="other")],
    }  # fmt: skip
    status = {"k-q": "needs_you", "k-w": "working", "k-d": "done"}

    def view(ws, row, viewer, room, people=None):
        return {"taskId": row["taskId"], "title": "T", "status": status[row["taskId"]], "result": ""}

    with (
        patch.object(voice_state.tasks_repo, "list_tasks_by_status", AsyncMock(side_effect=lambda ws, s, limit: (pages.get(s, []), None))),
        patch.object(voice_state.tasks_access, "can_view", AsyncMock(return_value=True)),
        patch.object(voice_state, "task_view", AsyncMock(side_effect=view)),
    ):  # fmt: skip
        out = await voice_state.events(
            "org_1", "u", ROOM, lambda r: r["audience"]["roomId"] == ROOM, "2026-11-08T10:00:00Z"
        )
    assert [(e["taskId"], e["status"]) for e in out] == [("k-d", "done"), ("k-q", "needs_you")]  # oldest first
