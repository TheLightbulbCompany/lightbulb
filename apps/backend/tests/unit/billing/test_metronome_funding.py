"""request_top_up creates a PAYMENT-GATED Metronome prepaid commit — no
local balance write, no restore call (the gate's lazy refresh re-arms), no
Stripe Checkout. Metronome + Stripe collect the money and release credits.
A durable dedup guard (webhook-dedup table) rejects an identical
owner+amount re-submit within the 60s window — a duplicate here is a
duplicate card charge."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_funding import FundingError, request_top_up


@pytest.fixture(autouse=True)
def lifecycle_enabled(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    monkeypatch.setattr("core.config.settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID", "ct-usd-cents")


@pytest.fixture(autouse=True)
def webhook_dedup_table(monkeypatch):
    """Every accepted top-up writes a dedup row, so the moto table is
    provisioned for the whole module."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-webhook-event-dedup",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")
        yield


_real_strftime = time.strftime
_real_gmtime = time.gmtime


class _FrozenTime:
    """Pins the dedup window so back-to-back duplicate-submit tests can't
    flake by straddling a real minute boundary."""

    @staticmethod
    def time() -> float:
        return 1_780_000_000.0

    strftime = staticmethod(_real_strftime)
    gmtime = staticmethod(_real_gmtime)


@pytest.fixture
def frozen_window(monkeypatch):
    monkeypatch.setattr("core.billing.metronome_funding.time", _FrozenTime)


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


@pytest.mark.asyncio
async def test_duplicate_request_within_window_is_rejected(provisioned_billing_row, frozen_window):
    """Second identical owner+amount submit inside the 60s window must not
    create a second payment-gated commit (= a second card charge)."""
    fake = _fake_client()
    await request_top_up("org_abc", 2500, client=fake)
    with pytest.raises(FundingError, match="duplicate top-up"):
        await request_top_up("org_abc", 2500, client=fake)
    fake.create_prepaid_commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_amount_within_window_passes(provisioned_billing_row, frozen_window):
    """The guard keys on owner+amount+window — a deliberate second top-up of
    a different amount is a distinct purchase, not a double-click."""
    fake = _fake_client()
    await request_top_up("org_abc", 2500, client=fake)
    await request_top_up("org_abc", 5000, client=fake)
    assert fake.create_prepaid_commit.await_count == 2


@pytest.mark.asyncio
async def test_commit_failure_releases_dedup_guard_for_retry(provisioned_billing_row, frozen_window):
    """A transient Metronome failure must not burn the dedup key — the user's
    retry within the same window has to go through."""
    fake = _fake_client()
    fake.create_prepaid_commit.side_effect = [RuntimeError("metronome blip"), None]
    with pytest.raises(RuntimeError, match="metronome blip"):
        await request_top_up("org_abc", 2500, client=fake)
    await request_top_up("org_abc", 2500, client=fake)
    assert fake.create_prepaid_commit.await_count == 2
