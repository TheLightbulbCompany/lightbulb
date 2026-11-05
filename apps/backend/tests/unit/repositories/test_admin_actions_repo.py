"""Tests for admin-actions DynamoDB repository.

Mirrors test_update_repo style — moto mock_aws + table create + patch
core.dynamodb internals.
"""

import os
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto admin-actions table matching the CDK schema."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-admin-actions",
            KeySchema=[
                {"AttributeName": "admin_user_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp_action_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "admin_user_id", "AttributeType": "S"},
                {"AttributeName": "timestamp_action_id", "AttributeType": "S"},
                {"AttributeName": "target_user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "target-timestamp-index",
                    "KeySchema": [
                        {"AttributeName": "target_user_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp_action_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-admin-actions")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _row(**overrides):
    base = dict(
        admin_user_id="user_admin",
        target_user_id="user_target",
        action="container.reprovision",
        payload={"tier": "starter"},
        result="success",
        audit_status="written",
        http_status=200,
        elapsed_ms=240,
        error_message=None,
        user_agent="Mozilla/5.0",
        ip="203.0.113.1",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_writes_all_required_fields(dynamodb_table):
    from core.repositories import admin_actions_repo

    item = await admin_actions_repo.create(**_row())
    assert item["admin_user_id"] == "user_admin"
    assert item["target_user_id"] == "user_target"
    assert item["action"] == "container.reprovision"
    assert item["payload"] == {"tier": "starter"}
    assert item["result"] == "success"
    assert item["audit_status"] == "written"
    assert item["http_status"] == 200
    assert item["elapsed_ms"] == 240
    assert item["user_agent"] == "Mozilla/5.0"
    assert item["ip"] == "203.0.113.1"
    # Generated SK shape: {ISO8601}#{ulid-like}
    sk = item["timestamp_action_id"]
    assert "#" in sk
    iso, action_id = sk.split("#", 1)
    assert "T" in iso  # ISO 8601 timestamp
    assert len(action_id) > 8  # non-trivial unique id


@pytest.mark.asyncio
async def test_create_omits_error_message_on_success(dynamodb_table):
    from core.repositories import admin_actions_repo

    item = await admin_actions_repo.create(**_row(error_message=None))
    assert "error_message" not in item


@pytest.mark.asyncio
async def test_create_includes_error_message_on_failure(dynamodb_table):
    from core.repositories import admin_actions_repo

    item = await admin_actions_repo.create(**_row(result="error", error_message="container_not_found", http_status=404))
    assert item["error_message"] == "container_not_found"
    assert item["http_status"] == 404
    assert item["result"] == "error"


@pytest.mark.asyncio
async def test_query_by_target_returns_newest_first(dynamodb_table):
    """Audit feed reads newest-first via the target-timestamp-index GSI."""
    from core.repositories import admin_actions_repo

    # Three actions on the same target, different times (uuid7 ensures ordering).
    for i in range(3):
        await admin_actions_repo.create(**_row(action=f"action_{i}"))

    page = await admin_actions_repo.query_by_target("user_target", limit=10)
    items = page["items"]
    assert len(items) == 3
    # Newest first
    actions_in_order = [r["action"] for r in items]
    assert actions_in_order == ["action_2", "action_1", "action_0"]


@pytest.mark.asyncio
async def test_query_by_target_filters_to_specified_user(dynamodb_table):
    from core.repositories import admin_actions_repo

    await admin_actions_repo.create(**_row(target_user_id="user_a"))
    await admin_actions_repo.create(**_row(target_user_id="user_b"))
    await admin_actions_repo.create(**_row(target_user_id="user_a"))

    page = await admin_actions_repo.query_by_target("user_a")
    assert len(page["items"]) == 2
    assert all(r["target_user_id"] == "user_a" for r in page["items"])


@pytest.mark.asyncio
async def test_query_by_target_respects_limit(dynamodb_table):
    from core.repositories import admin_actions_repo

    for _ in range(5):
        await admin_actions_repo.create(**_row())

    page = await admin_actions_repo.query_by_target("user_target", limit=2)
    assert len(page["items"]) == 2


@pytest.mark.asyncio
async def test_query_by_admin_returns_newest_first(dynamodb_table):
    from core.repositories import admin_actions_repo

    for i in range(3):
        await admin_actions_repo.create(**_row(action=f"action_{i}"))

    page = await admin_actions_repo.query_by_admin("user_admin", limit=10)
    items = page["items"]
    assert len(items) == 3
    assert [r["action"] for r in items] == ["action_2", "action_1", "action_0"]


@pytest.mark.asyncio
async def test_query_by_admin_isolates_admins(dynamodb_table):
    from core.repositories import admin_actions_repo

    await admin_actions_repo.create(**_row(admin_user_id="admin_a"))
    await admin_actions_repo.create(**_row(admin_user_id="admin_b"))

    page_a = await admin_actions_repo.query_by_admin("admin_a")
    page_b = await admin_actions_repo.query_by_admin("admin_b")
    assert len(page_a["items"]) == 1
    assert len(page_b["items"]) == 1
    assert page_a["items"][0]["admin_user_id"] == "admin_a"


@pytest.mark.asyncio
async def test_scan_recent_returns_rows_newest_first_across_admins(dynamodb_table):
    """Unfiltered audit view: rows from every admin/target, newest-first.

    This is the case that used to raise ValueError -> 400 -> empty list."""
    from core.repositories import admin_actions_repo

    # Rows from two different admins, interleaved in time.
    await admin_actions_repo.create(**_row(admin_user_id="admin_a", action="action_0"))
    await admin_actions_repo.create(**_row(admin_user_id="admin_b", action="action_1"))
    await admin_actions_repo.create(**_row(admin_user_id="admin_a", action="action_2"))

    page = await admin_actions_repo.scan_recent(limit=10)
    assert [r["action"] for r in page["items"]] == ["action_2", "action_1", "action_0"]
    assert page["cursor"] is None


@pytest.mark.asyncio
async def test_scan_recent_paginates_by_offset_cursor(dynamodb_table):
    from core.repositories import admin_actions_repo

    for i in range(5):
        await admin_actions_repo.create(**_row(admin_user_id=f"admin_{i}", action=f"action_{i}"))

    first = await admin_actions_repo.scan_recent(limit=2)
    assert [r["action"] for r in first["items"]] == ["action_4", "action_3"]
    assert first["cursor"] == "2"

    second = await admin_actions_repo.scan_recent(limit=2, cursor=first["cursor"])
    assert [r["action"] for r in second["items"]] == ["action_2", "action_1"]
    assert second["cursor"] == "4"

    third = await admin_actions_repo.scan_recent(limit=2, cursor=second["cursor"])
    assert [r["action"] for r in third["items"]] == ["action_0"]
    assert third["cursor"] is None


@pytest.mark.asyncio
async def test_scan_recent_empty_table(dynamodb_table):
    from core.repositories import admin_actions_repo

    page = await admin_actions_repo.scan_recent()
    assert page == {"items": [], "cursor": None}


@pytest.mark.asyncio
async def test_create_accepts_float_in_payload(dynamodb_table):
    from core.repositories import admin_actions_repo

    item = await admin_actions_repo.create(**_row(payload={"new": {"steer_threshold_p": 0.7}}))
    assert item["payload"]["new"]["steer_threshold_p"] == Decimal("0.7")
