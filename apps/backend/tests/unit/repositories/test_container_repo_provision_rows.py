"""Provision row writes: the dispatch's status projection, the error record,
``update_fields``' ``expected=`` condition (the CREATE_FAILED ARN clear), and
the conversion-marker writers. There is no lease: a RUNNING Step Functions
execution owns a provision, and every provision step is idempotent.

Exercised against a REAL DynamoDB engine (moto), not a hand mock: every
primitive here is a conditional write, and a mock would happily pass
expressions DynamoDB rejects.
"""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _row(table, owner_id):
    return table.get_item(Key={"owner_id": owner_id}).get("Item")


# ---------------------------------------------------------------------------
# mark_provisioning — dispatch's status projection (not a lease)


@pytest.mark.asyncio
async def test_mark_provisioning_creates_an_absent_row(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.mark_provisioning("org_new")

    row = _row(dynamodb_table, "org_new")
    assert row["status"] == "provisioning"
    assert row["substatus"] == "queued"
    assert row["id"] and row["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "stopped", "provisioning"])
async def test_mark_provisioning_flips_a_non_running_row_and_keeps_its_id(dynamodb_table, status):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_x", "status": status, "id": "row-1", "hook_token": "h"})
    await container_repo.mark_provisioning("org_x")

    row = _row(dynamodb_table, "org_x")
    assert row["status"] == "provisioning"
    assert row["id"] == "row-1"
    assert row["hook_token"] == "h"


@pytest.mark.asyncio
async def test_mark_provisioning_leaves_a_live_workspace_alone(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_live", "status": "running"})
    await container_repo.mark_provisioning("org_live")
    assert _row(dynamodb_table, "org_live")["status"] == "running"


@pytest.mark.asyncio
async def test_a_legacy_provider_switch_flag_no_longer_flips_a_running_row(dynamodb_table):
    """Task 8a: needs_provision is inert data on rows written before it was deleted."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_sw", "status": "running", "needs_provision": True})
    await container_repo.mark_provisioning("org_sw")
    assert _row(dynamodb_table, "org_sw")["status"] == "running"


# ---------------------------------------------------------------------------
# mark_provision_error — the failure record the status poll acts on


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [True, False])
async def test_mark_provision_error_records_cause_and_retryability(dynamodb_table, retryable):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_err", "status": "provisioning"})
    await container_repo.mark_provision_error("org_err", last_error="x" * 600, retryable=retryable)

    row = _row(dynamodb_table, "org_err")
    assert row["status"] == "error"
    assert row["substatus"] == "provision_failed"
    assert row["retryable"] is retryable
    assert row["last_error"] == "x" * 500


@pytest.mark.asyncio
async def test_mark_provision_error_never_overwrites_a_running_row(dynamodb_table):
    """A concurrent run that already succeeded wins; a failed reconcile of a
    live workspace leaves it live."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_ok", "status": "running"})
    await container_repo.mark_provision_error("org_ok", last_error="boom", retryable=True)
    row = _row(dynamodb_table, "org_ok")
    assert row["status"] == "running"
    assert "last_error" not in row


@pytest.mark.asyncio
async def test_mark_provision_error_noops_on_an_absent_row(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.mark_provision_error("org_gone", last_error="boom", retryable=True)
    assert _row(dynamodb_table, "org_gone") is None


# ---------------------------------------------------------------------------
# update_fields(expected=...) — the #14 CREATE_FAILED ARN clear's condition


@pytest.mark.asyncio
async def test_update_fields_expected_value_mismatch_writes_nothing(dynamodb_table):
    """A write conditioned on a field's current value (clearing
    agentcore_runtime_arn only if it still names the arn this run determined
    was dead) must not clobber a value a concurrent run has since changed."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_exp", "status": "provisioning", "agentcore_runtime_arn": "arn:new"})

    assert (
        await container_repo.update_fields(
            "org_exp", {"agentcore_runtime_arn": None}, expected={"agentcore_runtime_arn": "arn:dead"}
        )
        is None
    )
    assert _row(dynamodb_table, "org_exp")["agentcore_runtime_arn"] == "arn:new"

    # Matching expectation succeeds.
    await container_repo.update_fields(
        "org_exp", {"agentcore_runtime_arn": None}, expected={"agentcore_runtime_arn": "arn:new"}
    )
    assert _row(dynamodb_table, "org_exp")["agentcore_runtime_arn"] is None


@pytest.mark.asyncio
async def test_update_fields_expected_none_matches_an_absent_field(dynamodb_table):
    """ "No value yet" must match a field that was NEVER written (absent), not
    only one explicitly cleared to null."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_fresh", "status": "provisioning"})
    await container_repo.update_fields(
        "org_fresh", {"agentcore_runtime_arn": "arn:new"}, expected={"agentcore_runtime_arn": None}
    )
    assert _row(dynamodb_table, "org_fresh")["agentcore_runtime_arn"] == "arn:new"


