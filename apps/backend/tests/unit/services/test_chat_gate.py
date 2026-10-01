"""Tests for the free-tier daily cap wired into the live chat path.

``chat_gate.gate_chat`` Layer 2 is a **read-only** check (via
``trial_cap.reason_for_account``) of the trial window and the openrouter-gate's
``openrouter_capped_day`` stamp. It places NO reserve and writes nothing; the
OpenRouter per-key daily limit is the real valve. Blocking here is UX.

The predicate is ``subscription_status == "trial_local"``. The container row's
``provider_choice`` is NOT consulted: the moment billing flips to paid, the
owner is off the trial cap by definition.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services import trial_cap
from core.services.chat_gate import gate_chat


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
    capture_mock.assert_called_once_with(owner_id="u", event="trial_cap_reached", distinct_id="u")


@pytest.mark.asyncio
async def test_gate_allows_trial_owner_under_cap_and_writes_nothing():
    """An uncapped trial owner passes, and gate_chat writes nothing."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
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
    """A paid (openrouter_managed) owner never hits the cap check at all — the
    free tier's cap is a trial_local-only mechanism. Layer 3 (Metronome
    balance) still runs."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "openrouter_managed",
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
    passed is blocked with code=trial_expired."""
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
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is True
    assert gate["code"] == "trial_expired"
    assert isinstance(gate["message"], str) and gate["message"]


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


@pytest.mark.asyncio
async def test_trial_owner_under_cap_never_reaches_the_credits_layer():
    """A trial owner under the cap is ADMITTED, and Layer 3 is never consulted.

    This is the free-tier lockout guard. `/billing/trial-checkout` writes
    provider_choice="openrouter_managed" SYNCHRONOUSLY before redirecting to
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
                    "provider_choice": "openrouter_managed",
                }
            ),
        ),
        patch("core.services.trial_cap.reason_for_account", new=AsyncMock(return_value=None)),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked_mock),
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")

    assert gate["blocked"] is False
    blocked_mock.assert_not_awaited()


# --------- Moved from the deleted gateway-pool credit tests (Task 9) ---------
# Card-based provider gating (card1 oauth / card2 byo / card3 bedrock credits)
# and the subscription-status gate (Audit M1), both layers of the same
# gate_chat this file already covers for Layer 2 (the trial $3/day cap).


@pytest.mark.asyncio
async def test_card3_blocked_by_metronome_gate():
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "openrouter_managed",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
    ):
        result = await gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "Top up" in result["message"]


@pytest.mark.asyncio
async def test_openrouter_managed_owner_uses_the_same_credit_gate():
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "openrouter_managed",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
    ):
        result = await gate_chat(user_id="u_1", owner_id="org_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "workspace credits" in result["message"]


@pytest.mark.asyncio
async def test_card3_not_blocked_when_gate_clear():
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "openrouter_managed",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=False)),
    ):
        result = await gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_card3_gate_keyed_on_owner_for_org_credits_pool():
    """Both provider_choice (from billing_repo) AND the credits gate are
    keyed on owner_id. Org members all draw from one pool funded by the
    admin — without this, members get blocked with out_of_credits.
    """
    billing_mock = AsyncMock(
        return_value={
            "subscription_status": "active",
            "provider_choice": "openrouter_managed",
        }
    )
    blocked = AsyncMock(return_value=False)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await gate_chat(user_id="member_X", owner_id="org_Y", session_key="sk_test")

    assert result == {"blocked": False}
    billing_mock.assert_awaited_once_with("org_Y")
    blocked.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_card1_oauth_user_never_gated_by_credits():
    blocked = AsyncMock()
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
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_card2_byo_user_never_gated_by_credits():
    blocked = AsyncMock()
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
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_billing_row_without_provider_choice_never_gated():
    """Legacy billing rows without provider_choice shouldn't be locked
    out — they keep working until provider is set."""
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
        result = await gate_chat(user_id="u_legacy", owner_id="o_legacy", session_key="sk_test")
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
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"subscription_status": status}),
    ):
        result = await gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing"])
async def test_active_or_trialing_subscription_does_not_trigger_sub_gate(status):
    """active/trialing pass the subscription gate (then fall through to
    the provider_choice + balance check). Without provider_choice set,
    Layer-2 returns blocked=False."""
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "subscription_status": status,
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_missing_billing_row_blocks_chat_when_owner_id_provided():
    """An authenticated user whose billing row doesn't exist at all
    (never went through trial-checkout, or got cleaned up out-of-band)
    must be blocked. Otherwise non-Bedrock providers would chat freely
    with us still on the hook for container compute."""
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        result = await gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_legacy_billing_row_without_status_does_not_block():
    """A pre-Plan-3 billing row may have stripe_subscription_id but no
    subscription_status backfilled — those should NOT be locked out
    mid-deploy."""
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "stripe_subscription_id": "sub_legacy",
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result == {"blocked": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        {"subscription_status": "active", "plan": "base"},
        {"subscription_status": "active", "plan": "standard"},
    ],
)
async def test_gate_blocks_capped_paid_owner_at_the_daily_cap(account):
    """Base and paid-with-no-provider owners run on a per-key daily cap too, so a stamped cap
    blocks the send with THEIR card (included usage), never the free-trial one."""
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=AsyncMock(return_value=account)),
        patch("core.services.trial_cap.reason_for_account", new=AsyncMock(return_value=trial_cap.INCLUDED_CAP)),
        patch("core.observability.product_analytics.capture_once_per_owner_per_day"),
    ):
        gate = await gate_chat(user_id="u", owner_id="u", session_key="sk1")
    assert gate["blocked"] is True
    assert gate["code"] == "included_daily_cap"
    assert "free" not in gate["message"].lower()
