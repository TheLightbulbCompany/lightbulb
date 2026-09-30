"""Voice billing: who pays, how much a call may last, what a second costs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing import voice_billing as vb

ACTIVE_CREDITS = {"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"}


@pytest.mark.parametrize(
    "account, expected",
    [
        (None, None),
        ({"subscription_status": "canceled", "plan": "standard"}, None),
        ({"subscription_status": "past_due", "plan": "base"}, None),
        (ACTIVE_CREDITS, "credits"),
        ({"subscription_status": "active", "plan": "base"}, "free"),
        ({"subscription_status": "active", "plan": "standard"}, "free"),  # no provider chosen = capped
        ({"subscription_status": "active", "plan": "standard", "provider_choice": "byo_key"}, "free"),
        ({"subscription_status": "active", "plan": "ultra_pro", "provider_choice": "chatgpt_oauth"}, "free"),
        ({"subscription_status": "trial_local", "trial_end": 4102444800}, "free"),
    ],
)
def test_billing_mode(account, expected):
    assert vb.billing_mode(account) == expected


def test_billing_mode_expired_trial_is_refused():
    with patch.object(vb.trial_cap, "is_trial_expired", return_value=True):
        assert vb.billing_mode({"subscription_status": "trial_local", "trial_end": 1}) is None


def test_prices_follow_provider_cost_times_markup():
    assert vb.provider_nanodollars(60) == 50_000_000  # $0.05
    with patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"):
        assert vb.charge_microcents(60) == 60_000  # 6 cents
        # rounded provider cost (833_334 nd) x 1.2 / 1000 = 1000.0008 -> 1001, same as settlement
        assert vb.charge_microcents(1) == 1_001


def test_charge_refuses_unset_markup():
    with patch.object(vb.settings, "OPENROUTER_MARKUP", ""), pytest.raises(ValueError):
        vb.charge_microcents(60)


@pytest.mark.asyncio
async def test_free_budget_reads_todays_counter():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"day": "2026-09-30", "usedSeconds": 450}}
    with (
        patch.object(vb, "_containers", return_value=table),
        patch.object(vb.trial_cap, "utc_today", return_value="2026-09-30"),
    ):
        assert await vb.budget_seconds("org_a", "free") == 150


@pytest.mark.asyncio
async def test_free_budget_resets_on_a_new_day():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"day": "2026-09-29", "usedSeconds": 600}}
    with (
        patch.object(vb, "_containers", return_value=table),
        patch.object(vb.trial_cap, "utc_today", return_value="2026-09-30"),
    ):
        assert await vb.budget_seconds("org_a", "free") == 600


@pytest.mark.asyncio
async def test_credits_budget_uses_the_gate_headroom_rule():
    row = {
        "remaining_microcents": 300_000 + 60_000 * 5,
        "reserved_microcents": 0,
        "openrouter_reserved_microcents": 60_000,
    }
    gate = MagicMock(get_row=AsyncMock(return_value=row))
    with (
        patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=False)),
        patch.object(vb, "MetronomeFastGate", return_value=gate),
        patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"),
    ):
        assert await vb.budget_seconds("org_a", "credits") == 240  # 4 minutes of headroom at 6¢/min


@pytest.mark.asyncio
async def test_credits_budget_zero_without_row():
    gate = MagicMock(get_row=AsyncMock(return_value=None))
    with (
        patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=False)),
        patch.object(vb, "MetronomeFastGate", return_value=gate),
        patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"),
    ):
        assert await vb.budget_seconds("org_a", "credits") == 0


@pytest.mark.asyncio
async def test_credits_budget_zero_when_blocked():
    with patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=True)):
        assert await vb.budget_seconds("org_a", "credits") == 0


@pytest.mark.asyncio
async def test_budget_is_capped_at_thirty_minutes():
    table = MagicMock()
    table.get_item.return_value = {}
    with patch.object(vb, "_containers", return_value=table), patch.object(vb, "FREE_SECONDS_PER_DAY", 99_999):
        assert await vb.budget_seconds("org_a", "free") == vb.MAX_CALL_SECONDS
