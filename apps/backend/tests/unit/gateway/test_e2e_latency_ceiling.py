"""Tests for the chat.e2e.latency late-terminal ceiling guard.

A normal terminal (elapsed well under the ceiling) emits chat.e2e.latency. A
late/stale terminal (elapsed over _E2E_LATENCY_CEILING_MS — e.g. a hung run or
a terminal re-delivered after a reconnect) must NOT emit chat.e2e.latency
(which would inflate the p99 and false-fire W9); it bumps chat.run.abandoned
instead.

Driven through the real ``_handle_message`` seam, mirroring
test_e2e_latency_emit.py.
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
    c._run_state = {}
    c._side_run_ids = {}
    c._parked = False
    c._parked_watchdog_task = None
    return c


def _payload(session_key: str, run_id: str) -> dict:
    return {
        "sessionKey": session_key,
        "state": "final",
        "runId": run_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }


@pytest.mark.asyncio
async def test_normal_elapsed_emits_latency(conn):
    """A turn that started ~2s ago emits chat.e2e.latency, not abandoned."""
    session_key = "agent:main:main"
    conn._active_runs[session_key] = {
        "started_at": time.monotonic() - 2.0,
        "last_event_at": time.monotonic(),
        "agent_id": "main",
        "awaiting_approval": False,
    }

    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with (
        patch("core.gateway.connection_pool.put_metric", side_effect=fake_put_metric),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": _payload(session_key, "run-ok")})

    latency = [c for c in captured if c[0] == "chat.e2e.latency"]
    abandoned = [c for c in captured if c[0] == "chat.run.abandoned"]
    assert len(latency) == 1, "exactly one chat.e2e.latency for a normal turn"
    assert latency[0][1] > 0
    assert abandoned == [], "a normal turn is not abandoned"


@pytest.mark.asyncio
async def test_over_ceiling_elapsed_does_not_emit_latency(conn):
    """A terminal arriving 20 min after start is a stale outlier — it must NOT
    emit chat.e2e.latency (would inflate the p99 / false-fire W9) and must bump
    chat.run.abandoned instead."""
    session_key = "agent:main:main"
    conn._active_runs[session_key] = {
        # 20 min ago — well over the 10-min _E2E_LATENCY_CEILING_MS.
        "started_at": time.monotonic() - (20 * 60),
        "last_event_at": time.monotonic(),
        "agent_id": "main",
        "awaiting_approval": False,
    }

    captured: list[tuple] = []

    def fake_put_metric(name, value=1.0, unit="Count", dimensions=None):
        captured.append((name, value, unit, dimensions))

    with (
        patch("core.gateway.connection_pool.put_metric", side_effect=fake_put_metric),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": _payload(session_key, "run-stale")})

    latency = [c for c in captured if c[0] == "chat.e2e.latency"]
    abandoned = [c for c in captured if c[0] == "chat.run.abandoned"]
    assert latency == [], "over-ceiling terminal must NOT emit chat.e2e.latency"
    assert len(abandoned) == 1, "over-ceiling terminal bumps chat.run.abandoned"
