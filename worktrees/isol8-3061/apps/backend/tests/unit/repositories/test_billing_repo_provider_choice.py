"""Tests for billing_repo.set_provider_choice + oauth-connector handling."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import billing_repo


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB billing-accounts table with Stripe GSI."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
                {"AttributeName": "apple_original_transaction_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "apple-original-transaction-index",
                    "KeySchema": [{"AttributeName": "apple_original_transaction_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-billing-accounts")

        # The containers table exists so a test can prove set_provider_choice
        # never writes it (Task 8a: no provider-switch marker).
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.Table("test-containers").meta.client.get_waiter("table_exists").wait(TableName="test-containers")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_set_provider_choice_personal_managed(dynamodb_table):
    await billing_repo.create_if_not_exists("user_x", "cus_abc", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_x",
        provider_choice="openrouter_managed",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("user_x")
    assert row["provider_choice"] == "openrouter_managed"
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_set_provider_choice_personal_byo_key(dynamodb_table):
    await billing_repo.create_if_not_exists("user_y", "cus_def", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_y",
        provider_choice="byo_key",
        byo_provider="openai",
    )
    row = await billing_repo.get_by_owner_id("user_y")
    assert row["provider_choice"] == "byo_key"
    assert row["byo_provider"] == "openai"


@pytest.mark.asyncio
async def test_set_provider_choice_personal_chatgpt_oauth(dynamodb_table):
    await billing_repo.create_if_not_exists("user_z", "cus_ghi", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_z",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("user_z")
    assert row["provider_choice"] == "chatgpt_oauth"


@pytest.mark.asyncio
async def test_set_provider_choice_allows_chatgpt_oauth_for_org(dynamodb_table):
    """Orgs may now pick chatgpt_oauth — the old org invariant is gone.

    The owner's connector user id is persisted alongside the choice so the
    container knows whose ChatGPT tokens back it.
    """
    await billing_repo.create_if_not_exists("org_123", "cus_org1", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_abc",
    )
    row = await billing_repo.get_by_owner_id("org_123")
    assert row["provider_choice"] == "chatgpt_oauth"
    assert row["oauth_connector_user_id"] == "user_abc"


@pytest.mark.asyncio
async def test_set_provider_choice_chatgpt_without_connector_keeps_existing(dynamodb_table):
    """A chatgpt_oauth write that carries no connector (e.g. the webhook
    backup) must NOT strip the connector that the synchronous
    trial-checkout/change-provider write already persisted."""
    await billing_repo.create_if_not_exists("org_123", "cus_org2", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_abc",
    )
    # Webhook backup arrives without a connector.
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("org_123")
    assert row["oauth_connector_user_id"] == "user_abc"


@pytest.mark.asyncio
async def test_set_provider_choice_non_chatgpt_removes_connector(dynamodb_table):
    """Switching away from chatgpt_oauth drops any persisted connector."""
    await billing_repo.create_if_not_exists("org_123", "cus_org3", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_abc",
    )
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="openrouter_managed",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("org_123")
    assert "oauth_connector_user_id" not in row


@pytest.mark.asyncio
async def test_set_provider_choice_org_managed(dynamodb_table):
    await billing_repo.create_if_not_exists("org_y", "cus_mno", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_y",
        provider_choice="openrouter_managed",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("org_y")
    assert row["provider_choice"] == "openrouter_managed"


@pytest.mark.asyncio
async def test_set_provider_choice_org_byo_key(dynamodb_table):
    await billing_repo.create_if_not_exists("org_z", "cus_pqr", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_z",
        provider_choice="byo_key",
        byo_provider="anthropic",
    )
    row = await billing_repo.get_by_owner_id("org_z")
    assert row["provider_choice"] == "byo_key"
    assert row["byo_provider"] == "anthropic"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["invalid_choice", "bedrock_claude", "bedrock_trial"])
async def test_set_provider_choice_unknown_provider_rejected(dynamodb_table, choice):
    await billing_repo.create_if_not_exists("user_w", "cus_stu", owner_type="personal")
    with pytest.raises(ValueError, match="unknown provider_choice"):
        await billing_repo.set_provider_choice(
            "user_w",
            provider_choice=choice,
            byo_provider=None,
        )


@pytest.mark.asyncio
async def test_set_provider_choice_writes_only_the_billing_row(dynamodb_table):
    """Task 8a: the runtime follows through reconcile_runtime, so the container row carries
    no provider-switch marker a stale write could wedge turns on."""
    await billing_repo.create_if_not_exists("user_sw", "cus_sw", owner_type="personal")
    containers = boto3.resource("dynamodb", region_name="us-east-1").Table("test-containers")
    containers.put_item(Item={"owner_id": "user_sw", "status": "running"})

    row = await billing_repo.set_provider_choice("user_sw", provider_choice="chatgpt_oauth", byo_provider=None)

    assert row["provider_choice"] == "chatgpt_oauth"
    assert "_switch_mark" not in row
    assert containers.get_item(Key={"owner_id": "user_sw"})["Item"] == {"owner_id": "user_sw", "status": "running"}


@pytest.mark.asyncio
async def test_set_provider_choice_byo_key_without_provider_now_allowed(dynamodb_table):
    """byo_provider can be set later via the BYO settings step; not required
    at /trial-checkout time. The picker submits {provider_choice: 'byo_key'}
    alone.
    """
    await billing_repo.create_if_not_exists("user_v", "cus_vwx", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_v",
        provider_choice="byo_key",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("user_v")
    assert row["provider_choice"] == "byo_key"
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_set_provider_choice_overwrites_byo_provider_when_switching_away(dynamodb_table):
    """Switching from byo_key to openrouter_managed should clear byo_provider on the row."""
    await billing_repo.create_if_not_exists("user_t", "cus_yz1", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_t",
        provider_choice="byo_key",
        byo_provider="openai",
    )
    await billing_repo.set_provider_choice(
        "user_t",
        provider_choice="openrouter_managed",
        byo_provider=None,
    )
    row = await billing_repo.get_by_owner_id("user_t")
    assert row["provider_choice"] == "openrouter_managed"
    assert "byo_provider" not in row or row["byo_provider"] is None


@pytest.mark.asyncio
async def test_claim_oauth_connector_is_set_once(dynamodb_table):
    """The first claim records the owner as the connector."""
    await billing_repo.create_if_not_exists("org_123", "cus_claim1", owner_type="org")
    await billing_repo.claim_oauth_connector("org_123", connector_user_id="user_abc")
    row = await billing_repo.get_by_owner_id("org_123")
    assert row["oauth_connector_user_id"] == "user_abc"


@pytest.mark.asyncio
async def test_claim_oauth_connector_noops_when_already_claimed(dynamodb_table):
    """A second admin signing in must NOT repoint the org — set-once."""
    await billing_repo.create_if_not_exists("org_123", "cus_claim2", owner_type="org")
    await billing_repo.claim_oauth_connector("org_123", connector_user_id="user_abc")
    # A different admin tries to claim — must not raise, must not overwrite.
    await billing_repo.claim_oauth_connector("org_123", connector_user_id="user_other")
    row = await billing_repo.get_by_owner_id("org_123")
    assert row["oauth_connector_user_id"] == "user_abc"


@pytest.mark.asyncio
async def test_claim_oauth_connector_missing_row_is_noop(dynamodb_table):
    """No billing row → silent no-op (same phantom-row hazard guard as
    clear_provider_choice); must not upsert a phantom row."""
    await billing_repo.claim_oauth_connector("org_ghost", connector_user_id="user_abc")
    assert await billing_repo.get_by_owner_id("org_ghost") is None


@pytest.mark.asyncio
async def test_clear_provider_choice(dynamodb_table):
    await billing_repo.create_if_not_exists("user_s", "cus_234", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_s",
        provider_choice="byo_key",
        byo_provider="openai",
    )
    await billing_repo.clear_provider_choice("user_s")
    row = await billing_repo.get_by_owner_id("user_s")
    assert row.get("provider_choice") is None
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_clear_provider_choice_also_removes_connector(dynamodb_table):
    """Clearing the provider choice must also drop a persisted connector so a
    disconnect fully unbinds the ChatGPT account from the row."""
    await billing_repo.create_if_not_exists("org_123", "cus_clear1", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_123",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id="user_abc",
    )
    await billing_repo.clear_provider_choice("org_123")
    row = await billing_repo.get_by_owner_id("org_123")
    assert row.get("provider_choice") is None
    assert "oauth_connector_user_id" not in row


@pytest.mark.asyncio
async def test_clear_provider_choice_no_row_is_noop(dynamodb_table):
    """clear_provider_choice must NOT create a phantom billing row when
    called for an owner without one. Otherwise
    /billing/trial-checkout's `if not account` check sees the phantom and
    skips Stripe customer creation."""
    await billing_repo.clear_provider_choice("user_does_not_exist")
    row = await billing_repo.get_by_owner_id("user_does_not_exist")
    assert row is None  # no phantom row created


@pytest.mark.asyncio
async def test_clear_provider_choice_existing_row_clears_fields(dynamodb_table):
    """Sanity: when the row exists, clear still works."""
    await billing_repo.create_if_not_exists("user_y2", "cus_xyz", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_y2",
        provider_choice="byo_key",
        byo_provider="openai",
    )
    await billing_repo.clear_provider_choice("user_y2")
    row = await billing_repo.get_by_owner_id("user_y2")
    assert row is not None  # row preserved
    assert row.get("provider_choice") is None
    assert row.get("byo_provider") is None
    assert row.get("stripe_customer_id") == "cus_xyz"  # original fields preserved


@pytest.mark.asyncio
async def test_clear_phantom_connector_removes_matching_value(dynamodb_table):
    """CAS clear drops the recorded connector when it still equals the phantom id."""
    await billing_repo.create_if_not_exists("org_ph", "cus_ph1", owner_type="org")
    await billing_repo.claim_oauth_connector("org_ph", connector_user_id="user_ghost")
    assert await billing_repo.clear_phantom_oauth_connector("org_ph", expected_user_id="user_ghost") is True
    row = await billing_repo.get_by_owner_id("org_ph")
    assert "oauth_connector_user_id" not in row


@pytest.mark.asyncio
async def test_clear_phantom_connector_refuses_on_mismatch(dynamodb_table):
    """A changed value (e.g. a concurrent real claim) must NOT be clobbered."""
    await billing_repo.create_if_not_exists("org_ph2", "cus_ph2", owner_type="org")
    await billing_repo.claim_oauth_connector("org_ph2", connector_user_id="user_real")
    assert await billing_repo.clear_phantom_oauth_connector("org_ph2", expected_user_id="user_ghost") is False
    row = await billing_repo.get_by_owner_id("org_ph2")
    assert row["oauth_connector_user_id"] == "user_real"


@pytest.mark.asyncio
async def test_clear_phantom_connector_noop_without_row(dynamodb_table):
    """Missing row or missing attribute → False, and no phantom row is created."""
    assert await billing_repo.clear_phantom_oauth_connector("org_nonexistent", expected_user_id="user_ghost") is False
    assert await billing_repo.get_by_owner_id("org_nonexistent") is None
    await billing_repo.create_if_not_exists("org_bare", "cus_ph3", owner_type="org")
    assert await billing_repo.clear_phantom_oauth_connector("org_bare", expected_user_id="user_ghost") is False
