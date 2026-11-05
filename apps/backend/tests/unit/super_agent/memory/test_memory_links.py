"""Recall links (wave2-M Task 6): live rows only, each checked for the asking audience."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.services import memory_api, memory_links, memory_stages

WS = "org_1"
FIXTURE = Path(__file__).parents[3] / "fixtures" / "page_text_cases.json"


def _task(tid, *, audience=None, hidden=False, status="working", lane="shared", owner="user_sam"):
    return {
        "pk": f"WS#{WS}",
        "taskId": tid,
        "identifier": f"T-{tid[-2:]}",
        "title": f"Task {tid}",
        "status": status,
        "specialist": "agent-fin",
        "lane": lane,
        "ownerUserId": owner,
        "audience": audience or {"kind": "workspace", "userIds": [], "roomId": None},
        "hidden": hidden,
        "updatedAt": "2026-11-02T10:00:00+00:00",
        "version": 1,
        "attemptCount": 0,
    }


class Store:
    """tasks_repo over a dict: live rows, subscriptions and THREAD# rows."""

    def __init__(self, tasks, subs=None, threads=None):
        self.tasks, self.subs, self.threads = {t["taskId"]: t for t in tasks}, subs or {}, threads or {}
        self.tree_reads: list[str] = []

    async def get_task_tree(self, ws, task_id):
        self.tree_reads.append(task_id)
        return {
            "task": self.tasks.get(task_id),
            "attempts": [],
            "subscriptions": self.subs.get(task_id, []),
            "questions": [],
        }

    async def batch_get(self, ws, sks):
        return [self.threads[sk.removeprefix("THREAD#")] for sk in sks if sk.removeprefix("THREAD#") in self.threads]


@pytest.fixture
def store(monkeypatch):
    def use(*tasks, subs=None, threads=None, can_view=True, members=()):
        s = Store(tasks, subs, threads)
        monkeypatch.setattr(memory_links.tasks_repo, "get_task_tree", s.get_task_tree)
        monkeypatch.setattr(memory_links.tasks_repo, "batch_get", s.batch_get)
        viewer = can_view if callable(can_view) else (lambda t, u: can_view)
        monkeypatch.setattr(memory_links.tasks_access, "can_view", AsyncMock(side_effect=viewer))

        async def get_member(room_id, kind, user_id):
            return {"id": user_id} if (room_id, user_id) in members else None

        monkeypatch.setattr(memory_links.messaging_repo, "get_member", get_member)
        return s

    return use


ROOM_R1 = {"kind": "room", "roomId": "r1"}
WORKSPACE = {"kind": "workspace"}


def _turn(task_id=None, thread=None):
    return {
        "kind": "turn",
        "content": "x",
        **({"taskId": task_id} if task_id else {}),
        **({"threadId": thread} if thread else {}),
    }


async def test_links_from_task_arm_and_turn_tags_dedupe_in_rank_order(store):
    ids = [f"t{i:02d}" for i in range(14)]
    store(*[_task(i) for i in ids])
    results = [_turn("t03"), _turn("t00"), {"kind": "claim", "taskId": "t13"}, *[_turn(i) for i in ids[4:]]]
    links = await memory_links.resolve_links(WS, results, ["t01", "t02", "t01"], ROOM_R1)
    assert [lk["id"] for lk in links] == ["t01", "t02", "t03", "t00", *ids[4:10]]  # claims never link; cap 10
    assert [lk["why"] for lk in links[:4]] == ["task", "task", "turn", "turn"]


async def test_untagged_turn_resolves_task_through_inactive_thread_row(store):
    store(_task("t01"), threads={"room:r1:m9": {"threadKey": "room:r1:m9", "taskId": "t01", "active": False}})
    links = await memory_links.resolve_links(WS, [_turn(thread="room:r1:m9"), _turn(thread="h:abc")], [], ROOM_R1)
    assert [lk["id"] for lk in links] == ["t01"]


async def test_link_uses_live_row_and_drops_hidden_or_unviewable(store):
    s = store(
        _task("t01", status="resolved"),  # the card said "working"; the live row wins
        _task("t02", hidden=True),
        _task("t03", audience={"kind": "person", "userIds": ["user_priya"], "roomId": None}),
        can_view=lambda t, u: t["taskId"] != "t03",
    )
    links = await memory_links.resolve_links(
        WS, [], ["t01", "t02", "t03", "t99"], {"kind": "person", "userId": "user_sam"}
    )
    assert [(lk["id"], lk["status"]) for lk in links] == [("t01", "resolved")]
    assert s.tree_reads == ["t01", "t02", "t03", "t99"]


