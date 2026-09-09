"""Tests for pending-updates DynamoDB repository."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
        containers = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        containers.meta.client.get_waiter("table_exists").wait(TableName="test-containers")

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
async def test_get_pending(dynamodb_table):
    from core.repositories import update_repo

    await update_repo.create("user_1", "config_patch", "Update A", {"a": 1})
    await update_repo.create("user_1", "config_patch", "Update B", {"b": 2})
    await update_repo.create("user_2", "config_patch", "Update C", {"c": 3})

    results = await update_repo.get_pending("user_1")
    assert len(results) == 2
    descriptions = {r["description"] for r in results}
    assert descriptions == {"Update A", "Update B"}


@pytest.mark.asyncio
async def test_get_pending_filters_applied(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Update A", {"a": 1})
    # Mark as applied -- should be filtered out
    await update_repo.set_status_conditional("user_1", item["update_id"], "applied", ["pending"])

    results = await update_repo.get_pending("user_1")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_get_pending_empty(dynamodb_table):
    from core.repositories import update_repo

    results = await update_repo.get_pending("nonexistent_user")
    assert results == []


@pytest.mark.asyncio
async def test_get_in_flight_includes_applying_excludes_terminal(dynamodb_table):
    """get_in_flight is a superset of get_pending: pending + scheduled + the
    transient 'applying' state. Terminal rows (applied/failed) and other owners
    are excluded. Used by the admin force-apply guard for per-owner exclusion."""
    from core.repositories import update_repo

    await update_repo.create("user_1", "image_update", "P", {"a": 1})  # pending
    applying = await update_repo.create("user_1", "image_update", "AP", {"b": 2})
    await update_repo.set_status_conditional("user_1", applying["update_id"], "applying", ["pending"])
    applied = await update_repo.create("user_1", "image_update", "AD", {"c": 3})
    await update_repo.set_status_conditional("user_1", applied["update_id"], "applied", ["pending"])
    await update_repo.create("user_2", "image_update", "other-owner", {"d": 4})

    results = await update_repo.get_in_flight("user_1")
    assert {r["description"] for r in results} == {"P", "AP"}


@pytest.mark.asyncio
async def test_get_in_flight_empty(dynamodb_table):
    from core.repositories import update_repo

    assert await update_repo.get_in_flight("nonexistent_user") == []


@pytest.mark.asyncio
async def test_get_in_flight_paginates_across_pages(monkeypatch):
    """Follows LastEvaluatedKey so a newer in-flight row on a later page (pushed
    there by historical terminal rows the FilterExpression drops post-read) isn't
    missed by the guard. First page is strongly consistent + carries no
    ExclusiveStartKey; the second reuses page 1's key."""
    from unittest.mock import MagicMock

    from core.repositories import update_repo

    pages = [
        {
            "Items": [{"update_id": "a", "status": "pending"}],
            "LastEvaluatedKey": {"owner_id": "u", "update_id": "a"},
        },
        {"Items": [{"update_id": "b", "status": "applying"}]},
    ]
    seen: list[dict] = []

    def fake_query(**kwargs):
        seen.append(kwargs)
        return pages[len(seen) - 1]

    fake_table = MagicMock()
    fake_table.query = fake_query
    monkeypatch.setattr(update_repo, "_get_table", lambda: fake_table)

    results = await update_repo.get_in_flight("u")

    assert len(seen) == 2
    assert seen[0].get("ConsistentRead") is True
    assert "ExclusiveStartKey" not in seen[0]
    assert seen[1].get("ExclusiveStartKey") == {"owner_id": "u", "update_id": "a"}
    assert {r["update_id"] for r in results} == {"a", "b"}


