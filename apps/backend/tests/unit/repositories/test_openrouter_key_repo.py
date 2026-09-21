import boto3
import pytest
from moto import mock_aws

from core.repositories import openrouter_key_repo


@pytest.mark.asyncio
async def test_only_claim_owner_can_activate_key(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-test-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)

        assert await openrouter_key_repo.claim_new("owner-123", "attempt-a", now_sec=100)
        assert not await openrouter_key_repo.claim_new("owner-123", "attempt-b", now_sec=100)
        assert not await openrouter_key_repo.activate(
            "owner-123", "attempt-b", "key-hash", "secret-name", "workspace-uuid"
        )
        assert await openrouter_key_repo.activate("owner-123", "attempt-a", "key-hash", "secret-name", "workspace-uuid")
        assert await openrouter_key_repo.get("owner-123") == {
            "owner_id": "owner-123",
            "claim_id": "attempt-a",
            "claimed_at": 100,
            "state": "active",
            "key_hash": "key-hash",
            "secret_name": "secret-name",
            "workspace_id": "workspace-uuid",
        }


@pytest.mark.asyncio
async def test_stale_claim_takeover_blocks_original_attempt(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-test-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)

        assert await openrouter_key_repo.claim_new("owner-123", "attempt-a", now_sec=100)
        assert not await openrouter_key_repo.reclaim_stale("owner-123", "attempt-b", now_sec=150, ttl_sec=100)
        assert await openrouter_key_repo.reclaim_stale("owner-123", "attempt-b", now_sec=201, ttl_sec=100)
        assert not await openrouter_key_repo.activate("owner-123", "attempt-a", "old", "old-secret", "workspace-uuid")
        assert await openrouter_key_repo.activate("owner-123", "attempt-b", "new", "new-secret", "workspace-uuid")


@pytest.mark.asyncio
async def test_retirement_blocks_a_new_gate_key_without_deleting_historical_secret(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-test-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        table.put_item(
            Item={
                "owner_id": "owner-123",
                "state": "active",
                "key_hash": "key-hash",
                "secret_name": "historical-secret",
            }
        )
        assert await openrouter_key_repo.mark_retiring("owner-123") is True
        assert await openrouter_key_repo.mark_retiring("owner-123") is False
        row = table.get_item(Key={"owner_id": "owner-123"})["Item"]
        assert row["state"] == "retiring"
        assert row["secret_name"] == "historical-secret"


@pytest.mark.asyncio
async def test_retirement_racing_pending_creation_prevents_activation(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-test-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        assert await openrouter_key_repo.claim_new("owner-123", "attempt-a", now_sec=100)
        assert await openrouter_key_repo.mark_retiring("owner-123")
        assert not await openrouter_key_repo.activate(
            "owner-123", "attempt-a", "key-hash", "secret-name", "workspace-uuid"
        )
        assert not await openrouter_key_repo.reclaim_stale("owner-123", "attempt-b", now_sec=1000, ttl_sec=300)
        row = table.get_item(Key={"owner_id": "owner-123"})["Item"]
        assert row["state"] == "pending"
        assert row["retire_requested_at"]


@pytest.mark.asyncio
async def test_retirement_retries_if_creation_activates_between_conditional_updates(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-test-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"owner_id": "owner-123", "state": "pending", "claim_id": "attempt-a"})

        class RacingTable:
            def __init__(self):
                self.raced = False

            def get_item(self, **kwargs):
                return table.get_item(**kwargs)

            def update_item(self, **kwargs):
                if not self.raced and kwargs.get("ExpressionAttributeValues", {}).get(":expected") == "pending":
                    self.raced = True
                    table.put_item(Item={"owner_id": "owner-123", "state": "active", "key_hash": "key-hash"})
                return table.update_item(**kwargs)

        racing = RacingTable()
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: racing)
        assert await openrouter_key_repo.mark_retiring("owner-123") is True
        assert table.get_item(Key={"owner_id": "owner-123"})["Item"]["state"] == "retiring"
