"""T03 handoff events: approval_answered, task_moved, and the once-per-workspace handoff_completed."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import handoff_analytics, webhook_dedup

RECORDED = webhook_dedup.WebhookDedupResult.RECORDED
SEEN = webhook_dedup.WebhookDedupResult.ALREADY_SEEN


@pytest.mark.asyncio
async def test_first_handoff_emits_once_with_days_since_created():
    org = {"created_at": int((time.time() - 2 * 86400) * 1000)}
    with (
        patch.object(webhook_dedup, "record_event_or_skip", AsyncMock(side_effect=[RECORDED, SEEN])) as claim,
        patch.object(handoff_analytics.clerk_admin, "get_organization", AsyncMock(return_value=org)),
        patch.object(handoff_analytics, "capture_product_event") as capture,
    ):
        await handoff_analytics._first_handoff("org_1", "user_a", "approval_answered")
        await handoff_analytics._first_handoff("org_1", "user_b", "task_moved")
    assert claim.await_args_list[0].args == ("handoff-first:org_1",)
    capture.assert_called_once()
    kw = capture.call_args.kwargs
    assert kw["event"] == "handoff_completed"
    assert kw["distinct_id"] == "user_a"
    assert kw["properties"]["kind"] == "approval_answered"
    assert 1.9 < kw["properties"]["days_since_created"] < 2.1


@pytest.mark.asyncio
async def test_task_moved_only_for_an_agent_assigned_task():
    with (
        patch.object(handoff_analytics, "capture_product_event") as capture,
        patch.object(handoff_analytics, "handoff") as handoff,
    ):
        handoff_analytics.task_moved(workspace_id="org_1", user_id="user_a", issue={"id": "i1"}, status="done")
        capture.assert_not_called()
        handoff_analytics.task_moved(
            workspace_id="org_1", user_id="user_a", issue={"id": "i1", "assigneeAgentId": "ag"}, status="done"
        )
    assert capture.call_args.kwargs["event"] == "task_moved"
    assert capture.call_args.kwargs["properties"]["status"] == "done"
    handoff.assert_called_once_with(workspace_id="org_1", user_id="user_a", kind="task_moved")


def test_handoff_ignores_non_workspaces_and_unknown_kinds():
    with patch.object(handoff_analytics.asyncio, "get_running_loop", MagicMock()) as loop:
        handoff_analytics.handoff(workspace_id="user_x", user_id="user_x", kind="task_moved")
        handoff_analytics.handoff(workspace_id="org_1", user_id="user_x", kind="nope")
    loop.assert_not_called()