@pytest.mark.asyncio
async def test_set_status_conditional_success(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    ok = await update_repo.set_status_conditional("user_1", item["update_id"], "applied", ["pending", "scheduled"])
    assert ok is True

    # Verify the status changed
    results = await update_repo.get_pending("user_1")
    assert len(results) == 0  # applied items filtered out


@pytest.mark.asyncio
async def test_set_status_conditional_failure(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    # First transition to applied
    await update_repo.set_status_conditional("user_1", item["update_id"], "applied", ["pending"])
    # Now try to transition from pending again -- should fail
    ok = await update_repo.set_status_conditional("user_1", item["update_id"], "scheduled", ["pending"])
    assert ok is False


@pytest.mark.asyncio
async def test_set_scheduled(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    scheduled_at = "2026-04-01T03:00:00+00:00"
    ok = await update_repo.set_scheduled("user_1", item["update_id"], scheduled_at)
    assert ok is True

    # Verify it shows up in pending (scheduled is included)
    results = await update_repo.get_pending("user_1")
    assert len(results) == 1
    assert results[0]["status"] == "scheduled"
    assert results[0]["scheduled_at"] == scheduled_at


@pytest.mark.asyncio
async def test_set_scheduled_fails_if_not_pending(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    # Apply it first
    await update_repo.set_status_conditional("user_1", item["update_id"], "applied", ["pending"])
    ok = await update_repo.set_scheduled("user_1", item["update_id"], "2026-04-01T03:00:00+00:00")
    assert ok is False


@pytest.mark.asyncio
async def test_set_snoozed(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    ok = await update_repo.set_snoozed("user_1", item["update_id"])
    assert ok is True


@pytest.mark.asyncio
async def test_get_due_scheduled(dynamodb_table):
    from core.repositories import update_repo

    # Create and schedule an update in the past
    item = await update_repo.create("user_1", "config_patch", "Due update", {"x": 1})
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await update_repo.set_scheduled("user_1", item["update_id"], past)

    # Create and schedule an update in the future
    item2 = await update_repo.create("user_2", "config_patch", "Future update", {"y": 2})
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await update_repo.set_scheduled("user_2", item2["update_id"], future)

    due = await update_repo.get_due_scheduled()
    assert len(due) == 1
    assert due[0]["description"] == "Due update"


@pytest.mark.asyncio
async def test_get_due_scheduled_empty(dynamodb_table):
    from core.repositories import update_repo

    due = await update_repo.get_due_scheduled()
    assert due == []


@pytest.mark.asyncio
async def test_apply_claim_takeover_fences_target_and_terminal_writes(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create(
        "user_1",
        "task_definition_refresh",
        "Refresh",
        {"refresh_from_base": True},
        scheduled_at="2026-08-15T00:00:00+00:00",
    )
    first = await update_repo.claim_apply("user_1", item["update_id"], "claim-old")
    assert first is not None
    assert first["status"] == "applying"

    # Fresh claims are exclusive.
    assert await update_repo.claim_apply("user_1", item["update_id"], "claim-rival") is None

    dynamodb_table.update_item(
        Key={"owner_id": "user_1", "update_id": item["update_id"]},
        UpdateExpression="SET apply_claimed_at = :stale",
        ExpressionAttributeValues={":stale": 1},
    )
    stale = await update_repo.list_stale_applying("task_definition_refresh", stale_before_epoch=2)
    assert [row["update_id"] for row in stale] == [item["update_id"]]

    claimed = await update_repo.claim_stale_apply(
        "user_1",
        item["update_id"],
        "claim-new",
        stale_before_epoch=2,
    )
    assert claimed is not None
    assert claimed["apply_claim_id"] == "claim-new"
    assert await update_repo.heartbeat_apply("user_1", item["update_id"], "claim-old") is False
    assert await update_repo.set_apply_target("user_1", item["update_id"], "claim-old", "arn:target-old") is False
    assert await update_repo.set_apply_target("user_1", item["update_id"], "claim-new", "arn:target") is True
    assert (
        await update_repo.finish_claimed_apply(
            "user_1", item["update_id"], "claim-old", status="failed", failure_reason="late"
        )
        is False
    )
    assert await update_repo.finish_claimed_apply("user_1", item["update_id"], "claim-new", status="applied") is True

    stored = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": item["update_id"]})["Item"]
    assert stored["status"] == "applied"
    assert stored["target_task_definition_arn"] == "arn:target"


@pytest.mark.asyncio
async def test_list_stale_applying_includes_legacy_row_without_lease(dynamodb_table):
    from core.repositories import update_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "legacy",
            "update_id": "refresh-1",
            "update_type": "task_definition_refresh",
            "status": "applying",
            "changes": {"refresh_from_base": True},
        }
    )

    stale = await update_repo.list_stale_applying("task_definition_refresh", stale_before_epoch=2)
    assert [row["update_id"] for row in stale] == ["refresh-1"]


@pytest.mark.asyncio
async def test_mark_applied(dynamodb_table):
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "config_patch", "Test", {"x": 1})
    ok = await update_repo.mark_applied("user_1", item["update_id"])
    assert ok is True

    # Should no longer show in pending
    results = await update_repo.get_pending("user_1")
    assert len(results) == 0


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
    acquired = await update_repo.set_status_conditional(
        "user_1", item["update_id"], "applying", ["pending", "scheduled"]
    )
    assert acquired is True

    # mark_applied must now succeed from "applying" (the bug returned False here).
    ok = await update_repo.mark_applied("user_1", item["update_id"])
    assert ok is True

    # Row is finalized: status "applied", no longer pending.
    results = await update_repo.get_pending("user_1")
    assert len(results) == 0


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
        ("set_status_conditional", "pending"),
        ("claim_apply", "pending"),
        ("claim_stale_apply", "applying"),
        ("heartbeat_apply", "applying"),
        ("set_apply_target", "applying"),
        ("finish_claimed_apply_applied", "applying"),
        ("finish_claimed_apply_failed", "applying"),
        ("set_scheduled", "pending"),
        ("set_snoozed", "pending"),
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

    if mutation == "set_status_conditional":
        result = await update_repo.set_status_conditional(item["owner_id"], item["update_id"], "scheduled", ["pending"])
    elif mutation == "claim_apply":
        result = await update_repo.claim_apply(item["owner_id"], item["update_id"], "new-claim")
    elif mutation == "claim_stale_apply":
        result = await update_repo.claim_stale_apply(
            item["owner_id"], item["update_id"], "new-claim", stale_before_epoch=2
        )
    elif mutation == "heartbeat_apply":
        result = await update_repo.heartbeat_apply(item["owner_id"], item["update_id"], "claim")
    elif mutation == "set_apply_target":
        result = await update_repo.set_apply_target(item["owner_id"], item["update_id"], "claim", "arn:winner")
    elif mutation.startswith("finish_claimed_apply"):
        terminal_status = mutation.rsplit("_", 1)[-1]
        result = await update_repo.finish_claimed_apply(
            item["owner_id"],
            item["update_id"],
            "claim",
            status=terminal_status,
            failure_reason="must-not-land",
        )
    elif mutation == "set_scheduled":
        result = await update_repo.set_scheduled(item["owner_id"], item["update_id"], "2026-09-09T00:00:00+00:00")
    elif mutation == "set_snoozed":
        result = await update_repo.set_snoozed(item["owner_id"], item["update_id"])
    elif mutation == "mark_applied":
        result = await update_repo.mark_applied(item["owner_id"], item["update_id"])
    else:
        result = await update_repo.mark_failed(item["owner_id"], item["update_id"], "must-not-land")

    assert result is None if mutation in {"claim_apply", "claim_stale_apply"} else result is False
    stored = dynamodb_table.get_item(Key={"owner_id": item["owner_id"], "update_id": item["update_id"]})["Item"]
    assert stored == item


_OWNER_APPLY_ARN = "arn:aws:ecs:us-east-1:123456789012:task-definition/test:1"
_OWNER_APPLY_CONTAINER_EXCLUSIONS = (
    "release_update_id",
    "provision_claim_id",
    "provision_attempt_token",
    "provision_execution_name",
)
_OWNER_APPLY_UPDATE_EXCLUSIONS = (
    "release_revision",
    "container_id",
    "apply_claim_id",
    "apply_claimed_at",
    "phase",
    "target_release_id",
    "prior_release_id",
    "prior_desired_release_id",
    "prior_task_definition_arn",
    "prior_desired_count",
    "target_desired_count",
    "target_task_definition_arn",
    "staged_config_sha256",
    "prior_config_sha256",
    "effect_kind",
    "effect_claim_id",
    "effect_executor_id",
    "applied_at",
    "failure_reason",
)


async def _seed_owner_apply(
    update_repo,
    *,
    owner: dict | None = None,
    update_type: str = "release",
    changes: dict | None = None,
    scheduled_at: str | None = None,
):
    from core.dynamodb import get_table

    containers = get_table("containers")
    owner = owner or {"owner_id": "u", "id": "original", "status": "running"}
    containers.put_item(Item=owner)
    queued = await update_repo.create(
        owner["owner_id"],
        update_type,
        "Candidate",
        changes if changes is not None else {"target_release_id": "candidate"},
        scheduled_at=scheduled_at,
    )
    snapshot = await update_repo.get_by_id(owner["owner_id"], queued["update_id"], consistent=True)
    assert snapshot is not None
    return containers, deepcopy(owner), snapshot


async def _reserve_owner_apply(
    update_repo,
    owner: dict,
    update: dict,
    *,
    target_release_id: str | None = "candidate",
):
    return await update_repo.reserve_owner_apply(
        owner,
        update,
        "worker",
        target_release_id=target_release_id,
        prior_task_definition_arn=_OWNER_APPLY_ARN,
        prior_desired_count=1,
        target_desired_count=1,
    )


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


@pytest.mark.asyncio
async def test_owner_apply_reservation_is_paired(dynamodb_table):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    owner_before = deepcopy(owner)
    queued_before = deepcopy(queued)

    assert await _reserve_owner_apply(update_repo, owner, queued)

    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    stored_u = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert stored_u is not None
    assert stored_c["release_update_id"] == queued["update_id"]
    assert stored_c["desired_release_id"] == stored_u["target_release_id"] == "candidate"
    assert stored_c["release_revision"] == stored_u["release_revision"] == 1
    assert stored_c["provision_claim_id"] == stored_c["provision_attempt_token"] == "worker"
    assert stored_u["apply_claim_id"] == "worker"
    assert stored_u["container_id"] == "original"
    assert stored_u["status"] == "applying" and stored_u["phase"] == "reserved"
    assert "active_release_id" not in stored_c and "prior_release_id" not in stored_u
    assert "prior_desired_release_id" not in stored_u
    assert stored_c["status"] == "running"
    assert stored_c["provision_claimed_at"] == stored_u["apply_claimed_at"]
    assert stored_c["updated_at"] == stored_u["updated_at"]
    assert owner == owner_before and queued == queued_before


@pytest.mark.asyncio
async def test_owner_apply_release_preserves_complete_rows_and_nested_changes(dynamodb_table):
    from core.repositories import update_repo

    owner = {
        "owner_id": "u",
        "id": "original",
        "status": "running",
        "task_definition_arn": "arn:old-task",
        "desired_release_id": "old",
        "active_release_id": "old",
        "release_revision": Decimal("4"),
        "unrelated": {"keep": ["yes", Decimal("2")]},
    }
    nested = {"target_release_id": "candidate", "config": {"flags": [True, Decimal("3")]}}
    containers, owner_snapshot, queued = await _seed_owner_apply(update_repo, owner=owner, changes=nested)
    dynamodb_table.update_item(
        Key={"owner_id": "u", "update_id": queued["update_id"]},
        UpdateExpression="SET #t = :ttl, untouched = :value",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":ttl": 123, ":value": "keep"},
    )
    queued = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    owner_before, queued_before = deepcopy(owner_snapshot), deepcopy(queued)

    assert await _reserve_owner_apply(update_repo, owner_snapshot, queued)

    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    stored_u = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    expected_c = deepcopy(owner_snapshot)
    expected_c.update(
        desired_release_id="candidate",
        release_revision=5,
        release_update_id=queued["update_id"],
        provision_claim_id="worker",
        provision_attempt_token="worker",
        provision_claimed_at=stored_c["provision_claimed_at"],
        updated_at=stored_c["updated_at"],
    )
    expected_u = deepcopy(queued)
    expected_u.pop("ttl")
    expected_u.update(
        status="applying",
        phase="reserved",
        container_id="original",
        apply_claim_id="worker",
        apply_claimed_at=stored_u["apply_claimed_at"],
        updated_at=stored_u["updated_at"],
        release_revision=5,
        target_release_id="candidate",
        prior_release_id="old",
        prior_desired_release_id="old",
        prior_task_definition_arn=_OWNER_APPLY_ARN,
        prior_desired_count=1,
        target_desired_count=1,
    )
    assert stored_c == expected_c
    assert stored_u == expected_u
    assert stored_u["changes"] == nested and stored_u["untouched"] == "keep"
    assert owner_snapshot == owner_before and queued == queued_before


@pytest.mark.asyncio
@pytest.mark.parametrize("assigned", [False, True], ids=["legacy-unassigned", "assigned"])
async def test_owner_apply_non_release_preserves_release_ids(dynamodb_table, assigned):
    from core.repositories import update_repo

    owner = {"owner_id": "u", "id": "original", "status": "running", "runtime": "keep"}
    if assigned:
        owner.update(desired_release_id="old", active_release_id="old")
    containers, owner, queued = await _seed_owner_apply(
        update_repo,
        owner=owner,
        update_type="config_patch",
        changes={"patch": {"enabled": True}},
    )

    assert await _reserve_owner_apply(update_repo, owner, queued, target_release_id=None)

    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    stored_u = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert stored_c.get("desired_release_id") == ("old" if assigned else None)
    assert stored_c.get("active_release_id") == ("old" if assigned else None)
    assert "target_release_id" not in stored_u
    if assigned:
        assert stored_u["prior_release_id"] == stored_u["prior_desired_release_id"] == "old"
    else:
        assert "prior_release_id" not in stored_u and "prior_desired_release_id" not in stored_u
    assert stored_c["release_revision"] == stored_u["release_revision"] == 1
    assert stored_c["runtime"] == "keep" and stored_c["status"] == "running"


@pytest.mark.asyncio
async def test_owner_apply_scheduled_update_can_be_reserved(dynamodb_table):
    from core.repositories import update_repo

    _, owner, queued = await _seed_owner_apply(
        update_repo,
        scheduled_at="2026-09-09T12:00:00+00:00",
    )
    assert queued["status"] == "scheduled"
    assert await _reserve_owner_apply(update_repo, owner, queued)
    stored = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert stored["status"] == "applying" and stored["scheduled_at"] == queued["scheduled_at"]


@pytest.mark.asyncio
async def test_owner_apply_competing_updates_are_atomic(dynamodb_table):
    from core.dynamodb import get_table
    from core.repositories import update_repo

    containers = get_table("containers")
    owner = {"owner_id": "u", "id": "original", "status": "running"}
    containers.put_item(Item=owner)
    first = await update_repo.create("u", "release", "first", {"target_release_id": "candidate"})
    second = await update_repo.create("u", "release", "second", {"target_release_id": "candidate"})
    first = await update_repo.get_by_id("u", first["update_id"], consistent=True)
    second = await update_repo.get_by_id("u", second["update_id"], consistent=True)
    assert await _reserve_owner_apply(update_repo, owner, first)
    winner_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    loser_before = deepcopy(second)

    assert await _reserve_owner_apply(update_repo, owner, second) is False
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == winner_c
    assert await update_repo.get_by_id("u", second["update_id"], consistent=True) == loser_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["claimed", "scheduled", "snoozed", "edited", "update_type", "scheduled_at", "force_by"],
)
async def test_owner_apply_update_race_changes_neither_row(dynamodb_table, monkeypatch, mutation):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread
    calls = 0
    winner_u = deepcopy(queued)

    async def inject_at_transaction(func, *args, **kwargs):
        nonlocal calls
        if getattr(func, "__name__", "") == "transact_write_items":
            calls += 1
            if mutation == "claimed":
                field, value = "apply_claim_id", "rival"
            elif mutation == "scheduled":
                field, value = "status", "scheduled"
            elif mutation == "snoozed":
                field, value = "last_snoozed_at", "later"
            elif mutation == "edited":
                field, value = "changes", {"target_release_id": "edited"}
            else:
                field, value = mutation, "later"
            expression = "SET #s = :v" if mutation == "scheduled" else f"SET {field} = :v"
            values = {":v": value}
            names = {"#s": "status"} if mutation == "scheduled" else None
            request = {
                "Key": {"owner_id": "u", "update_id": queued["update_id"]},
                "UpdateExpression": expression,
                "ExpressionAttributeValues": values,
            }
            if names:
                request["ExpressionAttributeNames"] = names
            dynamodb_table.update_item(**request)
            winner_u[field] = value
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, owner, queued) is False
    assert calls == 1
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == owner
    stored_u = dynamodb_table.get_item(Key={"owner_id": "u", "update_id": queued["update_id"]})["Item"]
    assert stored_u == winner_u


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mutation"),
    [
        ("id", "SET id = :v"),
        ("status", "SET #s = :v"),
        ("task_definition_arn", "SET task_definition_arn = :v"),
        ("desired_release_id", "SET desired_release_id = :v"),
        ("active_release_id", "SET active_release_id = :v"),
        ("release_revision", "SET release_revision = :v"),
        ("absent_revision_to_zero", "SET release_revision = :zero"),
        ("absent_desired_to_null", "SET desired_release_id = :null"),
    ],
)
async def test_owner_apply_container_pins_each_snapshot_field(dynamodb_table, monkeypatch, case, mutation):
    from core.repositories import update_repo

    owner = {
        "owner_id": "u",
        "id": "original",
        "status": "running",
        "task_definition_arn": "task-old",
        "desired_release_id": "old",
        "active_release_id": "old",
        "release_revision": 4,
    }
    if case.startswith("absent_revision"):
        owner.pop("release_revision")
    if case.startswith("absent_desired"):
        owner.pop("desired_release_id")
        owner.pop("active_release_id")
    containers, snapshot, queued = await _seed_owner_apply(update_repo, owner=owner)
    original_run = update_repo.run_in_thread
    winner = deepcopy(snapshot)

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            value = None if "null" in case else 0 if "zero" in case else 5 if case == "release_revision" else "moved"
            binding = ":null" if "null" in case else ":zero" if "zero" in case else ":v"
            request = {
                "Key": {"owner_id": "u"},
                "UpdateExpression": mutation,
                "ExpressionAttributeValues": {binding: value},
            }
            if case == "status":
                request["ExpressionAttributeNames"] = {"#s": "status"}
            containers.update_item(**request)
            field = "desired_release_id" if case == "absent_desired_to_null" else case.removeprefix("absent_")
            field = "release_revision" if field == "revision_to_zero" else field
            winner[field] = value
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    queued_before = deepcopy(queued)
    assert await _reserve_owner_apply(update_repo, snapshot, queued) is False
    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    assert stored_c == winner
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) == queued_before


