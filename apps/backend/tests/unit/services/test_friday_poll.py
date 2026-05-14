"""Unit tests for friday_poll.maybe_run_friday_poll_pass."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import friday_poll
from core.services.friday_poll import (
    _POLL_INTERVAL_SECONDS,
    maybe_run_friday_poll_pass,
)
from core.services.paperclip_admin_client import PaperclipApiError


@pytest.fixture(autouse=True)
def _reset_status_cache():
    """Status cache is module-level — reset between tests."""
    friday_poll._STATUS_CACHE.clear()
    yield
    friday_poll._STATUS_CACHE.clear()


@pytest.fixture
def repo():
    r = MagicMock()
    # _iter_active_companies reads repo._table() then calls repo.get() per row.
    # The default table-scan returns one active row; repo.get returns a
    # MagicMock with status='active' and the right ids.
    table = MagicMock()
    table.scan = MagicMock(return_value={"Items": [{"user_id": "user_x"}], "LastEvaluatedKey": None})
    r._table = MagicMock(return_value=table)
    company = MagicMock(status="active", company_id="comp-1", org_id="user_x")
    r.get = AsyncMock(return_value=company)
    return r


@pytest.fixture
def admin_client(monkeypatch):
    client = MagicMock()
    client._http = MagicMock()
    client.list_issues = AsyncMock(return_value=[])

    async def fake_cookie(_http):
        return "session=abc"

    monkeypatch.setattr(friday_poll, "get_admin_session_cookie", fake_cookie)
    return client


@pytest.mark.asyncio
async def test_first_call_runs_pass_and_sets_last_at(repo, admin_client):
    holder = [0.0]

    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    # last_at is set on success
    assert holder[0] > 0.0
    # Pass executed: list_issues called
    admin_client.list_issues.assert_awaited()


@pytest.mark.asyncio
async def test_second_call_within_interval_skips(repo, admin_client, monkeypatch):
    """If less than _POLL_INTERVAL_SECONDS has elapsed, the pass is a no-op."""
    # First call sets the timer
    holder = [0.0]
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    first_at = holder[0]
    admin_client.list_issues.reset_mock()

    # Second call immediately after — should skip (no time travel needed
    # because real elapsed time is sub-second).
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    assert holder[0] == first_at  # unchanged
    admin_client.list_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_detects_issue_status_change(repo, admin_client, caplog):
    """When an issue's status changes between passes, log an info line."""
    import logging

    caplog.set_level(logging.INFO, logger="core.services.friday_poll")

    # First pass: issue is 'open'
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-1", "status": "open"}]
    )
    holder = [0.0]
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    # Force the timer back so the second pass runs
    holder[0] = 0.0

    # Second pass: same issue is now 'in_progress'
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-1", "status": "in_progress"}]
    )
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    assert any(
        "status change detected" in rec.message and "open" in rec.message and "in_progress" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_no_change_when_status_unchanged(repo, admin_client, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="core.services.friday_poll")

    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-1", "status": "open"}]
    )
    holder = [0.0]

    # Two passes back-to-back with the same status
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    holder[0] = 0.0  # bypass throttle
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    assert not any("status change detected" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_swallows_paperclip_outage(repo, admin_client):
    """A Paperclip outage logs + continues; never raises out of the pass."""
    admin_client.list_issues = AsyncMock(
        side_effect=PaperclipApiError("upstream down", 503, None)
    )
    holder = [0.0]

    # Should not raise
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )


@pytest.mark.asyncio
async def test_swallows_transport_error(repo, admin_client):
    admin_client.list_issues = AsyncMock(
        side_effect=httpx.ConnectError("upstream down")
    )
    holder = [0.0]

    # Should not raise
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )


