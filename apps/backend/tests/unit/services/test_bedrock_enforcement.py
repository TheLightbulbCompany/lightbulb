"""Kill-switch triggers: enforce_after_deplete fires only when a NEWLY-applied
deplete crosses balance <= 0 (replays and positive balances do nothing);
enforce_on_low_balance fires only when the webhook balance is non-positive
AND BEDROCK_ENFORCEMENT_LIVE is armed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing.ledger_interface import DepleteResult
from core.services.bedrock_enforcement import enforce_after_deplete, enforce_on_low_balance


def _killer(*, already_stopped: bool = False) -> MagicMock:
    """An EcsManager stand-in: not-yet-stopped by default so the kill fires."""
    killer = MagicMock()
    killer.kill_owner_bedrock = AsyncMock()
    killer.is_owner_service_stopped = AsyncMock(return_value=already_stopped)
    return killer


@pytest.mark.asyncio
async def test_fires_kill_when_applied_and_balance_non_positive():
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=-100, applied=True))
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_fires_kill_at_exactly_zero():
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=0, applied=True))
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_does_not_fire_on_dedup_replay():
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=-100, applied=False))
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_fire_when_balance_positive():
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=42_000, applied=True))
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_refire_when_service_already_stopped():
    """During the ~5-15s IAM-propagation window after the first kill, a NEW
    requestId's applied deplete also sees balance<=0 — but the service is
    already stopped, so the redundant kill (and its IAM revoke + metric) is
    skipped."""
    killer = _killer(already_stopped=True)
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=-9_999, applied=True))
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_kill_failure_does_not_propagate():
    """A flaky ECS/IAM call must not crash the meter ingest path — the
    balance is already depleted; the kill is best-effort and the next
    deplete (or the provision gate) re-asserts the stop."""
    killer = _killer()
    killer.kill_owner_bedrock = AsyncMock(side_effect=RuntimeError("ecs down"))
    with patch("core.containers.get_ecs_manager", return_value=killer):
        # Must not raise.
        await enforce_after_deplete("org_Y", DepleteResult(balance_microcents=-1, applied=True))
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_low_balance_fires_kill_when_enforcement_live(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", -100)
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_low_balance_fires_kill_at_exactly_zero(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", 0)
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_low_balance_no_kill_when_balance_positive(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", 42_000)
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_no_kill_when_enforcement_off(monkeypatch):
    """Shadow mode (the default): the webhook still feeds the fast-gate flag,
    but a non-positive balance never fires the kill — the Metronome pipeline
    can be validated end-to-end with zero blast radius."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", False)
    killer = _killer()
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", -100)
    killer.kill_owner_bedrock.assert_not_called()
    killer.is_owner_service_stopped.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_skips_refire_when_already_stopped(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    killer = _killer(already_stopped=True)
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", -9_999)
    killer.kill_owner_bedrock.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_kill_failure_does_not_propagate(monkeypatch):
    """A flaky ECS/IAM call must not 500 the Metronome webhook (which would
    trigger redelivery storms) — the kill is best-effort and the fast-gate
    flag already blocks new sends."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    killer = _killer()
    killer.kill_owner_bedrock = AsyncMock(side_effect=RuntimeError("ecs down"))
    with patch("core.containers.get_ecs_manager", return_value=killer):
        await enforce_on_low_balance("org_Y", -1)
    killer.kill_owner_bedrock.assert_awaited_once_with("org_Y")
