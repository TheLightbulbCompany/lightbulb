"""Tests for lifecycle/end billing trigger in GatewayConnection."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def conn():
    """Build a minimal GatewayConnection instance with enough state for billing tests."""
    from core.gateway.connection_pool import GatewayConnection

    c = GatewayConnection.__new__(GatewayConnection)
    c.user_id = "org_1"
    c._frontend_connections = set()
    c._pending_rpcs = {}
    c._management_api = MagicMock()
    c._billing_tasks = set()
    # Active-run tracking state (see _on_reader_loop_exit). These are set by
    # __init__ in real usage; tests that use __new__ to bypass init have to
    # populate them by hand.
    c._active_runs = {}
    c._parked = False
    c._parked_watchdog_task = None
    return c


@pytest.mark.asyncio
async def test_lifecycle_end_for_channel_dm_triggers_billing_with_linked_member(conn):
    payload = {
        "runId": "run-1",
        "stream": "lifecycle",
        "data": {"phase": "end"},
        "sessionKey": "agent:sales:telegram:sales:direct:99999",
    }

    with (
        patch(
            "core.gateway.connection_pool.channel_link_repo.get_by_peer",
            AsyncMock(return_value={"member_id": "user_bob", "peer_id": "99999"}),
        ),
        patch.object(
            conn,
            "_fetch_and_record_usage",
            AsyncMock(),
        ) as mock_fetch,
    ):
        conn._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": payload,
            }
        )
        # Yield to the loop so the task created by _record_usage_from_session runs
        await asyncio.sleep(0)

    mock_fetch.assert_awaited()
    call = mock_fetch.call_args
    assert call[0][0] == "agent:sales:telegram:sales:direct:99999"
    assert call[0][1] == "user_bob"


@pytest.mark.asyncio
async def test_lifecycle_end_for_channel_dm_unlinked_falls_back_to_owner(conn):
    payload = {
        "runId": "run-2",
        "stream": "lifecycle",
        "data": {"phase": "end"},
        "sessionKey": "agent:sales:telegram:sales:direct:66666",
    }

    with (
        patch(
            "core.gateway.connection_pool.channel_link_repo.get_by_peer",
            AsyncMock(return_value=None),
        ),
        patch.object(
            conn,
            "_fetch_and_record_usage",
            AsyncMock(),
        ) as mock_fetch,
    ):
        conn._handle_message({"type": "event", "event": "agent", "payload": payload})
        # Yield to the loop so the task created by _record_usage_from_session runs
        await asyncio.sleep(0)

    mock_fetch.assert_awaited()
    assert mock_fetch.call_args[0][1] == "org_1"


@pytest.mark.asyncio
async def test_lifecycle_end_for_org_webchat_uses_clerk_member(conn):
    payload = {
        "runId": "run-3",
        "stream": "lifecycle",
        "data": {"phase": "end"},
        "sessionKey": "agent:main:user_bob",
    }

    with (
        patch(
            "core.gateway.connection_pool.channel_link_repo.get_by_peer",
            AsyncMock(return_value=None),
        ),
        patch.object(
            conn,
            "_fetch_and_record_usage",
            AsyncMock(),
        ) as mock_fetch,
    ):
        conn._handle_message({"type": "event", "event": "agent", "payload": payload})
        # Yield to the loop so the task created by _record_usage_from_session runs
        await asyncio.sleep(0)

    mock_fetch.assert_awaited()
    assert mock_fetch.call_args[0][1] == "user_bob"


@pytest.mark.asyncio
async def test_lifecycle_error_does_not_trigger_billing(conn):
    payload = {
        "runId": "run-4",
        "stream": "lifecycle",
        "data": {"phase": "error"},  # error, not end
        "sessionKey": "agent:main:telegram:main:direct:12345",
    }

    with patch.object(conn, "_fetch_and_record_usage", AsyncMock()) as mock_fetch:
        conn._handle_message({"type": "event", "event": "agent", "payload": payload})
        # Yield to the loop so any spurious tasks have a chance to run
        await asyncio.sleep(0)

    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_session_key_bills_under_owner_not_literal_channel(conn):
    """Regression: pre-existing parser bug wrote member:telegram:{period}."""
    payload = {
        "runId": "run-5",
        "stream": "lifecycle",
        "data": {"phase": "end"},
        "sessionKey": "agent:main:telegram:group:-100123",
    }

    with patch.object(conn, "_fetch_and_record_usage", AsyncMock()) as mock_fetch:
        conn._handle_message({"type": "event", "event": "agent", "payload": payload})
        # Yield to the loop so the task created by _record_usage_from_session runs
        await asyncio.sleep(0)

    mock_fetch.assert_awaited()
    assert mock_fetch.call_args[0][1] != "telegram"
    assert mock_fetch.call_args[0][1] == "org_1"


@pytest.mark.asyncio
async def test_chat_final_no_longer_calls_billing(conn):
    """chat.final still fires UI signals but not billing (lifecycle is the new trigger)."""
    payload = {
        "sessionKey": "agent:main:main",
        "state": "final",
        "runId": "run-1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }

    with (
        patch.object(conn, "_fetch_and_record_usage", AsyncMock()) as mock_fetch,
        patch.object(conn, "_forward_to_frontends") as mock_forward,
    ):
        conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        # Yield for consistency with the other tests
        await asyncio.sleep(0)

    mock_fetch.assert_not_awaited()
    # But UI signal IS forwarded ({"type": "done"})
    any_done = any(isinstance(c[0][0], dict) and c[0][0].get("type") == "done" for c in mock_forward.call_args_list)
    assert any_done