@pytest.mark.asyncio
async def test_owner_apply_container_revision_detects_release_id_aba(dynamodb_table, monkeypatch):
    from core.repositories import update_repo

    owner = {
        "owner_id": "u",
        "id": "original",
        "status": "running",
        "desired_release_id": "old",
        "active_release_id": "old",
        "release_revision": 4,
    }
    containers, snapshot, queued = await _seed_owner_apply(update_repo, owner=owner)
    original_run = update_repo.run_in_thread

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            containers.update_item(
                Key={"owner_id": "u"},
                UpdateExpression="SET desired_release_id = :same, release_revision = :next",
                ExpressionAttributeValues={":same": "old", ":next": 5},
            )
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, snapshot, queued) is False
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) == queued


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True], ids=["delete", "delete-recreate"])
async def test_owner_apply_container_deletion_is_fenced(dynamodb_table, monkeypatch, recreate):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            containers.delete_item(Key={"owner_id": "u"})
            if recreate:
                containers.put_item(Item={"owner_id": "u", "id": "replacement", "status": "running"})
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, owner, queued) is False
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) == queued
    current = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True).get("Item")
    assert current is None if not recreate else current["id"] == "replacement"


@pytest.mark.asyncio
async def test_owner_apply_update_deletion_is_fenced(dynamodb_table, monkeypatch):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            dynamodb_table.delete_item(Key={"owner_id": "u", "update_id": queued["update_id"]})
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, owner, queued) is False
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == owner
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "error"])
@pytest.mark.parametrize("field", _OWNER_APPLY_CONTAINER_EXCLUSIONS)
@pytest.mark.parametrize("value", ["busy", None], ids=["value", "null"])
async def test_owner_apply_container_exclusion_snapshot_refuses_without_io(monkeypatch, status, field, value):
    from core.repositories import update_repo

    owner = {"owner_id": "u", "id": "original", "status": status, field: value}
    update = {
        "owner_id": "u",
        "update_id": "r",
        "update_type": "release",
        "changes": {"target_release_id": "candidate"},
        "status": "pending",
    }
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    monkeypatch.setattr(update_repo, "_get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    assert await _reserve_owner_apply(update_repo, owner, update) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _OWNER_APPLY_UPDATE_EXCLUSIONS)
@pytest.mark.parametrize("value", ["busy", None], ids=["value", "null"])
async def test_owner_apply_update_exclusion_snapshot_refuses_without_io(monkeypatch, field, value):
    from core.repositories import update_repo

    owner = {"owner_id": "u", "id": "original", "status": "running"}
    update = {
        "owner_id": "u",
        "update_id": "r",
        "update_type": "release",
        "changes": {"target_release_id": "candidate"},
        "status": "pending",
        field: value,
    }
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    monkeypatch.setattr(update_repo, "_get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    assert await _reserve_owner_apply(update_repo, owner, update) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["applying", "applied", "failed"])
async def test_owner_apply_terminal_or_applying_update_refuses_without_io(monkeypatch, status):
    from core.repositories import update_repo

    owner = {"owner_id": "u", "id": "original", "status": "running"}
    update = {
        "owner_id": "u",
        "update_id": "r",
        "update_type": "release",
        "changes": {"target_release_id": "candidate"},
        "status": status,
    }
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    monkeypatch.setattr(update_repo, "_get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    assert await _reserve_owner_apply(update_repo, owner, update) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _OWNER_APPLY_CONTAINER_EXCLUSIONS)
@pytest.mark.parametrize("value", ["busy", None], ids=["value", "null"])
async def test_owner_apply_container_exclusion_is_fenced_at_transaction(dynamodb_table, monkeypatch, field, value):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            containers.update_item(
                Key={"owner_id": "u"},
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": field},
                ExpressionAttributeValues={":value": value},
            )
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, owner, queued) is False
    stored_c = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    expected_c = deepcopy(owner)
    expected_c[field] = value
    assert stored_c == expected_c
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) == queued


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _OWNER_APPLY_UPDATE_EXCLUSIONS)
@pytest.mark.parametrize("value", ["busy", None], ids=["value", "null"])
async def test_owner_apply_update_exclusion_is_fenced_at_transaction(dynamodb_table, monkeypatch, field, value):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread

    async def inject_at_transaction(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "transact_write_items":
            dynamodb_table.update_item(
                Key={"owner_id": "u", "update_id": queued["update_id"]},
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": field},
                ExpressionAttributeValues={":value": value},
            )
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", inject_at_transaction)
    assert await _reserve_owner_apply(update_repo, owner, queued) is False
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == owner
    stored_u = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    expected_u = deepcopy(queued)
    expected_u[field] = value
    assert stored_u == expected_u


def _owner_apply_valid_arguments():
    return (
        {"owner_id": "u", "id": "original", "status": "running"},
        {
            "owner_id": "u",
            "update_id": "r",
            "update_type": "release",
            "changes": {"target_release_id": "candidate"},
            "status": "pending",
        },
        {
            "target_release_id": "candidate",
            "prior_task_definition_arn": _OWNER_APPLY_ARN,
            "prior_desired_count": 1,
            "target_desired_count": 1,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("owner", "owner_id", " "),
        ("owner", "id", ""),
        ("owner", "status", None),
        ("update", "owner_id", "other"),
        ("update", "update_id", 1),
        ("update", "update_type", " "),
        ("claim", "claim_id", ["secret-value"]),
        ("kwargs", "prior_task_definition_arn", " "),
    ],
)
async def test_owner_apply_validation_rejects_bad_identity_fields_without_io(monkeypatch, location, field, value):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    if location == "owner":
        owner[field] = value
    elif location == "update":
        update[field] = value
    elif location == "kwargs":
        kwargs[field] = value
    claim = value if location == "claim" else "worker"
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError) as caught:
        await update_repo.reserve_owner_apply(owner, update, claim, **kwargs)
    assert field in str(caught.value)
    assert "secret-value" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "value", "field"),
    [
        ("owner", None, "expected_container"),
        ("owner", [], "expected_container"),
        ("update", None, "expected_update"),
        ("update", [], "expected_update"),
        ("changes", None, "changes"),
        ("changes", [], "changes"),
    ],
)
async def test_owner_apply_validation_rejects_non_dict_snapshots_without_io(monkeypatch, snapshot, value, field):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    if snapshot == "owner":
        owner = value
    elif snapshot == "update":
        update = value
    else:
        update["changes"] = value
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match=field):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("present", "status"),
    [
        (False, None),
        (True, None),
        (True, ["secret-status"]),
        (True, {"secret-status": True}),
        (True, ""),
        (True, " "),
    ],
    ids=["missing", "null", "list", "dict", "empty", "whitespace"],
)
async def test_owner_apply_validation_rejects_malformed_status_before_io(monkeypatch, present, status):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    if present:
        update["status"] = status
    else:
        update.pop("status")
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    monkeypatch.setattr(update_repo, "_get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))

    with pytest.raises(ValueError) as caught:
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)

    assert str(caught.value) == "status is invalid"
    assert "secret-status" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision",
    [-1, Decimal("1.5"), Decimal("NaN"), Decimal("Infinity"), True, 1.0, "1", None],
    ids=["negative", "fractional", "nan", "infinity", "bool", "float", "string", "null"],
)
async def test_owner_apply_validation_rejects_invalid_revision_without_io(monkeypatch, revision):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    owner["release_revision"] = revision
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match="release_revision"):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["prior_desired_count", "target_desired_count"])
@pytest.mark.parametrize("value", [-1, True, 1.0], ids=["negative", "bool", "float"])
async def test_owner_apply_validation_rejects_invalid_counts_without_io(monkeypatch, field, value):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    kwargs[field] = value
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match=field):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("desired", "active"),
    [(None, None), ("", ""), ("old", None), (None, "old"), ("old", "different")],
    ids=["nulls", "empty", "desired-only", "active-only", "mismatch"],
)
async def test_owner_apply_validation_rejects_invalid_optional_release_ids_without_io(monkeypatch, desired, active):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    owner["desired_release_id"] = desired
    owner["active_release_id"] = active
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match="desired_release_id|active_release_id"):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update_type", "changes", "target", "field"),
    [
        ("release", {}, "candidate", "target_release_id"),
        ("release", {"target_release_id": "other"}, "candidate", "target_release_id"),
        ("release", {"target_release_id": "candidate"}, None, "target_release_id"),
        ("config_patch", {"patch": True}, "candidate", "target_release_id"),
    ],
)
async def test_owner_apply_validation_rejects_release_contract_mismatches_without_io(
    monkeypatch, update_type, changes, target, field
):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    update["update_type"] = update_type
    update["changes"] = changes
    kwargs["target_release_id"] = target
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match=field):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", " ", 1], ids=["empty", "whitespace", "nonstring"])
async def test_owner_apply_validation_rejects_invalid_explicit_target_without_io(monkeypatch, target):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    kwargs["target_release_id"] = target
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError, match="target_release_id"):
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, Decimal("0"), Decimal("4")], ids=["int-zero", "decimal-zero", "decimal"])
async def test_owner_apply_accepts_integral_revision_and_zero_counts(dynamodb_table, revision):
    from core.repositories import update_repo

    owner = {"owner_id": "u", "id": "original", "status": "running", "release_revision": revision}
    _, owner, queued = await _seed_owner_apply(update_repo, owner=owner)
    assert await update_repo.reserve_owner_apply(
        owner,
        queued,
        "worker",
        target_release_id="candidate",
        prior_task_definition_arn=_OWNER_APPLY_ARN,
        prior_desired_count=0,
        target_desired_count=0,
    )