@pytest.mark.asyncio
async def test_update_fields_expected_none_also_matches_an_explicit_null(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_cleared", "status": "provisioning", "agentcore_runtime_arn": None})
    await container_repo.update_fields(
        "org_cleared", {"agentcore_runtime_arn": "arn:fresh"}, expected={"agentcore_runtime_arn": None}
    )
    assert _row(dynamodb_table, "org_cleared")["agentcore_runtime_arn"] == "arn:fresh"


# ---------------------------------------------------------------------------
# update_conversion_fields — the allowlisted marker writer


@pytest.mark.asyncio
async def test_update_conversion_fields_never_touches_status(dynamodb_table):
    """The incident interleaving: dispatch's provisioning write lands first,
    the marker write second. The marker write must leave status intact."""
    from core.repositories import container_repo

    await container_repo.mark_provisioning("org_conv")
    await container_repo.update_conversion_fields("org_conv", {"conversion_pending": False, "substatus": None})

    row = _row(dynamodb_table, "org_conv")
    assert row["status"] == "provisioning", "marker write must never touch status"
    assert row["conversion_pending"] is False
    assert row.get("substatus") is None  # written as NULL


@pytest.mark.asyncio
async def test_update_conversion_fields_writes_the_stamp_fields(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_stamp", "status": "running"})
    await container_repo.update_conversion_fields(
        "user_stamp",
        {"conversion_pending": True, "substatus": "converting", "conversion_started_at": "2026-07-24T00:00:00+00:00"},
    )

    row = _row(dynamodb_table, "user_stamp")
    assert row["conversion_pending"] is True
    assert row["substatus"] == "converting"
    assert row["conversion_started_at"] == "2026-07-24T00:00:00+00:00"
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_update_conversion_fields_rejects_non_marker_fields(dynamodb_table):
    """The allowlist is the guarantee: this helper must never grow into a
    general row writer (that is exactly the clobber class it replaces)."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "user_allow", "status": "running"})
    with pytest.raises(ValueError, match="disallowed fields"):
        await container_repo.update_conversion_fields("user_allow", {"status": "stopped"})
    assert _row(dynamodb_table, "user_allow")["status"] == "running"


@pytest.mark.asyncio
async def test_update_conversion_fields_noops_on_an_absent_row(dynamodb_table):
    """A concurrently deleted row must not be resurrected as a marker-only
    ghost (the same attribute_exists condition every scoped stamp uses)."""
    from core.repositories import container_repo

    await container_repo.update_conversion_fields("user_ghost", {"conversion_pending": True})
    assert _row(dynamodb_table, "user_ghost") is None


# ---------------------------------------------------------------------------
# clear_conversion_deferral: the re-arm CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_conversion_deferral_clears_only_the_deferral_it_observed(dynamodb_table):
    """A re-arm drops the self-heal anchor + deferral marker ONLY while the row
    still carries the deferral it read; pending and status are untouched."""
    from core.repositories import container_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_defer",
            "status": "running",
            "conversion_pending": True,
            "conversion_started_at": "2026-07-24T00:00:00+00:00",
            "conversion_deferred_reason": "oauth_required",
        }
    )
    assert await container_repo.clear_conversion_deferral("user_defer", deferred_reason="oauth_required") is True
    row = _row(dynamodb_table, "user_defer")
    assert row["conversion_started_at"] is None
    assert row["conversion_deferred_reason"] is None
    assert row["conversion_pending"] is True
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_clear_conversion_deferral_refuses_when_a_fresh_dispatch_cleared_the_marker(dynamodb_table):
    """The race the CAS exists for: a fresh stamp cleared the marker (and
    re-armed the guard over a real stop + drain) between the re-arm's read and
    write. The write must refuse and leave the fresh anchor alone."""
    from core.repositories import container_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_fresh",
            "status": "running",
            "conversion_pending": True,
            "conversion_started_at": "2026-07-24T00:05:00+00:00",
            "conversion_deferred_reason": None,
        }
    )
    assert await container_repo.clear_conversion_deferral("user_fresh", deferred_reason="oauth_required") is False
    row = _row(dynamodb_table, "user_fresh")
    assert row["conversion_started_at"] == "2026-07-24T00:05:00+00:00"

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_other",
            "status": "running",
            "conversion_pending": False,
            "conversion_started_at": "2026-07-24T00:05:00+00:00",
            "conversion_deferred_reason": "oauth_required",
        }
    )
    assert await container_repo.clear_conversion_deferral("user_other", deferred_reason="oauth_required") is False
    assert await container_repo.clear_conversion_deferral("user_missing", deferred_reason="oauth_required") is False
