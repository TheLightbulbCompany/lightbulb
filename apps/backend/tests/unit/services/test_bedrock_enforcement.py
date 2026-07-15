"""Revoke-only enforcement: enforce_on_low_balance fires revoke_owner_bedrock
(never any container stop) only when the Metronome webhook balance is
non-positive AND BEDROCK_ENFORCEMENT_LIVE is armed; an already-revoked owner
is skipped, and IAM failures never propagate."""

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from core.services.bedrock_enforcement import enforce_on_low_balance


@contextlib.contextmanager
def _enforcement(*, container_row: dict | None = None, provider_choice: str | None = "bedrock_claude"):
    """Patch the lazy-imported collaborators; yields (revoke, ecs_factory).

    Defaults the billing row to a ``bedrock_claude`` owner because that is the
    only provider credit-balance enforcement acts on; the trial-exclusion tests
    override ``provider_choice``.
    """
    revoke = AsyncMock()
    account = None if provider_choice is None else {"provider_choice": provider_choice}
    with (
        patch("core.billing.bedrock_aip.revoke_owner_bedrock", new=revoke),
        patch(
            "core.services.bedrock_enforcement.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
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


# ---------------------------------------------------------------------------
# THE FREE-TIER FENCE. Credit-balance enforcement applies ONLY to credit-drawing
# (bedrock_claude) owners. A trial_local owner is on the flat $3/day meter cap
# and has a Metronome balance of 0 as a NORMAL state — revoking them on it was
# killing every free-tier trial ~30s after signup (confirmed on prod, prawahp).


@pytest.mark.asyncio
async def test_trial_local_owner_is_never_revoked_on_zero_balance(monkeypatch):
    """The regression. balance 0 + enforcement live, but a trial owner draws no
    credits, so the revoke must NOT fire."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(
        container_row={"owner_id": "user_T", "status": "running"},
        provider_choice="trial_local_has_no_provider",  # provider_choice != bedrock_claude
    ) as (revoke, _ecs):
        await enforce_on_low_balance("user_T", 0)
    revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_with_no_provider_choice_is_never_revoked(monkeypatch):
    """A trial_local row carries provider_choice=None. Balance 0 must not revoke."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(container_row={"owner_id": "user_T"}, provider_choice=None) as (revoke, _ecs):
        await enforce_on_low_balance("user_T", -5000)
    revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_bedrock_claude_owner_is_still_revoked_on_zero_balance(monkeypatch):
    """The fence must not disarm the enforcement it exists to scope: a real
    credit-drawing owner out of credits IS still revoked."""
    monkeypatch.setattr("core.config.settings.BEDROCK_ENFORCEMENT_LIVE", True)
    with _enforcement(
        container_row={"owner_id": "org_P", "status": "running"},
        provider_choice="bedrock_claude",
    ) as (revoke, _ecs):
        await enforce_on_low_balance("org_P", 0)
    revoke.assert_awaited_once_with("org_P")
