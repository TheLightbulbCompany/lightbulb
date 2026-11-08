"""A voice "yes" never answers a card: it marks it pendingConfirm and pushes a one-tap confirm to the approver."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import voice_approve

WS, MAYA = "org_1", "user_maya"
ORDER = {
    "taskId": "k1", "qid": "q-order", "kind": "approval", "subkind": "purchase", "state": "open",
    "title": "buy cleaning supplies for $212.00", "text": "", "createdAt": "2026-11-07T10:00:00+00:00",
}  # fmt: skip
POST = {**ORDER, "qid": "q-post", "kind": "review", "subkind": "publish", "title": "publish holiday hours post",
        "createdAt": "2026-11-07T11:00:00+00:00"}  # fmt: skip


@pytest.fixture
def m():
    rows = {q["qid"]: q for q in (ORDER, POST)}
    with (
        patch.object(voice_approve.questions_repo, "needs_for_user",
                     AsyncMock(return_value=([{"taskId": "k1", "qid": q} for q in rows], None))),
        patch.object(voice_approve.questions_repo, "get_question", AsyncMock(side_effect=lambda w, t, q: rows.get(q))),
        patch.object(voice_approve.questions_repo, "set_pending_confirm", AsyncMock()) as pend,
        patch.object(voice_approve.questions, "can_answer_now", AsyncMock(return_value=True)) as can,
        patch.object(voice_approve, "_push_once", AsyncMock()) as push,
        patch("core.temporal.client.update_task", AsyncMock()) as update,
        patch.object(voice_approve.questions_repo, "record_answer", AsyncMock()) as record,
    ):  # fmt: skip
        yield {"rows": rows, "pend": pend, "can": can, "push": push, "update": update, "record": record}


SAM_IN_MEET = {"user_id": MAYA, "name": "Sam", "where": "Monday standup"}


async def test_meet_yes_sets_pending_confirm_and_pushes(m):
    out = await voice_approve.approve(WS, "meet", SAM_IN_MEET, "yes", "the supply order", room_id="r1")
    assert out == {"confirm_sent": True, "title": "buy cleaning supplies for $212.00"}
    ws, task, qid, pending = m["pend"].await_args.args
    assert (ws, task, qid) == (WS, "k1", "q-order")
    assert {k: pending[k] for k in ("by", "seat", "roomId")} == {"by": "Sam", "seat": "meet", "roomId": "r1"}
    key, uid, title, body, data = m["push"].await_args.args
    assert uid == MAYA and data == {"source": "task_question", "taskId": "k1", "qid": "q-order"}
    assert body == "Approve in one tap: buy cleaning supplies for $212.00 (approved by voice in Monday standup)"


async def test_yes_never_answers_directly(m):
    for seat in ("meet", "app"):
        await voice_approve.approve(WS, seat, SAM_IN_MEET, "yes", "supplies")
    m["update"].assert_not_awaited()  # the answer path (TaskWorkflow update) is the tap's alone
    m["record"].assert_not_awaited()
    assert all(r["state"] == "open" for r in m["rows"].values())


async def test_no_open_card_returns_none(m):
    m["can"].return_value = False  # nothing this member may answer now
    assert await voice_approve.approve(WS, "app", SAM_IN_MEET, "yes", "supplies") == {"none": True}
    m["pend"].assert_not_awaited()
    m["push"].assert_not_awaited()


async def test_hint_picks_the_match_and_no_hint_picks_the_newest(m):
    await voice_approve.approve(WS, "app", SAM_IN_MEET, "yes", "")
    assert m["pend"].await_args.args[2] == "q-post"


async def test_no_and_undo_only_speak(m):
    assert await voice_approve.approve(WS, "app", SAM_IN_MEET, "no", "x") == {"say": "Say no in the app to decline."}
    assert await voice_approve.approve(WS, "app", SAM_IN_MEET, "undo", "x") == {"say": "Nothing to undo."}
    m["pend"].assert_not_awaited()
