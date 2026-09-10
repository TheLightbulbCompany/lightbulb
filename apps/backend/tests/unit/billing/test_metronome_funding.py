"""request_top_up creates a PAYMENT-GATED Metronome prepaid commit via the
v2 contract edit (the only API surface where payment gating exists) — no
local balance write, no restore call (the gate's lazy refresh re-arms), no
Stripe Checkout. Metronome + Stripe collect the money and release credits.
Double-submit safety is vendor-side: every commit carries an Idempotency-Key,
so a replayed request returns the original commit instead of charging twice."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_funding import FundingError, request_top_up


@pytest.fixture(autouse=True)
def metronome_env(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    monkeypatch.setattr("core.config.settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID", "ct-usd-cents")


@pytest.fixture(autouse=True)
def top_up_intent_table(monkeypatch):
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
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
    """Pins the first request's effective UTC midnight."""

    @staticmethod
    def time() -> float:
        return 1_780_000_000.0

    strftime = staticmethod(_real_strftime)
    gmtime = staticmethod(_real_gmtime)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr("core.billing.metronome_funding.time", _FrozenTime)


def _billing_row(owner_id="org_abc", **overrides):
    row = {
        "owner_id": owner_id,
        "stripe_customer_id": "cus-1",
        "subscription_status": "active",
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
        new=AsyncMock(side_effect=lambda owner_id, **_kwargs: _billing_row() if owner_id == "org_abc" else None),
    ):
        yield


def _fake_client():
    fake = MagicMock()
    fake.create_gated_prepaid_commit = AsyncMock()
    fake.get_customer_billing_provider_configurations = AsyncMock(
        return_value=[
            {
                "billing_provider": "stripe",
                "archived_at": None,
                "configuration": {
                    "stripe_customer_id": "cus-1",
                    "stripe_collection_method": "charge_automatically",
                },
            }
        ]
    )
    fake.get_contract = AsyncMock(
        return_value={
            "customer_id": "cust-uuid-1",
            "customer_billing_provider_configuration": {
                "billing_provider": "stripe",
                "archived_at": None,
                "configuration": {
                    "stripe_customer_id": "cus-1",
                    "stripe_collection_method": "charge_automatically",
                },
            },
        }
    )
    return fake


@pytest.mark.asyncio
async def test_request_creates_payment_gated_commit_in_cents(provisioned_billing_row):
    fake = _fake_client()
    await request_top_up("org_abc", 2573, request_id="11111111-1111-4111-8111-111111111111", client=fake)

    # The whole point: the GATED v2 method — Metronome collects via Stripe
    # before releasing the credits, the backend never touches the money. The
    # single amount feeds both the access and invoice schedules 1:1 inside
    # the client (credits unlocked == cents invoiced).
    fake.create_gated_prepaid_commit.assert_awaited_once()
    _, kwargs = fake.create_gated_prepaid_commit.call_args
    assert kwargs["customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["credit_type_id"] == "ct-usd-cents"
    assert kwargs["amount_cents"] == 2573
    assert "org_abc" in kwargs["description"]


@pytest.mark.asyncio
async def test_request_passes_caller_idempotency_key_through(provisioned_billing_row):
    """The caller UUID is Metronome's native idempotency key verbatim."""
    fake = _fake_client()
    request_id = "22222222-2222-4222-8222-222222222222"
    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    _, kwargs = fake.create_gated_prepaid_commit.call_args
    assert kwargs["idempotency_key"] == request_id


@pytest.mark.asyncio
async def test_completed_retry_returns_before_any_metronome_read_or_write(provisioned_billing_row):
    """A completed permanent intent remains available during provider outages."""
    fake = _fake_client()
    request_id = "21212121-2121-4212-8212-212121212121"
    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    fake.get_customer_billing_provider_configurations.reset_mock()
    fake.get_contract.reset_mock()
    fake.create_gated_prepaid_commit.reset_mock()

    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    fake.get_customer_billing_provider_configurations.assert_not_awaited()
    fake.get_contract.assert_not_awaited()
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_retry_across_midnight_reuses_the_pinned_metronome_body(provisioned_billing_row, monkeypatch):
    """A transport-uncertain first call stays pending; a next-day retry uses
    the original effective date and idempotency key rather than a new charge."""
    fake = _fake_client()
    fake.create_gated_prepaid_commit = AsyncMock(side_effect=[RuntimeError("timeout"), None])
    request_id = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setattr("core.billing.metronome_funding.time", _FrozenTime)
    with pytest.raises(RuntimeError, match="timeout"):
        await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    class _NextDay:
        time = staticmethod(_FrozenTime.time)
        strftime = staticmethod(lambda *_args: "2026-09-11T00:00:00.000Z")
        gmtime = staticmethod(_real_gmtime)

    monkeypatch.setattr("core.billing.metronome_funding.time", _NextDay)
    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    first, second = (call.kwargs for call in fake.create_gated_prepaid_commit.await_args_list)
    assert second == first


@pytest.mark.asyncio
async def test_transport_loss_retries_same_provider_write_inside_safety_window(provisioned_billing_row, monkeypatch):
    fake = _fake_client()
    fake.create_gated_prepaid_commit = AsyncMock(side_effect=[RuntimeError("response lost"), None])
    request_id = "34333333-3333-4333-8333-333333333333"
    monkeypatch.setattr("core.services.webhook_dedup.time.time", lambda: 1_000)
    monkeypatch.setattr("core.billing.metronome_funding.time.time", lambda: 1_000)

    with pytest.raises(RuntimeError, match="response lost"):
        await request_top_up("org_abc", 2500, request_id=request_id, client=fake)
    monkeypatch.setattr("core.services.webhook_dedup.time.time", lambda: 1_000 + 22 * 60 * 60)
    monkeypatch.setattr("core.billing.metronome_funding.time.time", lambda: 1_000 + 22 * 60 * 60)
    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    assert fake.create_gated_prepaid_commit.await_count == 2
    first, second = (call.kwargs for call in fake.create_gated_prepaid_commit.await_args_list)
    assert second == first


@pytest.mark.asyncio
async def test_transport_loss_after_retry_window_requires_reconciliation_without_provider_call(
    provisioned_billing_row, monkeypatch
):
    from core.services.webhook_dedup import TopUpIntentConflict, get_active_top_up

    fake = _fake_client()
    fake.create_gated_prepaid_commit = AsyncMock(side_effect=RuntimeError("response lost"))
    request_id = "35333333-3333-4333-8333-333333333333"
    monkeypatch.setattr("core.services.webhook_dedup.time.time", lambda: 1_000)
    monkeypatch.setattr("core.billing.metronome_funding.time.time", lambda: 1_000)
    with pytest.raises(RuntimeError, match="response lost"):
        await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    fake.get_customer_billing_provider_configurations.reset_mock()
    fake.get_contract.reset_mock()
    monkeypatch.setattr("core.services.webhook_dedup.time.time", lambda: 1_000 + 23 * 60 * 60)
    monkeypatch.setattr("core.billing.metronome_funding.time.time", lambda: 1_000 + 23 * 60 * 60)
    with pytest.raises(TopUpIntentConflict, match="reconciliation"):
        await request_top_up("org_abc", 2500, request_id=request_id, client=fake)

    assert fake.create_gated_prepaid_commit.await_count == 1
    fake.get_customer_billing_provider_configurations.assert_not_awaited()
    fake.get_contract.assert_not_awaited()
    assert (await get_active_top_up("org_abc"))["status"] == "action_required"


@pytest.mark.asyncio
async def test_request_description_is_deterministic(provisioned_billing_row):
    """No timestamp in the description: Metronome's idempotent replay
    requires the retried body to be byte-identical (same key + different
    body → 409)."""
    fake = _fake_client()
    await request_top_up("org_abc", 2573, request_id="44444444-4444-4444-8444-444444444444", client=fake)
    first = fake.create_gated_prepaid_commit.call_args.kwargs["description"]
    from core.services.webhook_dedup import settle_active_top_up

    await settle_active_top_up("org_abc", "cust-uuid-1", "contract-1", "paid", invoice_id="invoice_1")
    await request_top_up("org_abc", 2573, request_id="55555555-5555-4555-8555-555555555555", client=fake)
    second = fake.create_gated_prepaid_commit.call_args.kwargs["description"]
    assert first == second == "isol8 top-up owner=org_abc amount_cents=2573"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "failed"])
