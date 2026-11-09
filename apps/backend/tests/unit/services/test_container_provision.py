"""Unit tests for core.services.container_provision — provider-choice
resolution and the provision metric.

Provisioning dispatch itself lives in core.services.provision_dispatch
(the SFN executor) and scripts.provision_runner — see
tests/unit/services/test_provision_dispatch.py and
tests/unit/scripts/test_provision_runner.py for that behavior."""

from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest

from core.services.container_provision import provider_choice_from_row, resolve_provider_choice


# ---------------------------------------------------------------------------
# resolve_provider_choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_short_circuits_to_managed_free_route():
    """A trial_local row (the no-card local trial) short-circuits BEFORE the
    provider_choice/byo_provider lookup — `trial_local` is a
    subscription_status, never a stored provider_choice, so the provider must
    not be read off the row (a trial row carries none).

    It resolves to the managed free route. Falling through to a paid provider
    would hand a card-less owner an unentitled inference path.
    """
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"subscription_status": "trial_local", "provider_choice": "byo_key"},
        )
        result = await resolve_provider_choice("user_x")
    # Short-circuits: the row's stray provider_choice/byo_provider is ignored.
    assert result == ("openrouter_managed", None)


@pytest.mark.asyncio
async def test_resolve_provider_choice_trial_local_writes_nothing():
    """`bedrock_trial` is a config-path mode, never a STORED provider_choice —
    resolving one must not write it back onto the billing row."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await resolve_provider_choice("user_x")
    repo.update_fields.assert_not_called()
    repo.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_provider_choice_reads_strongly_consistent():
    """A fresh trial row must be visible to the provision that follows its
    creation — an eventually consistent miss returns None and silently falls
    back to bedrock_claude (no owner key minted, Bedrock profiles written)."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
        await resolve_provider_choice("user_x")
    repo.get_by_owner_id.assert_awaited_once_with("user_x", consistent_read=True)


def _literal_values(annotation) -> set[str]:
    """Flatten the string values out of a (possibly optional) Literal."""
    values: set[str] = set()
    for arg in get_args(annotation):
        if isinstance(arg, str):
            values.add(arg)
        else:
            values |= _literal_values(arg)
    return values


def test_change_provider_refuses_retired_bedrock_choices():
    """The change-provider API is the live door: it must refuse both retired
    Bedrock choices, and so must the repository write every other path uses."""
    from core.repositories.billing_repo import _VALID_PROVIDER_CHOICES
    from routers.billing import ChangeProviderRequest

    allowed = _literal_values(ChangeProviderRequest.model_fields["provider_choice"].annotation)
    assert allowed == {"chatgpt_oauth", "openrouter_managed"}
    assert _VALID_PROVIDER_CHOICES == allowed | {"byo_key"}  # existing saved keys remain valid
    with pytest.raises(ValueError):
        ChangeProviderRequest(provider_choice="byo_key")
    for retired in ("bedrock_claude", "bedrock_trial"):
        with pytest.raises(ValueError):
            ChangeProviderRequest(provider_choice=retired)


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
async def test_resolve_provider_choice_no_row_defaults_to_managed():
    """No billing row falls back to the managed gate; the retired
    bedrock_claude default is never produced."""
    with patch("core.services.container_provision.billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(return_value=None)
        result = await resolve_provider_choice("user_x")
    assert result == ("openrouter_managed", None)


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


@pytest.mark.parametrize("plan", ["base", "standard", "ultra_pro"])
def test_plan_without_stored_choice_resolves_to_managed(plan):
    row = {"plan": plan, "subscription_status": "active"}
    assert provider_choice_from_row(row) == ("openrouter_managed", None)


def test_paid_plan_with_stored_choice_is_never_remapped():
    row = {"plan": "standard", "subscription_status": "active", "provider_choice": "chatgpt_oauth"}
    assert provider_choice_from_row(row) == ("chatgpt_oauth", None)


def test_no_plan_and_no_choice_resolves_to_managed():
    assert provider_choice_from_row({"subscription_status": "active"}) == ("openrouter_managed", None)
