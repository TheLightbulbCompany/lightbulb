"""Unit tests for core.services.container_provision — provider-choice
resolution and the provision metric.

Provisioning dispatch itself lives in core.services.provision_dispatch
(the SFN executor) and scripts.provision_runner — see
tests/unit/services/test_provision_dispatch.py and
tests/unit/scripts/test_provision_runner.py for that behavior."""

from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest

from core.services.container_provision import BEDROCK_TRIAL_CHOICE, resolve_provider_choice


# ---------------------------------------------------------------------------
# resolve_provider_choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_short_circuits_to_bedrock_trial():
    """A trial_local row (the no-card local trial) short-circuits BEFORE the
    provider_choice/byo_provider lookup — `trial_local` is a
    subscription_status, never a stored provider_choice, so the provider must
    not be read off the row (a trial row carries none).

    It resolves to the one-tier `bedrock_trial` CONFIG MODE. Falling through to
    `bedrock_claude` would hand a card-less owner the full paid three-tier
    ladder — Opus included."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local", "provider_choice": "byo_key"},
        )
        result = await resolve_provider_choice("user_x")
    # Short-circuits: the row's stray provider_choice/byo_provider is ignored.
    assert result == (BEDROCK_TRIAL_CHOICE, None)
    assert result[0] == "bedrock_trial"


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_writes_nothing():
    """`bedrock_trial` is a config-path mode, never a STORED provider_choice —
    resolving one must not write it back onto the billing row."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await resolve_provider_choice("user_x")
    repo.update_fields.assert_not_called()
    repo.upsert.assert_not_called()


def _literal_values(annotation) -> set[str]:
    """Flatten the string values out of a (possibly optional) Literal."""
    values: set[str] = set()
    for arg in get_args(annotation):
        if isinstance(arg, str):
            values.add(arg)
        else:
            values |= _literal_values(arg)
    return values


def test_bedrock_trial_is_not_an_acceptable_stored_provider_choice():
    """The provider write paths validate against a Literal that must NOT
    admit `bedrock_trial` — a request body could otherwise persist the trial
    config mode as a paid owner's provider, and the trial lockdown (one AIP, no
    cron/ACP/heartbeat) would silently apply to a paying customer."""
    from routers.billing import ChangeProviderRequest
    from schemas.user_schemas import SyncUserRequest

    for model in (ChangeProviderRequest, SyncUserRequest):
        allowed = _literal_values(model.model_fields["provider_choice"].annotation)
        assert allowed, f"{model.__name__}.provider_choice is not a Literal any more"
        assert BEDROCK_TRIAL_CHOICE not in allowed, f"{model.__name__} admits {BEDROCK_TRIAL_CHOICE}"


@pytest.mark.asyncio
async def test_resolve_provider_choice_bedrock_unchanged():
    """Existing bedrock_claude path is untouched by the trial_local branch."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "provider_choice": "bedrock_claude",
            },
        )
        result = await resolve_provider_choice("user_x")
    assert result == ("bedrock_claude", None)


@pytest.mark.asyncio
async def test_resolve_provider_choice_byo_key_unchanged():
    """Existing byo_key path (with its byo_provider) is untouched."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "subscription_status": "active",
                "provider_choice": "byo_key",
                "byo_provider": "anthropic",
            },
        )
        result = await resolve_provider_choice("user_x")
    assert result == ("byo_key", "anthropic")


@pytest.mark.asyncio
async def test_resolve_provider_choice_no_row_defaults_to_bedrock():
    """No billing row at all still falls back to bedrock_claude — the
    legacy default for owners onboarded before per-owner provider_choice
    storage. Must not be confused with trial_local."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        result = await resolve_provider_choice("user_x")
    assert result == ("bedrock_claude", None)


# ---------------------------------------------------------------------------
# container_provision is provider-choice resolution ONLY. Provisioning itself
# is dispatched by core.services.provision_dispatch.dispatch_provision — the
# single funnel every call site (status-poll auto-retry, ensure_user_ready,
# reprovision_for_user, container_wake, change_provider's cold path) rides.


async def test_container_provision_never_touches_ecs_directly():
    """Regression guard: this module resolves a provider choice and emits a metric. It must
    reach no runtime at all -- provisioning lives entirely in scripts.provision_runner, a
    separate process the backend never invokes in-process."""
    from core.services import container_provision

    assert not hasattr(container_provision, "container_repo")
    for gone in ("get_ecs_manager", "container_rpc", "runtime_transport", "hermes_provisioning"):
        assert not hasattr(container_provision, gone), gone
