"""GET /container/status opportunistically reconciles team seats for org owners."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import container  # noqa: E402


@pytest.mark.asyncio
async def test_status_dispatches_seat_sync_for_org_owner(monkeypatch):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"status": "running", "service_name": "svc"}, "1.2.3.4"))
    ecs.get_service_status = AsyncMock(return_value=None)
    monkeypatch.setattr(container, "get_ecs_manager", lambda: ecs)
    # Isolate from gateway-pool internals — the status endpoint's phase resolver
    # is not under test here.
    monkeypatch.setattr(container, "_safe_phase", lambda c, o: "ready")

    dispatched = []
    monkeypatch.setattr(container.asyncio, "create_task", lambda coro: dispatched.append(coro) or MagicMock())
    # sync_team_seat_quantity is a plain MagicMock so calling it returns a
    # sentinel we can assert was scheduled (not awaited inline). Force
    # ``new=`` here: unittest.mock.patch auto-substitutes an AsyncMock for
    # coroutine-function targets (asyncio.iscoroutinefunction detection),
    # which would make the call return an unawaited coroutine instead of
    # the sentinel we want captured in ``dispatched``.
    sentinel = MagicMock(name="seat_sync_coro")
    mock_sync = MagicMock(return_value=sentinel)
    with patch("core.services.billing_service.sync_team_seat_quantity", new=mock_sync):
        auth = AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin")
        await container.container_status(auth=auth)

    mock_sync.assert_called_once_with("org_1")
    assert sentinel in dispatched
    # Close any real coroutine objects (e.g. ensure_user_ready) that the org
    # branch also scheduled, to silence "coroutine never awaited" warnings.
    for coro in dispatched:
        if hasattr(coro, "close"):
            coro.close()
