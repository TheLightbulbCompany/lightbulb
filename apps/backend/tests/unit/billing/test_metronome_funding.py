"""request_top_up creates a PAYMENT-GATED Metronome prepaid commit via the
v2 contract edit (the only API surface where payment gating exists) — no
local balance write, no restore call (the gate's lazy refresh re-arms), no
Stripe Checkout. Metronome + Stripe collect the money and release credits.
Double-submit safety is vendor-side: every commit carries an Idempotency-Key,
so a replayed request returns the original commit instead of charging twice."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing.metronome_funding import FundingError, request_top_up


@pytest.fixture(autouse=True)
def metronome_env(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    monkeypatch.setattr("core.config.settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID", "ct-usd-cents")


_real_strftime = time.strftime
_real_gmtime = time.gmtime


class _FrozenTime:
    """Pins the clock so the fallback idempotency key's minute bucket is
    assertable instead of racing the wall clock."""

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
    fake.create_gated_prepaid_commit = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_request_creates_payment_gated_commit_in_cents(provisioned_billing_row):
    fake = _fake_client()
    await request_top_up("org_abc", 2573, client=fake)

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
    """The endpoint's owner-namespaced key must reach the client verbatim —
    it is what makes a retried POST replay the original commit at Metronome
    instead of charging the card a second time."""
    fake = _fake_client()
    await request_top_up("org_abc", 2500, idempotency_key="topup-org_abc-uuid-1", client=fake)

    _, kwargs = fake.create_gated_prepaid_commit.call_args
    assert kwargs["idempotency_key"] == "topup-org_abc-uuid-1"


@pytest.mark.asyncio
async def test_request_without_key_falls_back_to_minute_bucket(provisioned_billing_row, frozen_clock):
    """Callers without a client token get the deterministic owner+amount+
    minute fallback. It retains the window-boundary hole, which is why real
    clients pass a per-intent UUID."""
    fake = _fake_client()
    await request_top_up("org_abc", 2500, client=fake)

    _, kwargs = fake.create_gated_prepaid_commit.call_args
    assert kwargs["idempotency_key"] == f"topup-org_abc-2500-{int(1_780_000_000.0 // 60)}"


@pytest.mark.asyncio
async def test_request_description_is_deterministic(provisioned_billing_row):
    """No timestamp in the description: Metronome's idempotent replay
    requires the retried body to be byte-identical (same key + different
    body → 409)."""
    fake = _fake_client()
    await request_top_up("org_abc", 2573, client=fake)
    first = fake.create_gated_prepaid_commit.call_args.kwargs["description"]
    await request_top_up("org_abc", 2573, client=fake)
    second = fake.create_gated_prepaid_commit.call_args.kwargs["description"]
    assert first == second == "isol8 top-up owner=org_abc amount_cents=2573"


@pytest.mark.asyncio
async def test_request_writes_no_local_balance(provisioned_billing_row):
    """No DDB credit write of any kind: credits are released by Metronome on
    payment, and the gate's lazy refresh is the only re-arm path."""
    fake = _fake_client()
    with patch("core.billing.metronome_fast_gate.MetronomeFastGate") as gate_cls:
        await request_top_up("org_abc", 2500, client=fake)
    gate_cls.assert_not_called()


@pytest.mark.asyncio
async def test_request_raises_when_customer_not_provisioned(provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="customer not provisioned"):
        await request_top_up("org_unprovisioned", 2500, client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_raises_when_contract_not_provisioned():
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(metronome_contract_id=None)),
    ):
        with pytest.raises(FundingError, match="contract not provisioned"):
            await request_top_up("org_abc", 2500, client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_rejects_non_positive_amount(provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await request_top_up("org_abc", 0, client=fake)
    fake.create_gated_prepaid_commit.assert_not_awaited()
