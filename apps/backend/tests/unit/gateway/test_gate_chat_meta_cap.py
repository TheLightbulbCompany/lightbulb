"""Tests for the free-tier $3/day Meta cost cap wired into the live chat
path (Task 6):

- Pre-send gate: ``GatewayConnectionPool.gate_chat`` reserves a worst-case
  turn cost for ``trial_local`` owners (``meta_cap.reserve_or_block``) and
  blocks with ``code="meta_daily_cap"`` if the owner is already at/over the
  daily cap. Non-``trial_local`` owners (paid cards 1-3) never reserve —
  the cap only applies to the card-less free tier, whose LLM cost is on
  Isol8 with no other backstop.

- Post-turn settle: ``GatewayConnection._fetch_and_record_usage`` always
  calls ``meta_cap.reconcile`` in a ``finally``, regardless of which
  early-return / exception path the usage-fetch flow takes, so a reserve
  is never orphaned.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool

# --------- Pre-send gate: gate_chat + meta_cap.reserve_or_block ---------


@pytest.mark.asyncio
async def test_gate_blocks_trial_local_at_cap():
    """A trial_local owner already at/over the daily cap gets blocked with
    the meta_daily_cap code — the turn must never reach OpenClaw."""
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.meta_cap.reserve_or_block",
            new=AsyncMock(return_value=True),
        ) as reserve_mock,
    ):
        gate = await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is True
    assert gate["code"] == "meta_daily_cap"
    assert isinstance(gate["message"], str) and gate["message"]
    reserve_mock.assert_awaited_once_with("u", "sk1")


@pytest.mark.asyncio
async def test_gate_allows_trial_local_under_cap():
    """A trial_local owner under the cap gets a reserve placed and the
    turn proceeds — Layer 3 (card-3 balance) doesn't apply since
    trial_local rows never carry provider_choice=bedrock_claude."""
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.meta_cap.reserve_or_block",
            new=AsyncMock(return_value=False),
        ) as reserve_mock,
    ):
        gate = await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    reserve_mock.assert_awaited_once_with("u", "sk1")


@pytest.mark.asyncio
async def test_paid_owner_never_reserves():
    """An active-subscription (paid) owner never calls reserve_or_block —
    the Meta cap is a free-tier-only mechanism."""
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=False)),
        patch(
            "core.services.meta_cap.reserve_or_block",
            new=AsyncMock(return_value=False),
        ) as reserve_mock,
    ):
        gate = await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    reserve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_chat_reserve_or_block_raise_propagates():
    """A raise inside reserve_or_block (e.g. its rollback ADD is a network
    call) must propagate rather than be swallowed into a permissive
    {"blocked": False} — an errored gate is the safe direction."""
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.meta_cap.reserve_or_block",
            new=AsyncMock(side_effect=RuntimeError("dynamodb boom")),
        ),
    ):
        with pytest.raises(RuntimeError, match="dynamodb boom"):
            await pool.gate_chat(user_id="u", owner_id="u", session_key="sk1")


# --------- Post-turn settle: _fetch_and_record_usage + meta_cap.reconcile ---------


def _make_connection_with_rpc(session: dict, user_id: str = "u_1") -> GatewayConnection:
    """A GatewayConnection whose sessions.list RPC returns one *session* row."""
    conn = object.__new__(GatewayConnection)
    conn.user_id = user_id
    conn.send_rpc = AsyncMock()
    conn.wait_for_response = AsyncMock(return_value={"sessions": [session]})
    return conn


def _bedrock_session(key: str | None = None) -> dict:
    import uuid

    return {
        "key": key or f"agent:main:{uuid.uuid4().hex}",
        "inputTokens": 10,
        "outputTokens": 5,
        "model": "amazon-bedrock/anthropic.claude-sonnet-4-6",
        "estimatedCostUsd": 1.5,
    }


@pytest.mark.asyncio
async def test_reconcile_called_post_turn_with_actual_cost():
    """A normal turn reconciles with the actual raw cost in microcents
    computed from estimatedCostUsd (1.5 USD -> 1_500_000 microcents)."""
    session = _bedrock_session()
    conn = _make_connection_with_rpc(session, user_id="owner_1")

    with (
        patch("core.services.usage_service.record_usage", new=AsyncMock()),
        patch("core.services.meta_cap.reconcile", new=AsyncMock()) as reconcile_mock,
    ):
        await conn._fetch_and_record_usage(session["key"], member_user_id="member_1")

    reconcile_mock.assert_awaited_once_with("owner_1", session["key"], 1_500_000)


@pytest.mark.asyncio
async def test_reconcile_called_even_when_session_not_found():
    """sessions.list not containing our session_key is an early return —
    reconcile must still fire (with cost=0, so meta_cap fails closed and
    keeps the worst-case reserve rather than silently dropping it)."""
    conn = object.__new__(GatewayConnection)
    conn.user_id = "owner_1"
    conn.send_rpc = AsyncMock()
    conn.wait_for_response = AsyncMock(return_value={"sessions": []})

    with patch("core.services.meta_cap.reconcile", new=AsyncMock()) as reconcile_mock:
        await conn._fetch_and_record_usage("agent:main:missing", member_user_id="member_1")

    reconcile_mock.assert_awaited_once_with("owner_1", "agent:main:missing", 0)


@pytest.mark.asyncio
async def test_reconcile_called_even_when_record_usage_raises():
    """record_usage failing must not skip settling the reserve."""
    session = _bedrock_session()
    conn = _make_connection_with_rpc(session, user_id="owner_1")

    with (
        patch(
            "core.services.usage_service.record_usage",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch("core.services.meta_cap.reconcile", new=AsyncMock()) as reconcile_mock,
    ):
        await conn._fetch_and_record_usage(session["key"], member_user_id="member_1")

    reconcile_mock.assert_awaited_once_with("owner_1", session["key"], 1_500_000)


@pytest.mark.asyncio
async def test_reconcile_exception_does_not_mask_usage_flow():
    """A reconcile that itself raises must not propagate out of
    _fetch_and_record_usage (it's a fire-and-forget background hook off
    the chat-end lifecycle event; the caller doesn't expect it to throw)."""
    session = _bedrock_session()
    conn = _make_connection_with_rpc(session, user_id="owner_1")

    with (
        patch("core.services.usage_service.record_usage", new=AsyncMock()),
        patch(
            "core.services.meta_cap.reconcile",
            new=AsyncMock(side_effect=RuntimeError("reconcile boom")),
        ),
    ):
        # Must not raise.
        await conn._fetch_and_record_usage(session["key"], member_user_id="member_1")
