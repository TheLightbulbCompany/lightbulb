"""Tests for the one-time boot reconcile pass (Task 14).

Backend restarts drop Task 12's in-process grace timers
(``GatewayConnectionPool._grace_close`` / ``_maybe_stop_trial_container``) —
the extended post-disconnect grace for trial (``trial_local``) containers is
an ``asyncio.Task`` living on a single backend instance. A deploy or crash
kills that task mid-grace, and nothing else re-arms the stop check, so the
container keeps running (and billing Bedrock credits) forever.

``_reconcile_trial_idle_containers`` is the one-time startup recovery: it
reads every ``status=running`` container row and dispatches its owner
straight to the SAME veto ladder Task 12 already covers exhaustively in
``tests/unit/gateway/test_connection_pool_scale_to_zero.py``
(``pool._maybe_stop_trial_container``). These tests exercise the real pool
+ real veto method (mocking only its DynamoDB/ECS collaborators, matching
the Task 12 test file's own patch targets) so "paid owner never stopped"
and "live-conn owner never stopped" are proven through the actual veto
path, not re-implemented/duplicated here.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.gateway.connection_pool import GatewayConnectionPool  # noqa: E402

OWNER = "owner_1"


def _pool(conn_svc=None) -> GatewayConnectionPool:
    """Real pool with a mock ConnectionService so count_live_chat_connections
    never touches DynamoDB. Defaults to 'nobody home' (count == 0). Matches
    the helper in tests/unit/gateway/test_connection_pool_scale_to_zero.py."""
    if conn_svc is None:
        conn_svc = MagicMock()
        conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
        # No Paperclip run in flight — the stop ladder's operator veto.
        conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    return GatewayConnectionPool(management_api=None, conn_svc=conn_svc)


def _trial_account():
    return {"subscription_status": "trial_local"}


def _patch_billing(account):
    return patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=account),
    )


def _patch_ecs(stop_mock):
    mgr = SimpleNamespace(stop_user_service=stop_mock)
    return patch("core.containers.get_ecs_manager", return_value=mgr)


@pytest.mark.asyncio
async def test_reconcile_stops_trial_container_with_zero_live_conns():
    """A running container owned by a trial_local owner with zero live chat
    connections and nothing in flight MUST be stopped via the boot pass."""
    from main import _reconcile_trial_idle_containers

    pool = _pool()
    stop = AsyncMock()
    rows = [{"owner_id": OWNER, "status": "running"}]

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=rows,
        ) as mock_get,
        patch("main.get_gateway_pool", return_value=pool),
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await _reconcile_trial_idle_containers()
        # Give the fire-and-forget task a chance to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    mock_get.assert_awaited_once_with("running")
    stop.assert_awaited_once_with(OWNER)


@pytest.mark.asyncio
async def test_reconcile_never_stops_paid_owner():
    """A paid (non-trial_local) owner's running container is never stopped —
    proven through the real veto ladder, not a boot-pass-side filter."""
    from main import _reconcile_trial_idle_containers

    pool = _pool()
    stop = AsyncMock()
    rows = [{"owner_id": OWNER, "status": "running"}]

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=rows,
        ),
        patch("main.get_gateway_pool", return_value=pool),
        _patch_billing({"subscription_status": "active"}),
        _patch_ecs(stop),
    ):
        await _reconcile_trial_idle_containers()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_never_stops_trial_owner_with_live_conn():
    """A trial_local owner with a live cross-task chat connection is never
    stopped — proven through the real veto ladder's live-chat-count check."""
    from main import _reconcile_trial_idle_containers

    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=1)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    pool = _pool(conn_svc=conn_svc)
    stop = AsyncMock()
    rows = [{"owner_id": OWNER, "status": "running"}]

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=rows,
        ),
        patch("main.get_gateway_pool", return_value=pool),
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await _reconcile_trial_idle_containers()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    stop.assert_not_called()
    conn_svc.count_live_chat_connections.assert_awaited_once_with(OWNER)


@pytest.mark.asyncio
async def test_reconcile_tolerates_empty():
    """Zero running rows -> no-op, no error. Matches
    _resume_provisioning_transitions's own empty-rows test: the pool getter
    may still be called (same shape as that function's unconditional
    get_ecs_manager() call), but nothing is dispatched off of it."""
    from main import _reconcile_trial_idle_containers

    pool = MagicMock()
    pool._maybe_stop_trial_container = AsyncMock()

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("main.get_gateway_pool", return_value=pool),
    ):
        await _reconcile_trial_idle_containers()

    pool._maybe_stop_trial_container.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_tolerates_ddb_failure():
    """get_by_status raising MUST NOT crash backend startup -- logged and
    shrugged off, matching _resume_provisioning_transitions's posture."""
    from main import _reconcile_trial_idle_containers

    with (
        patch(
            "main.container_repo.get_by_status",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ddb transient"),
        ),
        patch("main.get_gateway_pool") as mock_get_pool,
        patch("main.logger.warning") as mock_warn,
    ):
        # Must not raise.
        await _reconcile_trial_idle_containers()

    mock_get_pool.assert_not_called()
    mock_warn.assert_called_once()
