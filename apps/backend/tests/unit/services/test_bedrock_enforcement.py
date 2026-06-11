"""Revoke-only enforcement: enforce_on_low_balance fires revoke_owner_bedrock
(never any container stop) only when the Metronome webhook balance is
non-positive AND BEDROCK_ENFORCEMENT_LIVE is armed; an already-revoked owner
is skipped, and IAM failures never propagate."""

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from core.services.bedrock_enforcement import enforce_on_low_balance


@contextlib.contextmanager
def _enforcement(*, container_row: dict | None = None):
    """Patch the lazy-imported collaborators; yields (revoke, ecs_factory)."""
    revoke = AsyncMock()
    with (
        patch("core.billing.bedrock_aip.revoke_owner_bedrock", new=revoke),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=container_row),
        ),
        patch("core.containers.get_ecs_manager") as ecs_factory,
    ):
        yield revoke, ecs_factory


@pytest.mark.asyncio
async def test_low_balance_fires_revoke_when_enforcement_live(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row={"owner_id": "org_Y", "status": "running"}) as (revoke, ecs_factory):
        await enforce_on_low_balance("org_Y", -100)
    revoke.assert_awaited_once_with("org_Y")
    # Revoke-only: the container must NOT be stopped — the subscription pays
    # for the box; only Bedrock inference is cut.
    ecs_factory.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_fires_revoke_at_exactly_zero(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row={"owner_id": "org_Y"}) as (revoke, ecs_factory):
        await enforce_on_low_balance("org_Y", 0)
    revoke.assert_awaited_once_with("org_Y")
    ecs_factory.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_no_revoke_when_balance_positive(monkeypatch):
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement() as (revoke, _):
        await enforce_on_low_balance("org_Y", 42_000)
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_no_revoke_when_enforcement_off(monkeypatch):
    """Shadow mode: the webhook still feeds the fast-gate flag, but a
    non-positive balance never fires the revoke — the Metronome pipeline
    can be validated end-to-end with zero blast radius."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", False)
    with _enforcement() as (revoke, _):
        await enforce_on_low_balance("org_Y", -100)
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_low_balance_skips_refire_when_already_revoked(monkeypatch):
    """revoke_owner_bedrock stamps bedrock_revoked=True; subsequent triggers
    while the balance stays <= 0 must not re-fire the IAM delete."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row={"owner_id": "org_Y", "bedrock_revoked": True}) as (revoke, _):
        await enforce_on_low_balance("org_Y", -9_999)
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_missing_container_row_still_attempts_revoke(monkeypatch):
    """No containers row ≠ already revoked: revoke_owner_bedrock treats a
    missing IAM policy as already-revoked, so attempting is always safe."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row=None) as (revoke, _):
        await enforce_on_low_balance("org_Y", -1)
    revoke.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_revoke_failure_does_not_propagate(monkeypatch):
    """A flaky IAM call must not 500 the Metronome webhook (which would
    trigger redelivery storms) — the revoke is best-effort and the fast-gate
    flag already blocks new sends."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row={"owner_id": "org_Y"}) as (revoke, _):
        revoke.side_effect = RuntimeError("iam down")
        await enforce_on_low_balance("org_Y", -1)
    revoke.assert_awaited_once_with("org_Y")
