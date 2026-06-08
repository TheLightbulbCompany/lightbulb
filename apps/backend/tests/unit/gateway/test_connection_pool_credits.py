"""Tests for the credit-aware gateway hooks.

Two surfaces:

- Pool-level ``GatewayConnectionPool.gate_chat`` — pre-chat hard-stop for
  card-3 users when balance ≤ 0; cards 1+2 + legacy users always pass.

- Connection-level ``GatewayConnection._fetch_and_record_usage`` — invoked
  on the chat-end lifecycle event; after the meter cutover it records the
  analytics usage counters ONLY. Credit deduction moved OUT of the
  container path: the AWS Bedrock invocation-log meter is now the sole
  biller (every-run, cache-correct, exactly-once on requestId).

``provider_choice`` is read from the ``billing_accounts`` row (per
owner_id), NOT from the ``users`` row. Credit balance reads stay keyed on
the container owner.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnectionPool


# --------- Task 4: pool.gate_chat ---------


@pytest.mark.asyncio
async def test_card3_with_zero_balance_blocked():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=0)
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
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "Top up" in result["message"]


@pytest.mark.asyncio
async def test_card3_with_positive_balance_allowed():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=5_000_000)
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
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_card3_uses_consistent_read():
    """Top-ups landed via webhook must unblock the next chat without
    eventual-consistency lag — gate_chat reads with consistent=True."""
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=10_000_000)
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
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        await pool.gate_chat(user_id="u_1", owner_id="o_1")
    _, kwargs = ledger.get_balance.call_args
    assert kwargs.get("consistent") is True


@pytest.mark.asyncio
async def test_card3_gate_keyed_on_owner_for_org_credits_pool():
    """After the org-pooled-credits fix, both provider_choice (from
    billing_repo) AND credit balance (from the ledger) are keyed on
    owner_id. Org members all draw from one pool funded by the admin —
    without this, members see balance=0 and get out_of_credits.
    """
    pool = GatewayConnectionPool(management_api=None)
    billing_mock = AsyncMock(
        return_value={
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }
    )
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=1_000_000)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="member_X", owner_id="org_Y")

    assert result == {"blocked": False}
    billing_mock.assert_awaited_once_with("org_Y")
    balance_args, _ = ledger.get_balance.call_args
    assert balance_args[0] == "org_Y"


@pytest.mark.asyncio
async def test_card1_oauth_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "chatgpt_oauth",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    ledger.get_balance.assert_not_called()


@pytest.mark.asyncio
async def test_card2_byo_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "byo_key",
                }
            ),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    ledger.get_balance.assert_not_called()


@pytest.mark.asyncio
async def test_billing_row_without_provider_choice_never_gated():
    """Legacy billing rows without provider_choice shouldn't be locked
    out — they keep working until provider is set."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                # legacy row carries stripe_subscription_id, no
                # subscription_status, no provider_choice
                "stripe_subscription_id": "sub_legacy",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_legacy", owner_id="o_legacy")
    assert result == {"blocked": False}


# --------- Audit M1: subscription-status gate (any provider) ---------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused", "past_due"],
)
async def test_inactive_subscription_blocks_chat_regardless_of_provider(status):
    """A canceled / past_due / unpaid subscription blocks chat for ANY
    provider — including chatgpt_oauth and byo_key, where the LLM cost
    is on the user but the container compute is on us."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"subscription_status": status}),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing"])
async def test_active_or_trialing_subscription_does_not_trigger_sub_gate(status):
    """active/trialing pass the subscription gate (then fall through to
    the provider_choice + balance check). Without provider_choice set,
    Layer-2 returns blocked=False."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "subscription_status": status,
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_missing_billing_row_blocks_chat_when_owner_id_provided():
    """An authenticated user whose billing row doesn't exist at all
    (never went through trial-checkout, or got cleaned up out-of-band)
    must be blocked. Otherwise non-Bedrock providers would chat freely
    with us still on the hook for container compute."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_legacy_billing_row_without_status_does_not_block():
    """A pre-Plan-3 billing row may have stripe_subscription_id but no
    subscription_status backfilled — those should NOT be locked out
    mid-deploy."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "stripe_subscription_id": "sub_legacy",
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1")
    assert result == {"blocked": False}
