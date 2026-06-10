"""request_top_up creates a PAYMENT-GATED Metronome prepaid commit — no
local balance write, no restore call (the gate's lazy refresh re-arms), no
Stripe Checkout. Metronome + Stripe collect the money and release credits."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing.metronome_funding import FundingError, request_top_up


@pytest.fixture(autouse=True)
def lifecycle_enabled(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    monkeypatch.setattr("core.config.settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID", "ct-usd-cents")


def _billing_row(owner_id="org_abc", **overrides):
    row = {
        "owner_id": owner_id,
        "metronome_customer_id": "cust-uuid-1",
        "metronome_contract_id": "contract-1",
        "provider_choice": "bedrock_claude",
    }
    row.update(overrides)
    return row


@pytest.fixture
def provisioned_billing_row():
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(side_effect=lambda owner_id: _billing_row() if owner_id == "org_abc" else None),
    ):
        yield


def _fake_client():
    fake = MagicMock()
    fake.create_prepaid_commit = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_request_creates_payment_gated_commit_in_cents(provisioned_billing_row):
    fake = _fake_client()
    await request_top_up("org_abc", 2573, client=fake)

    fake.create_prepaid_commit.assert_awaited_once()
    _, kwargs = fake.create_prepaid_commit.call_args
    assert kwargs["customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["credit_type_id"] == "ct-usd-cents"
    assert kwargs["amount_cents"] == 2573
    assert "org_abc" in kwargs["description"]
    # The whole point: Metronome collects via Stripe before releasing the
    # credits — the backend never touches the money.
    assert kwargs["payment_gate_config"] == {"payment_gate_type": "STRIPE"}


@pytest.mark.asyncio
async def test_request_writes_no_local_balance(provisioned_billing_row):
    """No DDB credit write of any kind: credits are released by Metronome on
    payment, and the gate's lazy refresh is the only re-arm path."""
    fake = _fake_client()
    with patch("core.billing.metronome_fast_gate.MetronomeFastGate") as gate_cls:
        await request_top_up("org_abc", 2500, client=fake)
    gate_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_raises_when_lifecycle_disabled(provisioned_billing_row, monkeypatch):
    """A purchase request must fail loudly (the endpoint surfaces a 400),
    never silently drop, when Metronome isn't wired for the env yet."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    fake = _fake_client()
    with pytest.raises(FundingError, match="lifecycle is disabled"):
        await request_top_up("org_abc", 2500, client=fake)
    fake.create_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_raises_when_customer_not_provisioned(provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="customer not provisioned"):
        await request_top_up("org_unprovisioned", 2500, client=fake)
    fake.create_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_raises_when_contract_not_provisioned():
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(metronome_contract_id=None)),
    ):
        with pytest.raises(FundingError, match="contract not provisioned"):
            await request_top_up("org_abc", 2500, client=fake)
    fake.create_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_rejects_non_positive_amount(provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await request_top_up("org_abc", 0, client=fake)
    fake.create_prepaid_commit.assert_not_awaited()