async def test_private_task_link_only_for_owner_person_audience(store):
    private = _task("t01", lane="private", audience={"kind": "person", "userIds": ["user_priya"], "roomId": None})
    store(private, can_view=lambda t, u: u in t["audience"]["userIds"])
    for audience in (ROOM_R1, WORKSPACE, {"kind": "person", "userId": "user_sam"}):
        assert await memory_links.resolve_links(WS, [], ["t01"], audience) == []
    assert len(await memory_links.resolve_links(WS, [], ["t01"], {"kind": "person", "userId": "user_priya"})) == 1


async def test_room_audience_needs_same_room_or_workspace_task(store):
    store(
        _task("t01", audience={"kind": "room", "userIds": [], "roomId": "r1"}),
        _task("t02", audience={"kind": "room", "userIds": [], "roomId": "r2"}),
        _task("t03"),
    )
    links = await memory_links.resolve_links(WS, [], ["t01", "t02", "t03"], ROOM_R1)
    assert [lk["id"] for lk in links] == ["t01", "t03"]
    assert [lk["id"] for lk in await memory_links.resolve_links(WS, [], ["t01", "t02", "t03"], WORKSPACE)] == ["t03"]


async def test_threads_rechecked_per_audience(store):
    subs = {
        "t01": [
            {"threadKey": "room:r1:m1", "active": True},
            {"threadKey": "room:r2:m2", "active": True},
            {"threadKey": "mail:abc", "active": True},
        ]
    }
    store(_task("t01"), subs=subs, members={("r2", "user_sam")})
    [in_r1] = await memory_links.resolve_links(WS, [], ["t01"], ROOM_R1)
    assert [t["id"] for t in in_r1["threads"]] == ["room:r1:m1"]
    [sam] = await memory_links.resolve_links(WS, [], ["t01"], {"kind": "person", "userId": "user_sam"})
    assert [t["id"] for t in sam["threads"]] == ["room:r2:m2"]


async def test_read_link_returns_summary_or_none(store, monkeypatch):
    store(_task("t01"), _task("t02", hidden=True))
    monkeypatch.setattr(memory_links.task_conversation, "last_result_text", AsyncMock(return_value="Done: v2 sent"))
    out = await memory_links.read_link(WS, "t01", ROOM_R1)
    assert out["task"]["taskId"] == "t01" and out["lastResult"] == "Done: v2 sent"
    assert await memory_links.read_link(WS, "t02", ROOM_R1) is None
    assert await memory_links.read_link(WS, "t99", ROOM_R1) is None


def test_page_text_linked_section():
    """Shared with the Hermes plugin's copy (wave2-M Task 7): both render every case the same."""
    cases = json.loads(FIXTURE.read_text("utf-8"))
    assert cases
    for case in cases:
        assert memory_stages.page_text(case["page"]) == case["text"], case["name"]


def test_from_record_keeps_task_and_thread_ids():
    rec = {
        "memoryRecordId": "r1",
        "content": {"text": "Sam: ship it"},
        "metadata": {"taskId": {"stringValue": "t01"}, "threadId": {"stringValue": "room:r1:m1"}},
    }
    c = memory_stages.from_record(rec, "turn")
    assert (c.task_id, c.thread_id) == ("t01", "room:r1:m1")
    r = memory_stages._result(c)
    assert r["taskId"] == "t01" and r["threadId"] == "room:r1:m1"
    assert "taskId" not in memory_stages._result(memory_stages.from_record({"memoryRecordId": "r2"}, "turn"))


async def test_switch_off_page_has_no_links_key(monkeypatch):
    page = {"asOf": "x", "usedEvidenceTokens": 0, "results": []}
    rec = AsyncMock(return_value=dict(page))
    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    monkeypatch.setattr(memory_api, "_sync_in_background", lambda *a: None)
    monkeypatch.setattr(memory_api.memory_recall, "recall", rec)
    monkeypatch.setattr(memory_api, "flags_for", AsyncMock(return_value=False))
    assert await memory_api.recall_page(WS, None, "q") == page
    assert "tasks" not in rec.await_args.kwargs

    rec.return_value = {**page, "taskHits": ["t01"]}
    resolve = AsyncMock(return_value=[{"id": "t01"}])
    monkeypatch.setattr(memory_api, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(memory_api.memory_links, "resolve_links", resolve)
    out = await memory_api.recall_page(WS, "user_sam", "q")
    assert out["links"] == [{"id": "t01"}] and "taskHits" not in out
    assert rec.await_args.kwargs["tasks"] is True
    assert resolve.await_args.args == (WS, [], ["t01"], {"kind": "person", "userId": "user_sam"})
