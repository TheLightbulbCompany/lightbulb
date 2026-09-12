"""Tests for the free-tier $3/day cap wired into the live chat path.

``chat_gate.gate_chat`` Layer 2 is a **read-only** check of the
daily-spend counter (via ``trial_cap.reason_for_account``). It places NO
reserve and writes nothing — the counter itself has two writers, both
outside this backend: the bedrock-gate settles it first (JS, hot inference
path) and the meter Lambda backstops on invocations the gate missed, with
exactly-once guaranteed by the shared ``requestId`` dedup table rather than
single-writer discipline. The meter is also the authoritative enforcement
(revoke the owner's inference profile + ``desiredCount=0``). Blocking here
is UX.

The predicate is ``subscription_status == "trial_local"``. The container row's
``provider_choice`` is NOT consulted: the moment billing flips to paid, the
owner is off the cap by definition (their spend lands on Metronome), and the
meter keys on the owner's inference profile, not on any container-row stamp.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services import trial_cap
from core.services.chat_gate import gate_chat

from core.gateway.connection_pool import GatewayConnection


@pytest.mark.asyncio
async def test_gate_blocks_trial_owner_at_cap():
    """A trial_local owner at/over the daily cap is blocked with the
    trial_daily_cap code — the turn must never reach OpenClaw.

    Also the trial_cap_reached telemetry site: the block fires the
    once-per-owner-per-day PostHog capture."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ) as cap_mock,
        patch(
            "core.observability.product_analytics.capture_once_per_owner_per_day",
        ) as capture_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is True
    assert gate["code"] == "trial_daily_cap"
    assert isinstance(gate["message"], str) and gate["message"]
    cap_mock.assert_awaited_once_with("u", {"subscription_status": "trial_local"})
    capture_mock.assert_called_once_with(owner_id="u", event="trial_cap_reached")


@pytest.mark.asyncio
async def test_gate_allows_trial_owner_under_cap_and_writes_nothing():
    """An under-cap trial owner passes. gate_chat itself writes nothing to
    the counter — add_trial_daily_spend (zero production callers) is deleted,
    so that guard is now structural rather than a runtime mock assertion."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.repositories.billing_repo.get_trial_daily_spend",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "core.observability.product_analytics.capture_once_per_owner_per_day",
        ) as capture_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    capture_mock.assert_not_called()  # telemetry fires only on the blocked branch


@pytest.mark.asyncio
async def test_gate_does_not_read_the_container_row():
    """The cap keys on subscription_status alone. The container row's
    provider_choice stamp is gone — gate_chat must not read it (a read would
    resurrect the deleted upgrade-window coupling)."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(return_value=None),
        ),
        patch("core.repositories.container_repo.get_by_owner_id", new=AsyncMock()) as container_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    container_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_owner_is_never_capped():
    """A paid (bedrock_claude) owner never hits the cap check at all — the
    free tier's cap is a trial_local-only mechanism. Layer 3 (Metronome
    balance) still runs."""
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
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ) as cap_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    cap_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_owner_with_stale_trial_end_is_not_expired_or_capped():
    """Post-conversion: subscription_status is 'active' but the row still
    carries a trial_end left over from the local trial (and, for a few
    minutes, the still-trial container). The owner must NOT be told their
    trial expired, and must NOT be capped — both are gated on trial_local."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "trial_end": int(time.time()) - 60,
                    "provider_choice": "chatgpt_oauth",
                }
            ),
        ),
        patch(
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ) as cap_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate == {"blocked": False}
    cap_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_blocks_expired_trial_before_the_cap_check():
    """An owner still on subscription_status='trial_local' whose trial_end has
    passed is blocked with code=trial_expired BEFORE the cap read."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "trial_local",
                    "trial_end": int(time.time()) - 60,
                }
            ),
        ),
        patch(
            "core.services.trial_cap.billing_repo.get_trial_daily_spend",
            new=AsyncMock(return_value=0),
        ) as spend_mock,
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is True
    assert gate["code"] == "trial_expired"
    assert isinstance(gate["message"], str) and gate["message"]
    # Expiry short-circuits: the daily-spend counter is never even read.
    spend_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_chat_cap_read_raise_propagates():
    """A raise inside the cap read (a DDB network call) must propagate rather
    than be swallowed into a permissive {"blocked": False} — an errored gate
    is the safe direction for a cost cap."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch(
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(side_effect=RuntimeError("dynamodb boom")),
        ),
    ):
        with pytest.raises(RuntimeError, match="dynamodb boom"):
            await gate_chat(user_id="u", owner_id="u", session_key="sk1")


# --------- Post-turn: the usage path must NOT write the cap counter ---------


def _make_connection_with_rpc(session: dict, user_id: str = "u_1") -> GatewayConnection:
    """A GatewayConnection whose sessions.describe RPC returns *session*."""
    conn = object.__new__(GatewayConnection)
    conn.user_id = user_id
    conn.send_rpc = AsyncMock()
    conn.wait_for_response = AsyncMock(return_value={"session": session})
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
@pytest.mark.parametrize(
    "session_key",
    [
        "agent:main:user_bob",  # webchat
        "agent:agent_1:paperclip:issue:42",  # Paperclip/Teams
        "agent:agent_1:cron:job_7",  # cron
    ],
)
async def test_usage_path_never_writes_the_cap_counter(session_key):
    """``_fetch_and_record_usage`` fires for EVERY run source. It records
    analytics usage — but must never write to the daily-spend counter itself
    (add_trial_daily_spend, the only backend writer, had zero production
    callers and is deleted, so that half of the guard is now structural)."""
    session = _bedrock_session(key=session_key)
    conn = _make_connection_with_rpc(session, user_id="owner_1")

    with patch("core.services.usage_service.record_usage", new=AsyncMock()) as record_mock:
        await conn._fetch_and_record_usage(session_key, member_user_id="member_1")

    record_mock.assert_awaited_once()  # analytics still recorded


@pytest.mark.asyncio
async def test_trial_owner_under_cap_never_reaches_the_credits_layer():
    """A trial owner under the cap is ADMITTED, and Layer 3 is never consulted.

    This is the free-tier lockout guard. `/billing/trial-checkout` writes
    provider_choice="bedrock_claude" SYNCHRONOUSLY before redirecting to
    Stripe, so anyone who opens Checkout and closes the tab keeps that value
    while staying trial_local. Trial owners are deliberately never given a
    Metronome ledger (ensure_metronome_customer short-circuits them), so if
    Layer 2 fell through, the now-fail-closed credits layer would find no
    ledger and block them out of a fully-funded free tier — permanently, since
    the midnight cap reset does not clear a credits verdict.

    Asserting `is_owner_blocked` is NEVER AWAITED is the point: a return value
    assertion alone would still pass if the layer ran and happened to admit.
    """
    blocked_mock = AsyncMock(return_value=True)  # would block if ever consulted
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "trial_local",
                    # The abandoned-checkout residue.
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.services.trial_cap.reason_for_account", new=AsyncMock(return_value=None)),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked_mock),
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is False
    blocked_mock.assert_not_awaited()
