"""Activity → Needs you reads Question rows (wave2-Q Task 8; contracts-v2 §14.2, §14.5). Only answerers see an item,
it leaves once answered, it counts on the badge, and with the switch off the feed never reads the tasks table."""

from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import questions_repo
from core.services import activity, super_agent_flags

WS = "org_1"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def _q(qid="q-1", kind="approval", **over):
    return {
        "taskId": TASK,
        "qid": qid,
        "kind": kind,
        "subkind": "purchase",
        "title": "buy a domain for $12",
        "text": "acme.dev, one year",
        "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "askedBy": {"kind": "worker", "attempt": 1},
        "answerers": ["user_sam"],
        "answererRule": "requester",
        "baseAnswerers": ["user_sam"],
    } | over


@pytest.fixture
def feed_world(table, monkeypatch):
    """No messages and no Paperclip items: only the Question source varies."""
    from core.repositories import messaging_repo

    monkeypatch.setattr(messaging_repo, "list_activity", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging_repo, "get_activity_seen", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging_repo, "list_activity_since", AsyncMock(return_value=[]))
    monkeypatch.setattr(activity, "_room_gates", AsyncMock(return_value={}))
    paperclip = AsyncMock(return_value=[])
    monkeypatch.setattr(activity, "_paperclip_within_budget", paperclip)
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(super_agent_flags, "flags_for", flag)
    return {"paperclip": paperclip, "flag": flag}


async def _feed(user="user_sam"):
    page = await activity.list_activity(
        AuthContext(user_id=user, org_id=WS), cursor=None, limit=30, teams_ctx=AsyncMock()
    )
    return page.model_dump(by_alias=True, mode="json")


async def test_task_question_item_renders_and_counts(feed_world):
    await questions_repo.open_question(WS, _q())
    page = await _feed()
    [item] = page["items"]
    assert item["id"] == f"taskq:{TASK}:q-1" and item["kind"] == "task_question" and item["state"] == "open"
    assert item["summary"]["title"] == "Lightbulb needs your OK to buy a domain for $12"
    assert item["summary"]["links"] == [{"kind": "task", "id": TASK, "label": "buy a domain for $12"}]
    assert [a["navigate"] for a in item["actions"]] == [{"kind": "task", "id": TASK, "roomId": None}]
    assert page["attentionCount"] == 1 and page["unseenCount"] == 1
    assert await activity.unseen_counts(WS, "user_sam", AsyncMock()) == {"approval": 1}


async def test_only_answerers_see_it(feed_world):
    await questions_repo.open_question(WS, _q())
    assert (await _feed("user_priya"))["items"] == []


async def test_item_gone_after_answer(feed_world):
    await questions_repo.open_question(WS, _q())
    await questions_repo.record_answer(
        WS, TASK, "q-1", option_id="deny", trigger_id=None, by="user_sam", client_answer_id="c", expected_version=None
    )
    page = await _feed()
    assert page["items"] == [] and page["attentionCount"] == 0


async def test_cancelled_question_gone(feed_world):
    await questions_repo.open_question(WS, _q())
    await questions_repo.cancel_open(WS, TASK, by="user_priya", reason="stopped")
    assert (await _feed())["items"] == []


async def test_paperclip_outage_never_hides_a_question(feed_world):
    feed_world["paperclip"].return_value = None
    await questions_repo.open_question(WS, _q())
    page = await _feed()
    assert [i["kind"] for i in page["items"]] == ["task_question"]
    assert page["sources"] == {"paperclip": "unavailable"}


async def test_switch_off_activity_unchanged(feed_world, monkeypatch):
    feed_world["flag"].return_value = False
    spy = AsyncMock(side_effect=AssertionError("tasks table read with the switch off"))
    monkeypatch.setattr(questions_repo, "needs_for_user", spy)
    await questions_repo.open_question(WS, _q())
    page = await _feed()
    assert page["items"] == [] and page["sources"] == {"paperclip": "ok"} and page["attentionCount"] == 0
    feed_world["paperclip"].return_value = None
    assert (await _feed())["sources"] == {"paperclip": "unavailable"}
    assert await activity.unseen_counts(WS, "user_sam", AsyncMock()) is None
    spy.assert_not_awaited()


async def test_question_store_error_never_breaks_the_feed(feed_world, monkeypatch):
    monkeypatch.setattr(questions_repo, "needs_for_user", AsyncMock(side_effect=RuntimeError("ddb down")))
    feed_world["paperclip"].return_value = []
    page = await _feed()
    assert page["items"] == [] and page["sources"] == {"paperclip": "ok"}
