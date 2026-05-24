"""Tests for the credit-aware gateway hooks.

Two surfaces:

- Pool-level ``GatewayConnectionPool.gate_chat`` — pre-chat hard-stop for
  card-3 users when balance ≤ 0; cards 1+2 + legacy users always pass.

- Connection-level ``GatewayConnection._maybe_deduct_credits`` — invoked
  from ``_fetch_and_record_usage`` after token counts come back from
  ``sessions.list``; deducts from credit_ledger for card-3 only with
  1.4x markup.

``provider_choice`` is read from the ``billing_accounts`` row (per
owner_id), NOT from the ``users`` row. Credit balance + deduction stay
keyed on the chatting member.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


# --------- Task 4: pool.gate_chat ---------


@pytest.mark.asyncio
async def test_card3_with_zero_balance_blocked():
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
        patch(
            "core.services.credit_ledger.get_balance",
            new=AsyncMock(return_value=0),
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "Top up" in result["message"]


@pytest.mark.asyncio
async def test_card3_with_positive_balance_allowed():
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
        patch(
            "core.services.credit_ledger.get_balance",
            new=AsyncMock(return_value=5_000_000),
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_card3_uses_consistent_read():
    """Top-ups landed via webhook must unblock the next chat without
    eventual-consistency lag — gate_chat reads with consistent=True."""
    pool = GatewayConnectionPool(management_api=None)
    get_balance_mock = AsyncMock(return_value=10_000_000)
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
        patch("core.services.credit_ledger.get_balance", new=get_balance_mock),
    ):
        await pool.gate_chat(user_id="u_1", owner_id="o_1")
    _, kwargs = get_balance_mock.call_args
    assert kwargs.get("consistent") is True


@pytest.mark.asyncio
async def test_card3_gate_keyed_on_owner_for_org_credits_pool():
    """After the org-pooled-credits fix, both provider_choice (from
    billing_repo) AND credit balance (from credit_ledger.get_balance) are
    keyed on owner_id. Org members all draw from one pool funded by the
    admin — without this, members see balance=0 and get out_of_credits.
    """
    pool = GatewayConnectionPool(management_api=None)
    billing_mock = AsyncMock(
        return_value={
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }
    )
    balance_mock = AsyncMock(return_value=1_000_000)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.services.credit_ledger.get_balance", new=balance_mock),
    ):
        result = await pool.gate_chat(user_id="member_X", owner_id="org_Y")

    assert result == {"blocked": False}
    billing_mock.assert_awaited_once_with("org_Y")
    balance_args, _ = balance_mock.call_args
    assert balance_args[0] == "org_Y"


@pytest.mark.asyncio
async def test_card1_oauth_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    balance_mock = AsyncMock()
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
        patch("core.services.credit_ledger.get_balance", new=balance_mock),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    balance_mock.assert_not_called()


@pytest.mark.asyncio
async def test_card2_byo_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    balance_mock = AsyncMock()
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
        patch("core.services.credit_ledger.get_balance", new=balance_mock),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1")
    assert result == {"blocked": False}
    balance_mock.assert_not_called()


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


# --------- Task 5: connection._maybe_deduct_credits ---------


def _make_connection(user_id: str = "u_1") -> GatewayConnection:
    """Construct a GatewayConnection without opening a real WebSocket.

    The deduct path only touches self.user_id; the WebSocket fields are
    irrelevant. We bypass __init__ side-effects with object.__new__.
    """
    conn = object.__new__(GatewayConnection)
    conn.user_id = user_id
    return conn


@pytest.mark.asyncio
async def test_deduct_for_card3_sonnet_with_markup():
    """Sonnet 4.6 = $3 input / $15 output per MTok.
    1000 input + 500 output → raw 10500 microcents → 1.4x markup → 14_700.
    """
    conn = _make_connection()
    deduct_mock = AsyncMock(return_value=8_000_000)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_1",
            model="amazon-bedrock/anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )

    deduct_mock.assert_awaited_once()
    _, kwargs = deduct_mock.call_args
    assert kwargs["amount_microcents"] == 14_700
    assert kwargs["raw_cost_microcents"] == 10_500
    assert kwargs["markup_multiplier"] == 1.4
    assert kwargs["chat_session_id"] == "sess_1"


@pytest.mark.asyncio
async def test_deduct_for_card3_opus_with_markup():
    """Opus 4.7 = $15 input / $75 output per MTok.
    1000 input + 500 output → raw 52_500 microcents → 1.4x → 73_500.
    """
    conn = _make_connection()
    deduct_mock = AsyncMock(return_value=0)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_opus",
            model="amazon-bedrock/anthropic.claude-opus-4-6-v1",
            input_tokens=1000,
            output_tokens=500,
        )
    _, kwargs = deduct_mock.call_args
    assert kwargs["amount_microcents"] == 73_500
    assert kwargs["raw_cost_microcents"] == 52_500


@pytest.mark.asyncio
async def test_deduct_keyed_on_owner_credits_pool():
    """After the org-pooled-credits fix, both provider_choice (from
    billing_repo) AND deduct (from credit_ledger.deduct) are keyed on
    the connection's owner — self.user_id IS the container owner per
    GatewayConnectionPool._create_connection. Without this, an org
    member's chat would draw against their personal user_id row instead
    of the org pool, leaving the admin's funded balance untouched.

    The member_user_id passed in is the actual chatting Clerk user — it
    must NOT be used as the ledger key (that was the bug pre-fix).
    """
    conn = _make_connection(user_id="org_Y")
    billing_mock = AsyncMock(return_value={"provider_choice": "bedrock_claude"})
    deduct_mock = AsyncMock(return_value=0)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="agent:main:member_X",
            model="amazon-bedrock/anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )

    billing_mock.assert_awaited_once_with("org_Y")
    deduct_mock.assert_awaited_once()
    args, _ = deduct_mock.call_args
    # Owner-keyed: the chatting member_X is parseable from the session
    # key, but the deduct key MUST be the connection's owner (org_Y).
    assert args[0] == "org_Y"


@pytest.mark.asyncio
async def test_skip_deduct_for_card1():
    conn = _make_connection()
    deduct_mock = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "chatgpt_oauth"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_x",
            model="openai-codex/gpt-5.5",
            input_tokens=999,
            output_tokens=999,
        )
    deduct_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skip_deduct_for_card2():
    conn = _make_connection()
    deduct_mock = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "byo_key"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_byo",
            model="openai/gpt-5.4",
            input_tokens=100,
            output_tokens=100,
        )
    deduct_mock.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_model_skips_deduct():
    """A new Claude model not yet in bedrock_pricing must not crash —
    log + skip; operator updates the rate table and the next chat is fine."""
    conn = _make_connection()
    deduct_mock = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_unknown",
            model="amazon-bedrock/anthropic.claude-fake-99",
            input_tokens=100,
            output_tokens=100,
        )
    deduct_mock.assert_not_called()


@pytest.mark.asyncio
async def test_model_id_without_provider_prefix_works():
    """sessions.list may return a bare model id without the
    "amazon-bedrock/" prefix — bedrock_pricing accepts the bare form."""
    conn = _make_connection()
    deduct_mock = AsyncMock(return_value=0)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"provider_choice": "bedrock_claude"}),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_bare",
            model="anthropic.claude-sonnet-4-6",  # no prefix
            input_tokens=1000,
            output_tokens=500,
        )
    deduct_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_billing_row_skips_deduct():
    """No billing row for owner → skip credit deduction (no provider
    choice to gate on)."""
    conn = _make_connection()
    deduct_mock = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.credit_ledger.deduct", new=deduct_mock),
    ):
        await conn._maybe_deduct_credits(
            chat_session_id="sess_x",
            model="amazon-bedrock/anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )
    deduct_mock.assert_not_called()
