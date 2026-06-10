"""Unit tests for Metronome top-up funding (mocked client + moto DDB)."""

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.billing.metronome_funding import FundingError, fund_top_up


@pytest.fixture(autouse=True)
def lifecycle_enabled(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)


@pytest.fixture
def fast_gate_table(monkeypatch):
    """The fast-gate projection table the top-up re-arm writes to. The
    Metronome customer UUID itself lives on the billing row (mocked via
    billing_repo below), not here."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
        yield client


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


@pytest.fixture(autouse=True)
def restore_bedrock_mock():
    """fund_top_up lazily imports restore_owner_bedrock (the post-top-up
    un-kill); stub it so these tests never touch IAM/container repos."""
    with patch("core.billing.bedrock_aip.restore_owner_bedrock", new=AsyncMock()) as mock:
        yield mock


def _fake_client():
    fake = MagicMock()
    fake.edit_contract_add_commits = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_fund_adds_ungated_prepaid_commit_in_cents(fast_gate_table, provisioned_billing_row):
    # Pre-arm the cache as blocked so we can prove the top-up re-arms it.
    fast_gate_table.put_item(
        TableName="test-metronome-fast-gate",
        Item={"owner_id": {"S": "org_abc"}, "remaining_microcents": {"N": "0"}},
    )
    fake = _fake_client()
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_730_000,  # $25.73 — a value that exposes a 100x slip
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()
    _, kwargs = fake.edit_contract_add_commits.call_args
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    commit = kwargs["add_commits"][0]
    assert commit["type"] == "PREPAID"
    assert commit["product_id"] == "prod-credits"
    # Our Stripe Checkout already collected the money — a Metronome payment
    # gate would invoice the customer a second time (or never release).
    assert "payment_gate_config" not in commit
    [item] = commit["access_schedule"]["schedule_items"]
    assert item["amount"] == 2573  # CENTS (Metronome's USD credit type), not dollars
    assert item["starting_at"].endswith("Z")
    assert item["ending_before"] == "9999-12-31T00:00:00.000Z"
    assert "cs_test_123" in commit["description"]
    # The cache is optimistically re-armed so the owner's next chat message
    # unblocks without waiting for a Metronome balance webhook (which won't
    # fire on a top-up). get_balance reads this cache, so without the re-arm a
    # paid top-up would never unblock chat.
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["remaining_microcents"]["N"] == "25730000"


@pytest.mark.asyncio
async def test_fund_noop_when_lifecycle_disabled(fast_gate_table, provisioned_billing_row, monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    fake = _fake_client()
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_fund_uses_passed_through_ids_without_rereading_billing_row(fast_gate_table):
    """The Stripe webhook already read the billing row; passing both ids
    through must skip the duplicate read."""
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(),
    ) as get_mock:
        await fund_top_up(
            owner_id="org_abc",
            amount_microcents=25_000_000,
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
            metronome_customer_id="cust-uuid-1",
            metronome_contract_id="contract-1",
        )
    get_mock.assert_not_awaited()
    fake.edit_contract_add_commits.assert_awaited_once()


@pytest.mark.asyncio
async def test_fund_rearm_failure_does_not_raise(fast_gate_table, provisioned_billing_row, monkeypatch):
    # The Metronome commit is what releases money; the cache re-arm is a
    # best-effort convenience. A re-arm failure after a successful commit must
    # not raise (the next low-balance webhook re-syncs the authoritative
    # number), or the webhook would 500 back to Stripe AFTER the commit landed.
    fake = _fake_client()
    monkeypatch.setattr(
        "core.billing.metronome_funding.MetronomeFastGate.add_credit",
        AsyncMock(side_effect=RuntimeError("ddb down")),
    )
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()


@pytest.mark.asyncio
async def test_fund_raises_when_customer_not_provisioned(fast_gate_table, provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="customer not provisioned"):
        await fund_top_up(
            owner_id="org_unprovisioned",
            amount_microcents=25_000_000,
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )
    fake.edit_contract_add_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_fund_raises_when_contract_not_provisioned(fast_gate_table):
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(metronome_contract_id=None)),
    ):
        with pytest.raises(FundingError, match="contract not provisioned"):
            await fund_top_up(
                owner_id="org_abc",
                amount_microcents=25_000_000,
                product_id="prod-credits",
                stripe_session_id="cs_test_123",
                client=fake,
            )
    fake.edit_contract_add_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_fund_rejects_non_positive_amount(fast_gate_table, provisioned_billing_row):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await fund_top_up(
            owner_id="org_abc",
            amount_microcents=0,
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )


@pytest.mark.asyncio
async def test_fund_restores_bedrock_and_rearms_flag(fast_gate_table, provisioned_billing_row, restore_bedrock_mock):
    """A confirmed top-up must undo the kill-switch (restore the per-owner
    Bedrock policy) AND re-arm the fast-gate flag — Metronome fires no
    recovery webhook, so this is the only automatic un-block path."""
    fast_gate_table.put_item(
        TableName="test-metronome-fast-gate",
        Item={"owner_id": {"S": "org_abc"}, "remaining_microcents": {"N": "-500"}},
    )
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=_fake_client(),
    )
    restore_bedrock_mock.assert_awaited_once_with("org_abc")
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["remaining_microcents"]["N"] == "24999500"


@pytest.mark.asyncio
async def test_fund_skips_restore_for_non_bedrock_provider(fast_gate_table, restore_bedrock_mock):
    """BYO/OAuth owners have no per-owner AIP map — restoring would only log
    noise on every top-up, so the un-kill is gated on provider_choice."""
    fake = _fake_client()
    with patch(
        "core.billing.metronome_funding.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=_billing_row(provider_choice="byo_key")),
    ):
        await fund_top_up(
            owner_id="org_abc",
            amount_microcents=25_000_000,
            product_id="prod-credits",
            stripe_session_id="cs_test_123",
            client=fake,
        )
    fake.edit_contract_add_commits.assert_awaited_once()
    restore_bedrock_mock.assert_not_awaited()
    # The fast-gate re-arm is provider-independent (the gate blocks chat for
    # every provider path) and must still land.
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["remaining_microcents"]["N"] == "25000000"


@pytest.mark.asyncio
async def test_fund_restore_failure_does_not_raise_and_still_rearms(
    fast_gate_table, provisioned_billing_row, restore_bedrock_mock
):
    """restore_owner_bedrock raises for owners with no persisted AIP map
    (container never provisioned) and on IAM flakes — the commit already
    landed against a confirmed payment, so neither may 500 the Stripe
    webhook, and the fast-gate re-arm must still proceed."""
    restore_bedrock_mock.side_effect = RuntimeError("no persisted AIP map")
    fake = _fake_client()
    await fund_top_up(
        owner_id="org_abc",
        amount_microcents=25_000_000,
        product_id="prod-credits",
        stripe_session_id="cs_test_123",
        client=fake,
    )
    fake.edit_contract_add_commits.assert_awaited_once()
    item = fast_gate_table.get_item(
        TableName="test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_abc"}},
    )["Item"]
    assert item["remaining_microcents"]["N"] == "25000000"
