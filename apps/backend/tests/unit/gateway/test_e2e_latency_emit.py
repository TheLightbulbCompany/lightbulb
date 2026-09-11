"""Tests for the chat.e2e.latency SLO emission in GatewayConnection.

The span is no longer correlated by a bespoke run_id->start map. Instead the
END is emitted from the pool's existing per-session active-run window: the
dispatcher's ``mark_run_active`` stamps ``started_at`` (BEFORE the chat.send
RPC is forwarded) and the chat-terminal handler in ``_handle_message`` reads it
back at the clear site and emits ``chat.e2e.latency``.

Driven through the real ``_handle_message`` seam (same fixture/pattern as
test_lifecycle_billing.py) so the emit is exercised in its true position —
after all frame forwarding, gated on the captured ``started_at``.

Covers:
  - A primary-run TRUE terminal (final) with a pre-populated active-run
    started_at emits chat.e2e.latency with a positive Milliseconds value and
    no dimensions.
  - A terminal for a session_key with NO active-run entry emits nothing.
  - A side (`/btw`) run terminal emits nothing (it owns no active-run window).
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def conn():
    """Minimal GatewayConnection with enough state to drive chat-terminal handling."""
    from core.gateway.connection_pool import GatewayConnection

    c = GatewayConnection.__new__(GatewayConnection)
    c.user_id = "org_1"
    c.owner_id = "org_1"
    c._frontend_connections = set()
    c._pending_rpcs = {}
    c._management_api = MagicMock()
    c._billing_tasks = set()
    c._conn_svc = MagicMock()
    c._conn_svc.query_by_owner = AsyncMock(return_value=[])
    c._recipients_cache = None
    c._recipients_cache_ts = 0.0
    c._active_runs = {}
    c._terminal_run_ids = {}
    c._analytics_tasks = set()
    c._run_state = {}
    c._side_run_ids = {}
    c._run_text = {}
    c._parked = False
    c._parked_watchdog_task = None
    # Run-completion observer registry (INV-5 bootstrap dispatcher). Not
    # exercised by these latency tests, but _handle_message's TRUE-terminal
    # branch always pops from it now.
    c._run_completion_callbacks = {}
    c._run_completion_tasks = set()
    return c


@pytest.mark.asyncio
async def test_primary_terminal_emits_positive_latency(conn):
    """A primary-run final with a tracked started_at emits the SLO metric in ms."""
    session_key = "agent:main:main"
    # mark_run_active stamps started_at BEFORE the chat.send RPC; simulate a
    # turn that started ~10ms ago so elapsed is comfortably positive.
    conn._active_runs[session_key] = {
        "started_at": time.monotonic() - 0.01,
        "last_event_at": time.monotonic(),
        "agent_id": "main",
        "awaiting_approval": False,
    }
    payload = {
        "sessionKey": session_key,
        "state": "final",
        "runId": "run-1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }

    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with (
        patch("core.gateway.connection_pool.put_metric", side_effect=fake_put_metric),
        patch.object(conn, "_publish_room_frame", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})

    latency = [c for c in captured if c[0] == "chat.e2e.latency"]
    assert len(latency) == 1, "exactly one chat.e2e.latency emission"
    name, value, unit, dimensions = latency[0]
    assert unit == "Milliseconds"
    assert value > 0, "elapsed latency must be a positive value"
    # No dimensions — alarm W9 reads chat.e2e.latency on {env,service} only
    # (auto-injected by put_metric); a custom dim would create an un-alarmed stream.
    assert dimensions is None
    # The active-run window was cleared on the true terminal (no leak).
    assert session_key not in conn._active_runs


@pytest.mark.asyncio
async def test_terminal_without_active_run_emits_nothing(conn):
    """A terminal for a session with no tracked started_at is a silent no-op."""
    payload = {
        "sessionKey": "agent:main:main",
        "state": "final",
        "runId": "run-untracked",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }

    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with (
        patch("core.gateway.connection_pool.put_metric", side_effect=fake_put_metric),
        patch.object(conn, "_publish_room_frame", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})

    assert not [c for c in captured if c[0] == "chat.e2e.latency"], (
        "no latency emitted when the session had no active-run started_at"
    )


@pytest.mark.asyncio
async def test_side_run_terminal_emits_nothing(conn):
    """A side (`/btw`) run terminal owns no active-run window → no latency emit.

    The side run shares the primary run's session_key on the wire, so even if a
    primary window exists it must NOT be consumed/measured by the side run's
    terminal (the side terminal is gated out before the clear site).
    """
    session_key = "agent:main:main"
    run_id = "side-run-1"
    conn._side_run_ids[run_id] = None
    # A primary window IS present for the shared key — proving the side path
    # never reads/clears it (the primary run continues).
    conn._active_runs[session_key] = {
        "started_at": time.monotonic() - 0.01,
        "last_event_at": time.monotonic(),
        "agent_id": "main",
        "awaiting_approval": False,
    }
    payload = {
        "sessionKey": session_key,
        "state": "final",
        "runId": run_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "by the way"}]},
    }

    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with (
        patch("core.gateway.connection_pool.put_metric", side_effect=fake_put_metric),
        patch.object(conn, "_publish_room_frame", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})

    assert not [c for c in captured if c[0] == "chat.e2e.latency"], "side run must not emit latency"
    # The primary window is left intact — the side terminal never touched it.
    assert session_key in conn._active_runs
