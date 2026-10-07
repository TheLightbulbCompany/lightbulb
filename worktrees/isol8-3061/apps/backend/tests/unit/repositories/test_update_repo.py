"""Tests for pending-updates DynamoDB repository."""

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB pending-updates table with status GSI."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-pending-updates",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "update_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "update_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "scheduled_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "scheduled_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-pending-updates")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_create(dynamodb_table):
    from core.repositories import update_repo

    result = await update_repo.create(
        owner_id="user_1",
        update_type="config_patch",
        description="Update model to claude-4",
        changes={"path": "provider.model", "value": "claude-4"},
    )
    assert result["owner_id"] == "user_1"
    assert result["update_type"] == "config_patch"
    assert result["status"] == "pending"
    assert result["description"] == "Update model to claude-4"
    assert "update_id" in result
    assert "created_at" in result
    assert "force_by" not in result


@pytest.mark.asyncio
async def test_create_with_force_by(dynamodb_table):
    from core.repositories import update_repo

    result = await update_repo.create(
        owner_id="user_1",
        update_type="config_patch",
        description="Security update",
        changes={"patch": "security"},
        force_by="2026-04-01T00:00:00+00:00",
    )
    assert result["force_by"] == "2026-04-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_create_scheduled_is_atomic(dynamodb_table):
    from core.repositories import update_repo

    result = await update_repo.create(
        owner_id="user_1",
        update_type="task_definition_refresh",
        description="Refresh from base",
        changes={"refresh_from_base": True},
        scheduled_at="2026-08-15T12:00:00+00:00",
    )

    assert result["status"] == "scheduled"
    assert result["scheduled_at"] == "2026-08-15T12:00:00+00:00"
    stored = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": result["update_id"]})["Item"]
    assert stored["status"] == "scheduled"


@pytest.mark.asyncio
async def test_mark_applied(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    ok = await update_repo.mark_applied("user_1", item["update_id"])
    assert ok is True

    stored = await update_repo.get_by_id("user_1", item["update_id"])
    assert stored["status"] == "applied"
    assert "ttl" in stored


@pytest.mark.asyncio
async def test_mark_applied_from_applying_state(dynamodb_table):
    """Regression: apply_update flips a row pending -> applying BEFORE doing the
    work and calling mark_applied, so mark_applied must accept the "applying"
    state. When its condition only accepted pending/scheduled, the flip to
    "applied" silently failed (ConditionalCheckFailed -> swallowed) and the row
    stayed stuck in "applying" forever even though the ECS change had run."""
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "image_update", "Img", {"new_image": "repo:tag"})

    # Mirror apply_update step 1: pending -> applying.
    dynamodb_table.update_item(
        Key={"owner_id": "user_1", "update_id": item["update_id"]},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "applying"},
    )

    # mark_applied must now succeed from "applying" (the bug returned False here).
    ok = await update_repo.mark_applied("user_1", item["update_id"])
    assert ok is True

    stored = await update_repo.get_by_id("user_1", item["update_id"])
    assert stored["status"] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_marker",
    [
        {"update_type": "release"},
        {"update_type": "task_definition_refresh", "release_revision": 0},
        {"update_type": "config_patch", "release_revision": None},
    ],
    ids=["release-type", "refresh-revision-zero", "null-revision"],
)
@pytest.mark.parametrize(
    ("mutation", "status"),
    [
        ("mark_applied", "applying"),
        ("mark_failed", "applying"),
    ],
)
async def test_release_owned_updates_refuse_every_ordinary_mutation(dynamodb_table, release_marker, mutation, status):
    from core.repositories import update_repo

    item = {
        "owner_id": "release-owner",
        "update_id": f"release-{mutation}",
        "status": status,
        "apply_claim_id": "claim",
        "apply_claimed_at": 1,
        "updated_at": "before",
        **release_marker,
    }
    dynamodb_table.put_item(Item=item)

    if mutation == "mark_applied":
        result = await update_repo.mark_applied(item["owner_id"], item["update_id"])
    else:
        result = await update_repo.mark_failed(item["owner_id"], item["update_id"], "must-not-land")

    assert result is False
    stored = dynamodb_table.get_item(Key={"owner_id": item["owner_id"], "update_id": item["update_id"]})["Item"]
    assert stored == item


@pytest.mark.asyncio
@pytest.mark.parametrize("consistent", [False, True])
async def test_owner_apply_lookup_uses_exact_key_and_consistency(monkeypatch, consistent):
    from core.repositories import update_repo

    row = {"owner_id": "u", "update_id": "r", "status": "pending"}
    table = MagicMock()
    table.get_item.return_value = {"Item": row}
    monkeypatch.setattr(update_repo, "_get_table", lambda: table)

    assert await update_repo.get_by_id("u", "r", consistent=consistent) is row
    table.get_item.assert_called_once_with(Key={"owner_id": "u", "update_id": "r"}, ConsistentRead=consistent)


@pytest.mark.asyncio
async def test_owner_apply_lookup_missing_and_aws_error_propagate(monkeypatch):
    from core.repositories import update_repo

    table = MagicMock()
    table.get_item.return_value = {}
    monkeypatch.setattr(update_repo, "_get_table", lambda: table)
    assert await update_repo.get_by_id("u", "missing") is None

    error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "GetItem")
    table.get_item.side_effect = error
    with pytest.raises(ClientError) as caught:
        await update_repo.get_by_id("u", "r")
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner_id", "update_id", "field"), [("", "r", "owner_id"), ("u", " ", "update_id")])
async def test_owner_apply_lookup_validates_keys_before_io(monkeypatch, owner_id, update_id, field):
    from core.repositories import update_repo

    monkeypatch.setattr(update_repo, "_get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match=field):
        await update_repo.get_by_id(owner_id, update_id)
