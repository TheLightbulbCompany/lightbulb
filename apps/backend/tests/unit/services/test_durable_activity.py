"""Trial scale-to-zero must never stop a container with an open ledger run."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import durable_activity

ROW = {"owner_id": "user_1", "status": "running", "last_interactive_at": 0}


def _patches(*, has_open_runs: bool):
    connections = MagicMock()
    connections.has_live_operator_connection = AsyncMock(return_value=False)
    ecs = MagicMock()
    ecs.stop_user_service = AsyncMock()
    return ecs, [
        patch.object(durable_activity.container_repo, "get_by_status", AsyncMock(return_value=[ROW])),
        patch.object(durable_activity.container_repo, "get_by_owner_id", AsyncMock(return_value=ROW)),
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
