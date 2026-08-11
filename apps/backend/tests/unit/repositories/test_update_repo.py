"""Tests for pending-updates DynamoDB repository."""

import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import boto3
import pytest
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
async def test_applying_transition_stamps_applying_at(dynamodb_table):
    """Transitioning a row to 'applying' records when it entered that state, so
    a crashed applier can later be detected as stale (CC-3)."""
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "image_update", "AP", {"a": 1})
    ok = await update_repo.set_status_conditional("user_1", item["update_id"], "applying", ["pending"])
    assert ok is True
    row = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": item["update_id"]})["Item"]
    assert row["status"] == "applying"
    assert "applying_at" in row


@pytest.mark.asyncio
async def test_fresh_applying_blocks_takeover(dynamodb_table):
    """A row that is freshly 'applying' (live roll in progress) must NOT be
    stealable even when a stale-takeover window is offered."""
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "image_update", "AP", {"a": 1})
    await update_repo.set_status_conditional("user_1", item["update_id"], "applying", ["pending"])
    # A second apply attempt offering a generous takeover window still loses,
    # because applying_at is fresh (well within the window).
    ok = await update_repo.set_status_conditional(
        "user_1", item["update_id"], "applying", ["pending", "scheduled"], stale_takeover_seconds=900
    )
    assert ok is False


@pytest.mark.asyncio
async def test_stale_applying_is_reclaimable(dynamodb_table):
    """A wedged 'applying' row (applying_at older than the takeover window) can
    be re-claimed, and re-stamps applying_at — otherwise the crashed applier's
    row 409s every future roll for the owner forever (CC-3)."""
    from core.repositories import update_repo

    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=update_repo.STALE_APPLYING_SECONDS + 120)).isoformat()
    dynamodb_table.put_item(
        Item={
            "owner_id": "user_1",
            "update_id": "wedged",
            "update_type": "image_update",
            "status": "applying",
            "applying_at": stale_at,
        }
    )
    ok = await update_repo.set_status_conditional(
        "user_1",
        "wedged",
        "applying",
        ["pending", "scheduled"],
        stale_takeover_seconds=update_repo.STALE_APPLYING_SECONDS,
    )
    assert ok is True
    row = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": "wedged"})["Item"]
    assert row["applying_at"] > stale_at  # re-stamped fresh


@pytest.mark.asyncio
async def test_get_in_flight_excludes_stale_and_legacy_applying(dynamodb_table):
    """The mutual-exclusion guard must ignore a crashed applier: a stale
    'applying' row (old applying_at) AND a pre-fix one (no applying_at) are both
    excluded, while pending and a FRESH applying row are still counted."""
    from core.repositories import update_repo

    await update_repo.create("user_1", "image_update", "P", {"a": 1})  # pending
    fresh = await update_repo.create("user_1", "image_update", "FRESH", {"b": 2})
    await update_repo.set_status_conditional("user_1", fresh["update_id"], "applying", ["pending"])
    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=update_repo.STALE_APPLYING_SECONDS + 120)).isoformat()
    dynamodb_table.put_item(
        Item={"owner_id": "user_1", "update_id": "stale", "status": "applying", "applying_at": stale_at}
    )
    dynamodb_table.put_item(
        Item={"owner_id": "user_1", "update_id": "legacy", "status": "applying"}  # no applying_at
    )

    results = await update_repo.get_in_flight("user_1")
    assert {r["description"] for r in results if "description" in r} == {"P", "FRESH"}
    assert all(r["update_id"] not in {"stale", "legacy"} for r in results)


@pytest.mark.asyncio
async def test_stale_applier_cannot_finalize_after_takeover(dynamodb_table):
    """P1 owner-fence: a paused (not dead) applier A whose stale claim was taken
    over by applier B must NOT be able to finalize the row B now owns. Every
    terminal write is fenced on the applying_at token, so A's mark_applied /
    applying->failed no-op once B re-stamped applying_at."""
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "image_update", "roll", {"a": 1})
    uid = item["update_id"]

    # Applier A claims, but its applying_at is already old (A ran long / paused).
    token_a = (datetime.now(timezone.utc) - timedelta(seconds=update_repo.STALE_APPLYING_SECONDS + 120)).isoformat()
    assert await update_repo.set_status_conditional(
        "user_1", uid, "applying", ["pending", "scheduled"], applying_token=token_a
    )

    # Applier B takes over the stale claim with its own fresh token.
    token_b = datetime.now(timezone.utc).isoformat()
    assert await update_repo.set_status_conditional(
        "user_1",
        uid,
        "applying",
        ["pending", "scheduled"],
        stale_takeover_seconds=update_repo.STALE_APPLYING_SECONDS,
        applying_token=token_b,
    )

    # A resumes and tries to finalize — both terminal writes must NO-OP.
    assert await update_repo.mark_applied("user_1", uid, applying_token=token_a) is False
    assert (
        await update_repo.set_status_conditional("user_1", uid, "failed", ["applying"], applying_token=token_a) is False
    )
    row = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": uid})["Item"]
    assert row["status"] == "applying"  # still B's row
    assert row["applying_at"] == token_b

    # B (the real owner) finalizes successfully.
    assert await update_repo.mark_applied("user_1", uid, applying_token=token_b) is True
    row = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": uid})["Item"]
    assert row["status"] == "applied"


@pytest.mark.asyncio
async def test_tokenless_mark_applied_still_works_from_pending(dynamodb_table):
    """The retry pass marks pending -> applied with NO token; the owner fence
    must not break that (pending/scheduled terms are never fenced)."""
    from core.repositories import update_repo

    item = await update_repo.create("user_1", "paperclip_provision", "retry", {"op": "archive_member"})
    assert await update_repo.mark_applied("user_1", item["update_id"]) is True
    row = dynamodb_table.get_item(Key={"owner_id": "user_1", "update_id": item["update_id"]})["Item"]
    assert row["status"] == "applied"
