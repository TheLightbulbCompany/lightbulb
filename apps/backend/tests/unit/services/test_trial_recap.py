"""T19: the day-28 recap numbers."""

from unittest.mock import AsyncMock

import pytest

from core.services import trial_recap

END = 1_800_000_000
SINCE = END - trial_recap.WINDOW_S
IN = "2027-01-10T00:00:00Z"  # inside the window
OLD = "2026-01-01T00:00:00Z"  # before it


def test_recap_window_is_the_last_three_days():
    assert trial_recap.in_recap_window(END, now=END - 3 * 86400 + 1)
    assert not trial_recap.in_recap_window(END, now=END - 4 * 86400)
    assert not trial_recap.in_recap_window(END, now=END + 1)
    assert not trial_recap.in_recap_window(None)


@pytest.mark.asyncio
async def test_trial_recap_counts_questions(monkeypatch):
    """Answered Questions in the window are the handoffs; allowed approvals are the approvals, the latest names
    the top item; a denial is a handoff but not an approval. No Paperclip."""
    trial_recap._cache.clear()
    answered = [
        {"kind": "approval", "answer": {"optionId": "allow_once"}, "answeredAt": IN, "title": "Send invoices"},
        {
            "kind": "approval",
            "answer": {"optionId": "allow_once"},
            "answeredAt": "2027-01-11T00:00:00+00:00",
            "title": "Hire",
        },
        {"kind": "approval", "answer": {"optionId": "deny"}, "answeredAt": IN, "title": "Refund"},
        {"kind": "question", "answer": {"optionId": "answer"}, "answeredAt": IN, "title": "Which day?"},
    ]
    read = AsyncMock(return_value=answered)
    monkeypatch.setattr(trial_recap.questions_repo, "answered_since", read)
    monkeypatch.setattr(trial_recap, "room_activity", AsyncMock(return_value=(2, 3)))
    got = await trial_recap.recap("org_1", END)
    assert (got["handoffs"], got["approvals"], got["top_item"], got["people"], got["agents"]) == (4, 2, "Hire", 2, 3)
    assert read.await_args.args == ("org_1", "2026-12-16T08:00:00+00:00")


@pytest.mark.asyncio
async def test_a_questions_outage_reads_zero_and_is_not_cached(monkeypatch):
    trial_recap._cache.clear()
    monkeypatch.setattr(trial_recap.questions_repo, "answered_since", AsyncMock(side_effect=RuntimeError("ddb")))
    monkeypatch.setattr(trial_recap, "room_activity", AsyncMock(return_value=(0, 0)))
    assert (await trial_recap.recap("org_1", END))["handoffs"] == 0
    assert trial_recap._cache == {}


@pytest.mark.asyncio
async def test_room_activity_counts_distinct_people_and_agents_in_the_window(monkeypatch):
    msgs = [
        {"createdAt": OLD, "author": {"id": "u_old", "kind": "human"}},
        {"createdAt": IN, "author": {"id": "u1", "kind": "human"}},
        {"createdAt": IN, "author": {"id": "u1", "kind": "human"}},
        {"createdAt": IN, "author": {"id": "ceo", "kind": "agent"}},
    ]
    monkeypatch.setattr(trial_recap.messaging_repo, "list_owner_room_ids", AsyncMock(return_value=["r1"]))
    monkeypatch.setattr(trial_recap.messaging_repo, "get_room", AsyncMock(return_value={"lastSeq": 4}))
    monkeypatch.setattr(trial_recap.messaging_repo, "list_messages_before_page", AsyncMock(return_value=(msgs, None)))
    assert await trial_recap.room_activity("org_1", SINCE) == (1, 1)