async def test_collection_can_finish_before_request_acknowledgement(provisioned_billing_row, status):
    from core.services.webhook_dedup import get_active_top_up, settle_active_top_up

    fake = _fake_client()

    async def collect_before_response(**_kwargs):
        await settle_active_top_up("org_abc", "cust-uuid-1", "contract-1", status, invoice_id="invoice_1")

    fake.create_gated_prepaid_commit.side_effect = collect_before_response
    request_id = "12345678-1234-4234-8234-123456789abc"
    await request_top_up("org_abc", 2500, request_id=request_id, client=fake)
    assert (await get_active_top_up("org_abc"))["status"] == status
    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table("test-webhook-event-dedup")
        .get_item(Key={"event_id": f"metronome-top-up:org_abc:{request_id}"})["Item"]
    )
    assert item["state"] == status


@pytest.mark.asyncio
async def test_request_writes_no_local_balance(provisioned_billing_row):
    """No DDB credit write of any kind: credits are released by Metronome on
    payment, and the gate's lazy refresh is the only re-arm path."""
    fake = _fake_client()
    with patch("core.billing.metronome_fast_gate.MetronomeFastGate") as gate_cls:
        await request_top_up("org_abc", 2500, request_id="66666666-6666-4666-8666-666666666666", client=fake)
    gate_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_raises_when_customer_not_provisioned(provisioned_billing_row):
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(stripe_customer_id=None)),
    ):
        with pytest.raises(FundingError, match="Stripe customer not provisioned"):
            await request_top_up("org_abc", 2500, request_id="77777777-7777-4777-8777-777777777777", client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_raises_when_contract_not_provisioned():
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(metronome_contract_id=None)),
    ):
        with pytest.raises(FundingError, match="contract not provisioned"):
            await request_top_up("org_abc", 2500, request_id="88888888-8888-4888-8888-888888888888", client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_rejects_non_positive_amount(provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await request_top_up("org_abc", 0, request_id="99999999-9999-4999-8999-999999999999", client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_fails_closed_when_contract_is_linked_to_another_stripe_customer(provisioned_billing_row):
    """A workspace may only charge the Stripe customer bound to its billing
    row; a stale Metronome contract must never redirect collection elsewhere."""
    fake = _fake_client()
    fake.get_contract.return_value["customer_billing_provider_configuration"]["configuration"]["stripe_customer_id"] = (
        "cus-other"
    )

    with pytest.raises(FundingError, match="Stripe billing link"):
        await request_top_up(
            "org_abc",
            2500,
            request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            client=fake,
        )
    fake.create_gated_prepaid_commit.assert_not_awaited()
