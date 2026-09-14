"""Trial scale-to-zero must never stop a container with an open ledger run."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import durable_activity

ROW = {"owner_id": "user_1", "status": "running", "last_interactive_at": 0}


def _patches(*, has_open_runs: bool, row: dict = ROW):
    connections = MagicMock()
    connections.has_live_operator_connection = AsyncMock(return_value=False)
    ecs = MagicMock()
    ecs.stop_user_service = AsyncMock()
    return ecs, [
        patch.object(durable_activity.container_repo, "get_by_status", AsyncMock(return_value=[row])),
        patch.object(durable_activity.container_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(durable_activity.container_repo, "try_claim_idle_stop", AsyncMock(return_value="claim-1")),
        patch.object(durable_activity.container_repo, "idle_stop_claim_current", AsyncMock(return_value=True)),
        patch.object(
            durable_activity.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch.object(durable_activity.messaging_repo, "has_open_runs", AsyncMock(return_value=has_open_runs)),
        patch.object(durable_activity, "ConnectionService", MagicMock(return_value=connections)),
        patch.object(durable_activity, "get_ecs_manager", MagicMock(return_value=ecs)),
        patch.object(durable_activity, "put_metric", MagicMock()),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_open_runs,stopped", [(True, False), (False, True)])
async def test_an_open_ledger_run_vetoes_the_idle_stop(has_open_runs, stopped):
    ecs, patches = _patches(has_open_runs=has_open_runs)
    for p in patches:
        p.start()
    try:
        await durable_activity.sweep_trial_idle_once(now=10_000)
    finally:
        for p in patches:
            p.stop()
    if stopped:
        ecs.stop_user_service.assert_awaited_once_with("user_1", reason="trial_idle")
    else:
        ecs.stop_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_last_interactive_at_falls_back_to_created_at_not_a_churning_updated_at():
    """A row that has NEVER recorded a genuine interaction (legacy row, or a
    container nobody has messaged yet) has no ``last_interactive_at`` at all.
    The fallback must use ``created_at`` -- immutable after the row is
    created -- not ``updated_at``: ``try_claim_idle_stop`` itself stamps
    ``updated_at`` on every claim attempt, so falling back to it would let
    the sweeper's own claim perpetually re-freshen the row it is trying to
    judge, identically to the Decimal bug this module was fixed for.
    """
    # A real-scale epoch: _activity_epoch parses ISO strings into real wall-clock
    # seconds, so `now` must be on the same scale for the comparison to mean
    # anything (unlike the Decimal tests above, which never reach the ISO branch
    # on the fixed path).
    now = int(datetime.now(timezone.utc).timestamp())
    ancient = datetime.fromtimestamp(now - durable_activity.TRIAL_IDLE_SECONDS - 1000, tz=timezone.utc)
    row = {
        "owner_id": "user_1",
        "status": "running",
        "created_at": ancient.isoformat(),  # well outside the window
        "updated_at": datetime.now(timezone.utc).isoformat(),  # churns; must be ignored
    }
    ecs, patches = _patches(has_open_runs=False, row=row)
    for p in patches:
        p.start()
    try:
        await durable_activity.sweep_trial_idle_once(now=now)
    finally:
        for p in patches:
            p.stop()
    ecs.stop_user_service.assert_awaited_once_with("user_1", reason="trial_idle")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds_past_window,stopped",
    [
        (1, True),  # last_interactive_at just older than TRIAL_IDLE_SECONDS -> idle
        (-1, False),  # last_interactive_at just inside the window -> not idle
    ],
)
async def test_decimal_last_interactive_at_is_read_over_a_fresh_updated_at(seconds_past_window, stopped):
    """DynamoDB hands back numeric attributes as ``decimal.Decimal``, never
    ``int``/``float``. A row whose ``last_interactive_at`` is a Decimal must
    still be read for real activity instead of falling back to a fresh
    ``updated_at`` string (stamped moments ago by an unrelated write, e.g.
    the sweeper's own idle-stop claim) -- that fallback is what silently
    disabled trial scale-to-zero fleet-wide.
    """
    now = 10_000
    row = {
        "owner_id": "user_1",
        "status": "running",
        "last_interactive_at": Decimal(now - durable_activity.TRIAL_IDLE_SECONDS - seconds_past_window),
        # Real wall-clock "now" -- always fresher than the test's fake `now`,
        # so a broken fallback to this field would always read as "active".
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ecs, patches = _patches(has_open_runs=False, row=row)
    for p in patches:
        p.start()
    try:
        await durable_activity.sweep_trial_idle_once(now=now)
    finally:
        for p in patches:
            p.stop()
    if stopped:
        ecs.stop_user_service.assert_awaited_once_with("user_1", reason="trial_idle")
    else:
        ecs.stop_user_service.assert_not_awaited()
