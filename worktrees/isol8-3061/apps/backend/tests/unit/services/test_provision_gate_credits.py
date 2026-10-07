"""The provision-time balance gate must read the shared Metronome gate
predicate (core/services/metronome_gate.is_owner_blocked), the same source
the chat gate reads — so a top-up that unblocks chat also lets the
container re-provision, and a drained balance keeps it down."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.provision_gate import evaluate_provision_gate


def _billing_account(**overrides) -> dict:
    base = {
        "subscription_status": "active",
        "provider_choice": "openrouter_managed",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_blocked_owner_returns_credits_required_gate():
    blocked = AsyncMock(return_value=True)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        gate = await evaluate_provision_gate(owner_id="org_Y", is_admin=True)
    assert gate is not None
    assert gate.code == "credits_required"
    blocked.assert_awaited_once_with("org_Y")
    # The CTA must land somewhere credits are actually sold. CreditsSection is
    # embedded in LLMPanel (the "providers" panel) and there is no credits UI on
    # the billing panel at all — so the old /settings/billing#credits target was
    # a dead end, made worse by /settings/billing being a redirect that drops
    # the fragment. This is the one action on a "you have no credits" card.
    assert gate.action_href == "/settings?panel=providers"


@pytest.mark.asyncio
async def test_unblocked_owner_passes_balance_layer():
    blocked = AsyncMock(return_value=False)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        gate = await evaluate_provision_gate(owner_id="org_Y", is_admin=True)
    # openrouter_managed with a non-blocked gate clears the balance layer; any
    # gate returned here is NOT credits_required.
    assert gate is None or gate.code != "credits_required"


@pytest.mark.asyncio
async def test_non_bedrock_provider_never_reads_credits_gate():
    blocked = AsyncMock()
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account(provider_choice="byo_key")),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        await evaluate_provision_gate(owner_id="org_Y", is_admin=True)
    blocked.assert_not_called()
