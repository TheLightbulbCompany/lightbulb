"""The provision-time balance gate must read through the ledger interface
(get_ledger), not any concrete backend directly — so the chat gate and the
provision gate read one money source. A top-up that unblocks chat must
also let the container re-provision (and a runaway that drained the
balance must keep it down)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.provision_gate import evaluate_provision_gate


def _billing_account(**overrides) -> dict:
    base = {
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_zero_balance_returns_credits_required_gate_via_ledger():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=0)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        gate = await evaluate_provision_gate(owner_id="org_Y", clerk_user_id="user_X", is_admin=True)
    assert gate is not None
    assert gate.code == "credits_required"
    balance_args, _ = ledger.get_balance.call_args
    assert balance_args[0] == "org_Y"


@pytest.mark.asyncio
async def test_positive_balance_passes_balance_layer_via_ledger():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock(return_value=5_000_000)
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account()),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        gate = await evaluate_provision_gate(owner_id="org_Y", clerk_user_id="user_X", is_admin=True)
    # bedrock_claude with positive balance clears the balance layer; any
    # gate returned here is NOT credits_required.
    assert gate is None or gate.code != "credits_required"


@pytest.mark.asyncio
async def test_non_bedrock_provider_never_reads_balance():
    ledger = MagicMock()
    ledger.get_balance = AsyncMock()
    with (
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_account(provider_choice="byo_key")),
        ),
        patch("core.billing.ledger_interface.get_ledger", return_value=ledger),
    ):
        await evaluate_provision_gate(owner_id="org_Y", clerk_user_id="user_X", is_admin=True)
    ledger.get_balance.assert_not_called()