@pytest.mark.asyncio
async def test_owner_apply_owner_mismatch_validation_names_field_without_io(monkeypatch):
    from core.repositories import update_repo

    owner, update, kwargs = _owner_apply_valid_arguments()
    update["owner_id"] = "do-not-leak-owner"
    monkeypatch.setattr(update_repo, "get_table", MagicMock(side_effect=AssertionError("unexpected I/O")))
    with pytest.raises(ValueError) as caught:
        await update_repo.reserve_owner_apply(owner, update, "worker", **kwargs)
    assert "owner_id" in str(caught.value) and "do-not-leak-owner" not in str(caught.value)


def _owner_apply_transaction_error(reasons=None, *, code="TransactionCanceledException"):
    response = {"Error": {"Code": code, "Message": "failed"}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    return ClientError(response, "TransactWriteItems")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reasons",
    [
        [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
        [{"Code": None}, {"Code": "ConditionalCheckFailed"}],
        [{"Code": "ConditionalCheckFailed"}, {"Code": "ConditionalCheckFailed"}],
    ],
    ids=["first", "second", "both"],
)
async def test_owner_apply_transaction_classifier_returns_false_only_for_conditional_matrix(
    dynamodb_table, monkeypatch, reasons
):
    from core.repositories import update_repo

    error = _owner_apply_transaction_error(reasons)
    containers, owner, update = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread
    calls = 0

    async def fail_once(func, *args, **kwargs):
        nonlocal calls
        if getattr(func, "__name__", "") == "transact_write_items":
            calls += 1
            raise error
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", fail_once)
    assert await _reserve_owner_apply(update_repo, owner, update) is False
    assert calls == 1
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == owner
    assert dynamodb_table.get_item(Key={"owner_id": "u", "update_id": update["update_id"]})["Item"] == update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        _owner_apply_transaction_error(),
        _owner_apply_transaction_error([{"Code": "ConditionalCheckFailed"}]),
        _owner_apply_transaction_error([{"Code": "ConditionalCheckFailed"}, {"Code": "None"}, {"Code": "None"}]),
        _owner_apply_transaction_error([{"Code": "ConditionalCheckFailed"}, "bad"]),
        _owner_apply_transaction_error([{}, {"Code": "ConditionalCheckFailed"}]),
        _owner_apply_transaction_error([{"Code": "None"}, {"Code": None}]),
        _owner_apply_transaction_error([{"Code": "ThrottlingError"}, {"Code": "None"}]),
        _owner_apply_transaction_error([{"Code": "ConditionalCheckFailed"}, {"Code": "ThrottlingError"}]),
        _owner_apply_transaction_error(
            [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}], code="ValidationException"
        ),
        TimeoutError("unknown commit outcome"),
    ],
    ids=[
        "missing-reasons",
        "wrong-count-one",
        "wrong-count-three",
        "malformed-entry",
        "missing-code",
        "no-conditional",
        "operational",
        "mixed",
        "non-cancellation",
        "timeout",
    ],
)
async def test_owner_apply_transaction_classifier_propagates_unproven_failures(dynamodb_table, monkeypatch, failure):
    from core.repositories import update_repo

    containers, owner, update = await _seed_owner_apply(update_repo)
    original_run = update_repo.run_in_thread
    calls = 0

    async def fail_once(func, *args, **kwargs):
        nonlocal calls
        if getattr(func, "__name__", "") == "transact_write_items":
            calls += 1
            raise failure
        return await original_run(func, *args, **kwargs)

    monkeypatch.setattr(update_repo, "run_in_thread", fail_once)
    with pytest.raises(type(failure)) as caught:
        await _reserve_owner_apply(update_repo, owner, update)
    assert caught.value is failure and calls == 1
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == owner
    assert dynamodb_table.get_item(Key={"owner_id": "u", "update_id": update["update_id"]})["Item"] == update


