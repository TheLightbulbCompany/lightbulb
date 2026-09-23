"""A free-tier workspace whose agent run fails BEFORE it has ever had a reply is a new user's
first impression dying (the 09-22 gate-413 bug failed every one). Counted so P29 pages."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging


@pytest.mark.parametrize(
    ("billing", "counted"),
    [
        ({"subscription_status": "trial_local"}, True),
        ({"subscription_status": "trial_local", "first_agent_response_at": "2026-09-22T10:00:00Z"}, False),
        ({"subscription_status": "active"}, False),
        (None, False),
    ],
)
@pytest.mark.asyncio
async def test_first_turn_failure_is_counted_only_for_a_trial_that_never_got_a_reply(billing, counted):
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=billing)),
        patch.object(messaging, "put_metric") as metric,
    ):
        await messaging.count_first_turn_failure("org_1", "model_unavailable")
    calls = [c for c in metric.call_args_list if c.args[0] == "messaging.run.first_turn_failed"]
    assert len(calls) == (1 if counted else 0)


@pytest.mark.asyncio
async def test_a_billing_read_failure_never_raises():
    with patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb"))):
        await messaging.count_first_turn_failure("org_1", None)


@pytest.mark.asyncio
async def test_every_failed_run_goes_through_the_counter():
    """Both failure paths (run.ended callback and the sweeper) share post_run_failure_message."""
    counter = AsyncMock()
    with patch.object(messaging, "count_first_turn_failure", counter):
        await messaging.post_run_failure_message("org_1", {}, "error", "agent_did_not_start")
    counter.assert_awaited_once_with("org_1", "agent_did_not_start")
