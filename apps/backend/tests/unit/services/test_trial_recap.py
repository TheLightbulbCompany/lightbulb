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


def test_approvals_count_a_rejection_as_a_handoff_and_name_the_latest_approval():
    rows = [
        {"status": "approved", "decidedByUserId": "u1", "decidedAt": IN, "payload": {"title": "Send invoices"}},
        {
            "status": "approved",
            "decidedByUserId": "u1",
            "decidedAt": "2027-01-11T00:00:00Z",
            "payload": {"title": "Hire"},
        },
        {"status": "rejected", "decidedByUserId": "u2", "decidedAt": IN, "payload": {}},
        {"status": "approved", "decidedByUserId": "u1", "decidedAt": OLD, "payload": {"title": "old"}},
        {"status": "pending", "payload": {"title": "open"}},
    ]
    assert trial_recap.approval_numbers(rows, SINCE) == (3, 2, "Hire")


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