@pytest.mark.asyncio
async def test_skips_when_no_active_companies(repo, admin_client):
    """No companies returned from scan → pass returns silently, no list_issues call."""
    table = MagicMock()
    table.scan = MagicMock(return_value={"Items": [], "LastEvaluatedKey": None})
    repo._table = MagicMock(return_value=table)
    holder = [0.0]

    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    admin_client.list_issues.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wake-Friday path
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_pool(monkeypatch):
    """Mock the singleton gateway pool + ecs_manager that the wake uses."""
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True})
    pool.mark_run_active = MagicMock()

    ecs_manager = MagicMock()
    ecs_manager.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "tok-1"}, "10.0.0.42")
    )

    # core.containers exports get_gateway_pool + get_ecs_manager as singletons.
    # We monkeypatch them at the module level since friday_poll imports them
    # lazily inside _wake_friday_for_status_changes.
    import core.containers as containers_pkg

    monkeypatch.setattr(containers_pkg, "get_gateway_pool", lambda: pool, raising=False)
    monkeypatch.setattr(containers_pkg, "get_ecs_manager", lambda: ecs_manager, raising=False)
    return pool, ecs_manager


@pytest.mark.asyncio
async def test_wakes_friday_via_chat_send_on_status_change(
    repo, admin_client, gateway_pool
):
    """Status change triggers a chat.send RPC to Friday's session."""
    pool, _ecs = gateway_pool
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "open"}]
    )
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "friday-uuid", "role": "ceo", "name": "Friday"}]
    )
    holder = [0.0]

    # Pass 1: baseline (no change, no wake)
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    pool.send_rpc.assert_not_awaited()

    # Pass 2: status changed → wake fires
    holder[0] = 0.0
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "in_progress"}]
    )
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    pool.send_rpc.assert_awaited_once()
    kwargs = pool.send_rpc.await_args.kwargs
    assert kwargs["user_id"] == "user_x"
    assert kwargs["method"] == "chat.send"
    assert kwargs["ip"] == "10.0.0.42"
    assert kwargs["token"] == "tok-1"
    assert kwargs["params"]["sessionKey"] == "paperclip:poll:user_x"
    assert "issue-7" in kwargs["params"]["message"]
    assert "open" in kwargs["params"]["message"]
    assert "in_progress" in kwargs["params"]["message"]
    assert "idempotencyKey" in kwargs["params"]

    # Active-run window opened for the parked-vs-dead decision.
    pool.mark_run_active.assert_called_once_with(
        "user_x", "paperclip:poll:user_x", "friday-uuid"
    )


@pytest.mark.asyncio
async def test_skips_wake_when_container_unreachable(repo, admin_client, gateway_pool):
    pool, ecs = gateway_pool
    # No container provisioned yet (or scaled to zero, no IP).
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))

    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "open"}]
    )
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "friday-uuid", "role": "ceo"}]
    )
    holder = [0.0]

    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    holder[0] = 0.0
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "in_progress"}]
    )
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_wake_when_no_ceo_in_roster(repo, admin_client, gateway_pool):
    """Defensive: company has no CEO agent (user deleted Friday) → skip wake."""
    pool, _ecs = gateway_pool
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "open"}]
    )
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "u-pulse", "role": "specialist", "name": "Pulse"}]
    )
    holder = [0.0]

    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    holder[0] = 0.0
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "in_progress"}]
    )
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )

    pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_rpc_failure_does_not_propagate(repo, admin_client, gateway_pool):
    """A bad container shouldn't break the loop for other owners."""
    pool, _ecs = gateway_pool
    pool.send_rpc = AsyncMock(side_effect=RuntimeError("upstream gone"))

    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "open"}]
    )
    admin_client.list_agents = AsyncMock(
        return_value=[{"id": "friday-uuid", "role": "ceo"}]
    )
    holder = [0.0]

    # Baseline + change → wake attempt raises internally → swallowed
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
    holder[0] = 0.0
    admin_client.list_issues = AsyncMock(
        return_value=[{"id": "issue-7", "status": "in_progress"}]
    )
    # Should not raise
    await maybe_run_friday_poll_pass(
        paperclip_repo=repo, admin_client=admin_client, last_pass_at_holder=holder
    )
