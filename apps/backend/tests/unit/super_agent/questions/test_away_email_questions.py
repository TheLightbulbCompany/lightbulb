"""The away email for an unanswered Question (wave2-Q Task 8; contracts-v2 §14.5): one check per answerer, dropped
once the Question is answered or cancelled, and never the content in the email."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import settings
from core.repositories import questions_repo
from core.services import needs_you_email as away

WS = "org_1"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 11:00 in New York


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "k")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example")
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api.example")
    w = MagicMock()
    w.put = MagicMock()
    w.finish = AsyncMock()
    w.send = AsyncMock(return_value="ses-1")
    fake_table = MagicMock()
    fake_table.put_item = w.put
    monkeypatch.setattr(away.messaging_repo, "_table", lambda: fake_table)
    monkeypatch.setattr(away.messaging_repo, "finish_away_check", w.finish)
    monkeypatch.setattr(
        away.user_repo,
        "get",
        AsyncMock(return_value={"user_id": "user_sam", "email": "s+clerk_test@x.co", "timezone": "America/New_York"}),
    )
    monkeypatch.setattr(away.email_service, "send_email", w.send)
    monkeypatch.setattr(away, "capture_product_event", MagicMock())
    monkeypatch.setattr(away, "_claim_room_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(away, "put_metric", lambda *a, **k: None)
    return w


async def _open():
    await questions_repo.open_question(
        WS,
        {
            "taskId": TASK,
            "qid": "q-1",
            "kind": "approval",
            "title": "send the Q3 invoice to acme@example.com",
            "askedBy": {"kind": "worker"},
            "answerers": ["user_sam"],
            "answererRule": "requester",
            "baseAnswerers": ["user_sam"],
        },
    )


def _row(user="user_sam"):
    return {
        "pk": f"AWAY#{user}",
        "sk": f"Q#{TASK}#q-1",
        "kind": "task_question",
        "userId": user,
        "ownerId": WS,
        "taskId": TASK,
        "qid": "q-1",
        "title": "t",
        "body": "b",
        "createdAt": "2026-10-14T14:54:00+00:00",
    }


async def test_queue_question_one_row_per_answerer(world):
    await away.queue_question(WS, TASK, "q-1", ["user_sam", "user_ada"], "Finance needs your OK", "x" * 400)
    items = [c.kwargs["Item"] for c in world.put.call_args_list]
    assert [(i["pk"], i["sk"], i["kind"]) for i in items] == [
        ("AWAY#user_sam", f"Q#{TASK}#q-1", "task_question"),
        ("AWAY#user_ada", f"Q#{TASK}#q-1", "task_question"),
    ]
    assert items[0]["sweep"] == "away" and len(items[0]["body"]) == 300 and items[0]["ownerId"] == WS


async def test_away_email_for_unseen_question_after_5min(world):
    await _open()
    assert await away.check_one(_row(), NOW) is True
    kw = world.send.await_args.kwargs
    assert kw["subject"] == "A decision is waiting in Lightbulb"
    assert "invoice" not in kw["text"] and "acme" not in kw["html"]  # where, never what
    assert f"panel=task&taskId={TASK}&from=away_email" in kw["text"]


async def test_away_email_dropped_when_answered(world):
    await _open()
    await questions_repo.record_answer(
        WS, TASK, "q-1", option_id="deny", trigger_id=None, by="user_sam", client_answer_id="c", expected_version=None
    )
    assert await away.check_one(_row(), NOW) is False
    world.send.assert_not_awaited()
    world.finish.assert_awaited_once_with("AWAY#user_sam", f"Q#{TASK}#q-1")


async def test_away_email_dropped_for_a_non_answerer(world):
    await _open()
    assert await away.check_one(_row("user_priya"), NOW) is False
    world.send.assert_not_awaited()
