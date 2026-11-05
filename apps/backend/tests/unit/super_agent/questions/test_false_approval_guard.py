"""The Router can't claim an approval that didn't happen (wave2-Q Task 9; spec 05 §3.5 layer 3). A Lightbulb reply
naming a task with an open Question and saying "approved"/"going ahead now" is replaced by the card line."""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.repositories import questions_repo
from core.services import messaging
from core.services import task_conversation as tc

from .conftest import TASK, WS

ROOM = str(uuid.uuid4())


@pytest.fixture
def posted(table, monkeypatch):
    sent = []

    async def create(ws, req, attrs=None):
        sent.append((req.text, attrs))
        return type("R", (), {"message_id": uuid.uuid4()})()

    monkeypatch.setattr(tc.messaging, "create_agent_message", create)
    monkeypatch.setattr(tc, "front_agent_id", AsyncMock(return_value=str(uuid.uuid4())))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda u: u.split("_")[1].title()))
    return sent


async def _open():
    await questions_repo.open_question(
        WS,
        {
            "taskId": TASK,
            "qid": "q-1",
            "kind": "approval",
            "subkind": "purchase",
            "title": "buy acme.dev",
            "askedBy": {"kind": "worker"},
            "answerers": ["user_sam"],
            "answererRule": "requester",
            "baseAnswerers": ["user_sam"],
        },
    )


REFS = [{"taskId": TASK, "role": "started"}]


async def test_false_claim_dropped_rest_kept_card_appended(posted):
    await _open()
    await tc.post_as_lightbulb(
        WS,
        ROOM,
        text="Approved — proceeding with the purchase. I'll also draft the renewal note.",
        event_id="e1",
        specialist="Finance",
        task_refs=REFS,
    )
    [(text, attrs)] = posted
    assert text == "I'll also draft the renewal note. Queued for Finance after Sam's decision."
    assert attrs["kind"] == "question"
    assert attrs["taskRefs"] == [{"taskId": TASK, "role": "started", "qid": "q-1"}]  # one card, bound to the question


async def test_go_ahead_wording_replaced_too(posted):
    await _open()
    await tc.post_as_lightbulb(WS, ROOM, text="Got it, going ahead now.", event_id="e1", task_refs=REFS)
    assert posted[0][0] == "Queued for Lightbulb after Sam's decision."


async def test_report_of_a_past_approval_posts_as_written(posted):
    await _open()
    text = "Sam approved the copy yesterday, so I sent it. Once approved, I'll book the venue too."
    await tc.post_as_lightbulb(WS, ROOM, text=text, event_id="e1", task_refs=REFS)
    assert posted[0][0] == text and posted[0][1].get("kind") is None


async def test_many_answerers_are_capped(posted, monkeypatch):
    await questions_repo.open_question(
        WS,
        {
            "taskId": TASK,
            "qid": "q-many",
            "kind": "approval",
            "subkind": "purchase",
            "title": "buy acme.dev",
            "askedBy": {"kind": "worker"},
            "answerers": ["user_sam", "user_ada", "user_bo", "user_cy", "user_di"],
            "answererRule": "admins",
            "baseAnswerers": [],
        },
    )
    await tc.post_as_lightbulb(WS, ROOM, text="Approved and proceeding.", event_id="e1", task_refs=REFS)
    assert posted[0][0] == "Queued for Lightbulb after a decision from Sam, Ada and 3 others."


async def test_honest_reply_posts_as_written(posted):
    await _open()
    await tc.post_as_lightbulb(WS, ROOM, text="Waiting on Sam to approve the purchase.", event_id="e1", task_refs=REFS)
    assert posted[0][0] == "Waiting on Sam to approve the purchase."


async def test_no_open_question_posts_as_written(posted):
    await tc.post_as_lightbulb(WS, ROOM, text="Approved and sent.", event_id="e1", task_refs=REFS)
    assert posted[0][0] == "Approved and sent."


async def test_cards_are_not_rewritten(posted):
    await _open()
    await tc.post_as_lightbulb(WS, ROOM, text="Approved by Sam at 14:02", event_id="e1", task_refs=REFS, kind="result")
    assert posted[0][0] == "Approved by Sam at 14:02"