@pytest.mark.asyncio
async def test_release_checkpoint_and_finish_require_full_evidence_chain(dynamodb_table, monkeypatch):
    from core.repositories import update_repo
    from core.services import openclaw_release

    release = openclaw_release.OpenClawRelease(
        release_id="candidate",
        image="877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:9.3-final",
        image_digest=f"sha256:{'a' * 64}",
        build_id="build-final",
        upstream_version="2026.9.3",
        fork_tag="v2026.9.3-isol8.1",
        fork_commit="c" * 40,
        schema_id="2026.9.3-isol8.1",
        schema_sha256="d" * 64,
        shared_db_schema_version=20,
        agent_db_schema_version=24,
        config_capabilities=frozenset(),
        qualification_id="founder-native-v1",
        evidence_id=f"sha256:{'e' * 64}",
        runtime_capabilities=frozenset({"sessions.create", "users.self"}),
        qualification_capabilities=frozenset({"legacy-privacy", "machine-coexistence", "native-human-auth"}),
    )
    monkeypatch.setattr(openclaw_release, "release_by_id", lambda release_id: release)

    containers, owner, queued = await _seed_owner_apply(update_repo)
    assert await _reserve_owner_apply(update_repo, owner, queued)

    async def advance(expected_phase, next_phase, *, fields=None, remove_fields=()):
        container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
        update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
        assert await update_repo.checkpoint_owner_release(
            container,
            update,
            "worker",
            expected_phase=expected_phase,
            next_phase=next_phase,
            fields=fields,
            remove_fields=remove_fields,
        )

    container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert not await update_repo.checkpoint_owner_release(
        container,
        update,
        "worker",
        expected_phase="reserved",
        next_phase="validating",
        fields={"target_task_definition_arn": "arn:target"},
    )
    effects = ("effect_kind", "effect_claim_id", "effect_executor_id")
    source = {
        "service_arn": "arn:service",
        "task_arn": "arn:old-task",
        "task_definition_arn": _OWNER_APPLY_ARN,
        "image_digest": f"sha256:{'9' * 64}",
        "build_id": "source-build",
        "access_point_id": "fsap-source",
        "file_system_id": "fs-source",
        "efs_root": "/users/u",
        "shared_schema_version": 15,
        "agent_schema_version": 19,
    }
    await advance(
        "reserved",
        "stop_requested",
        fields={
            "effect_kind": "stop_service",
            "effect_claim_id": "stop-effect",
            "effect_executor_id": "worker",
            "pre_stop_task_arns": ["arn:old-task"],
            "pre_stop_captured_at": "2026-09-09T11:59:00Z",
            "source": source,
        },
    )
    container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert not await update_repo.checkpoint_owner_release(
        container,
        update,
        "worker",
        expected_phase="stop_requested",
        next_phase="drained",
        fields={"drain_evidence": {"service_desired_count": 0}},
        remove_fields=effects,
    )
    drain_evidence = {
        "observed_at": "2026-09-09T12:00:00Z",
        "service_arn": "arn:service",
        "service_desired_count": 0,
        "service_running_count": 0,
        "service_pending_count": 0,
        "stopped_tasks": [
            {
                "task_arn": "arn:old-task",
                "last_status": "STOPPED",
                "task_definition_arn": _OWNER_APPLY_ARN,
                "image_digest": f"sha256:{'9' * 64}",
                "exit_code": 0,
                "stop_code": "UserInitiated",
                "stopped_reason": "service scale down",
            }
        ],
    }
    await advance(
        "stop_requested",
        "drained",
        fields={"drain_evidence": drain_evidence},
        remove_fields=effects,
    )
    await advance(
        "drained",
        "recovery_requested",
        fields={
            "effect_kind": "create_recovery_set",
            "effect_claim_id": "recovery-effect",
            "effect_executor_id": "worker",
        },
    )
    recovery_set = {
        "recovery_set_id": "rs-recovery-v1",
        "owner_id": "u",
        "update_id": queued["update_id"],
        "status": "verified",
        "manifest_bucket": "artifacts",
        "manifest_key": "recovery-sets/u/rs-recovery-v1/manifest.json",
        "manifest_version_id": "version-1",
        "manifest_sha256": "3" * 64,
        "release_id": "candidate",
        "release_image_digest": release.image_digest,
        "source_service_arn": source["service_arn"],
        "source_task_arn": source["task_arn"],
        "source_task_definition_arn": source["task_definition_arn"],
        "source_image_digest": source["image_digest"],
        "source_build_id": source["build_id"],
        "access_point_id": source["access_point_id"],
        "file_system_id": source["file_system_id"],
        "efs_root": source["efs_root"],
        "drain_observed_at": drain_evidence["observed_at"],
        "drain_evidence_sha256": hashlib.sha256(
            json.dumps(drain_evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "stable_replica_inventory_sha256": "6" * 64,
        "shared_db_generation": "7" * 64,
        "agent_db_generations": {},
        "efs_archive_key": "recovery-sets/u/rs-recovery-v1/efs/owner-root.tar",
        "efs_archive_version_id": "version-2",
        "efs_archive_etag": "etag",
        "efs_archive_size": 1,
        "efs_archive_sha256": "4" * 64,
        "efs_tree_manifest_sha256": "5" * 64,
        "provider": {"name": "lightbulb-owner-recovery-set", "version": "1"},
        "tools": {"litestream": "0.5.15", "sqlite": "3"},
        "verified_at": "2026-09-09T12:00:00Z",
    }
    await advance(
        "recovery_requested",
        "recovered",
        fields={"recovery_set": recovery_set},
        remove_fields=effects,
    )
    await advance(
        "recovered",
        "prepare_requested",
        fields={
            "effect_kind": "prepare_task_definition",
            "effect_claim_id": "prepare-effect",
            "effect_executor_id": "worker",
        },
    )
    await advance(
        "prepare_requested",
        "prepared",
        fields={"target_task_definition_arn": "arn:target"},
        remove_fields=effects,
    )
    await advance(
        "prepared",
        "stage_requested",
        fields={
            "effect_kind": "stage_config",
            "effect_claim_id": "stage-effect",
            "effect_executor_id": "worker",
        },
    )
    await advance(
        "stage_requested",
        "staged",
        fields={"prior_config_sha256": "e" * 64, "staged_config_sha256": "b" * 64},
        remove_fields=effects,
    )
    await advance(
        "staged",
        "activating",
        fields={
            "effect_kind": "activate_config",
            "effect_claim_id": "activate-effect",
            "effect_executor_id": "worker",
        },
    )
    await advance(
        "activating",
        "activated",
        remove_fields=effects,
    )
    await advance(
        "activated",
        "start_requested",
        fields={
            "effect_kind": "start_service",
            "effect_claim_id": "start-effect",
            "effect_executor_id": "worker",
        },
    )
    await advance(
        "start_requested",
        "validating",
        remove_fields=effects,
    )
    acceptance = {
        "release_id": "candidate",
        "image": release.image,
        "task_arn": "arn:task",
        "task_definition_arn": "arn:target",
        "image_digest": release.image_digest,
        "build_id": release.build_id,
        "upstream_version": release.upstream_version,
        "fork_tag": release.fork_tag,
        "fork_commit": release.fork_commit,
        "schema_id": release.schema_id,
        "schema_sha256": release.schema_sha256,
        "shared_db_schema_version": release.shared_db_schema_version,
        "agent_db_schema_version": release.agent_db_schema_version,
        "config_capabilities": sorted(release.config_capabilities),
        "qualification_id": release.qualification_id,
        "evidence_id": release.evidence_id,
        "runtime_capabilities": sorted(release.runtime_capabilities),
        "qualification_capabilities": sorted(release.qualification_capabilities),
        "config_sha256": "b" * 64,
        "recovery_set_id": recovery_set["recovery_set_id"],
        "recovery_manifest_sha256": recovery_set["manifest_sha256"],
        "gateway": "accepted",
    }
    assert not await update_repo.checkpoint_owner_release(
        containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"],
        await update_repo.get_by_id("u", queued["update_id"], consistent=True),
        "worker",
        expected_phase="validating",
        next_phase="validating",
        fields={"acceptance": {**acceptance, "image_digest": f"sha256:{'f' * 64}"}},
    )
    await advance("validating", "validating", fields={"acceptance": acceptance})

    prepared_container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    prepared_update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert await update_repo.finish_owner_release(
        prepared_container,
        prepared_update,
        "worker",
        expected_phase="validating",
    )
    finished_container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    finished_update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert finished_container["desired_release_id"] == finished_container["active_release_id"] == "candidate"
    assert finished_container["release_revision"] == 2
    assert "release_update_id" not in finished_container
    assert "provision_claim_id" not in finished_container
    assert finished_update["status"] == "applied" and finished_update["phase"] == "healthy"
    assert finished_update["release_revision"] == 2


@pytest.mark.asyncio
async def test_release_checkpoint_refuses_missing_prepared_evidence(dynamodb_table):
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    assert await _reserve_owner_apply(update_repo, owner, queued)
    container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert not await update_repo.checkpoint_owner_release(
        container,
        update,
        "worker",
        expected_phase="reserved",
        next_phase="prepared",
        fields={},
    )
    assert containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"] == container
    assert await update_repo.get_by_id("u", queued["update_id"], consistent=True) == update


@pytest.mark.asyncio
async def test_release_takeover_moves_both_claims_atomically(dynamodb_table):
    from core.dynamodb import get_table
    from core.repositories import update_repo

    containers, owner, queued = await _seed_owner_apply(update_repo)
    assert await _reserve_owner_apply(update_repo, owner, queued)
    container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert await update_repo.checkpoint_owner_release(
        container,
        update,
        "worker",
        expected_phase="reserved",
        next_phase="stop_requested",
        fields={
            "effect_kind": "stop_service",
            "effect_claim_id": "stale-effect",
            "effect_executor_id": "worker",
            "pre_stop_task_arns": ["arn:old-task"],
            "pre_stop_captured_at": "2026-09-09T11:59:00Z",
            "source": {
                "service_arn": "arn:service",
                "task_arn": "arn:old-task",
                "task_definition_arn": _OWNER_APPLY_ARN,
                "image_digest": f"sha256:{'9' * 64}",
                "build_id": "source-build",
                "access_point_id": "fsap-source",
                "file_system_id": "fs-source",
                "efs_root": "/users/u",
                "shared_schema_version": 15,
                "agent_schema_version": 19,
            },
        },
    )
    old = 1
    containers.update_item(
        Key={"owner_id": "u"},
        UpdateExpression="SET provision_claimed_at = :old",
        ExpressionAttributeValues={":old": old},
    )
    get_table("pending-updates").update_item(
        Key={"owner_id": "u", "update_id": queued["update_id"]},
        UpdateExpression="SET apply_claimed_at = :old",
        ExpressionAttributeValues={":old": old},
    )
    container = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    update = await update_repo.get_by_id("u", queued["update_id"], consistent=True)

    assert await update_repo.takeover_owner_release(
        container,
        update,
        "successor",
        stale_before_epoch=2,
    )
    assert containers.get_item(Key={"owner_id": "u"})["Item"]["provision_claim_id"] == "successor"
    claimed = await update_repo.get_by_id("u", queued["update_id"], consistent=True)
    assert claimed["apply_claim_id"] == "successor"
    assert claimed["effect_executor_id"] == "successor"
    assert claimed["effect_claim_id"] != "stale-effect"


@pytest.mark.asyncio
async def test_ordinary_and_release_owner_claims_exclude_each_other(dynamodb_table):
    from core.dynamodb import get_table
    from core.repositories import update_repo

    containers = get_table("containers")
    owner = {"owner_id": "u", "id": "original", "status": "running", "task_definition_arn": _OWNER_APPLY_ARN}
    containers.put_item(Item=owner)
    ordinary = await update_repo.create("u", "config_patch", "ordinary", {"config_patch": {"x": 1}})
    ordinary = await update_repo.claim_apply("u", ordinary["update_id"], "ordinary-claim")
    assert ordinary is not None
    assert await update_repo.claim_ordinary_owner("u", ordinary["update_id"], "ordinary-claim")
    assert await update_repo.assert_ordinary_owner("u", ordinary["update_id"], "ordinary-claim")

    release = await update_repo.create("u", "release", "release", {"target_release_id": "candidate"})
    release = await update_repo.get_by_id("u", release["update_id"], consistent=True)
    marked_owner = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    assert await _reserve_owner_apply(update_repo, marked_owner, release) is False

    assert await update_repo.release_ordinary_owner("u", ordinary["update_id"], "ordinary-claim")
    assert await _reserve_owner_apply(update_repo, owner, release)

    second = await update_repo.create("u", "config_patch", "second", {"config_patch": {"y": 2}})
    second = await update_repo.claim_apply("u", second["update_id"], "second-claim")
    assert second is not None
    assert not await update_repo.claim_ordinary_owner("u", second["update_id"], "second-claim")


@pytest.mark.asyncio
async def test_stale_ordinary_finally_cannot_clear_a_successor_marker(dynamodb_table):
    from core.dynamodb import get_table
    from core.repositories import update_repo

    containers = get_table("containers")
    containers.put_item(Item={"owner_id": "u", "id": "container", "status": "running"})
    update = await update_repo.create("u", "config_patch", "ordinary", {"config_patch": {"x": 1}})
    claimed = await update_repo.claim_apply("u", update["update_id"], "stale-claim")
    assert claimed is not None
    assert await update_repo.claim_ordinary_owner("u", update["update_id"], "stale-claim")
    containers.update_item(
        Key={"owner_id": "u"},
        UpdateExpression="SET ordinary_update_claim_id = :successor",
        ExpressionAttributeValues={":successor": "successor-claim"},
    )

    assert not await update_repo.release_ordinary_owner("u", update["update_id"], "stale-claim")
    row = containers.get_item(Key={"owner_id": "u"}, ConsistentRead=True)["Item"]
    assert row["ordinary_update_id"] == update["update_id"]
    assert row["ordinary_update_claim_id"] == "successor-claim"
