"""Provision row writes: the dispatch's status projection, the error record,
``update_fields``' ``expected_field`` condition (the CREATE_FAILED ARN clear), and
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


@pytest.mark.asyncio
async def test_note_provision_error_records_one_line_reason_without_touching_status(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_n", "status": "provisioning"})
    await container_repo.note_provision_error("org_n", "boom\n  second   line" + "x" * 600)
    row = _row(dynamodb_table, "org_n")
    assert row["status"] == "provisioning"
    assert row["last_error"].startswith("boom second line") and len(row["last_error"]) == 500
    assert row["last_error_at"]


@pytest.mark.asyncio
async def test_note_provision_error_skips_running_and_absent_rows(dynamodb_table):
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_r", "status": "running"})
    await container_repo.note_provision_error("org_r", "boom")
    await container_repo.note_provision_error("org_gone", "boom")
    assert "last_error" not in _row(dynamodb_table, "org_r")
    assert _row(dynamodb_table, "org_gone") is None


# ---------------------------------------------------------------------------
# update_fields(expected_field=...) — the #14 CREATE_FAILED ARN clear's condition


@pytest.mark.asyncio
async def test_update_fields_expected_value_mismatch_writes_nothing(dynamodb_table):
    """A write conditioned on a field's current value (clearing
    agentcore_runtime_arn only if it still names the arn this run determined
    was dead) must not clobber a value a concurrent run has since changed."""
    from core.repositories import container_repo

    dynamodb_table.put_item(Item={"owner_id": "org_exp", "status": "provisioning", "agentcore_runtime_arn": "arn:new"})

    assert (
        await container_repo.update_fields(
            "org_exp",
            {"agentcore_runtime_arn": None},
            expected_field="agentcore_runtime_arn",
            expected_value="arn:dead",
        )
        is None
    )
    assert _row(dynamodb_table, "org_exp")["agentcore_runtime_arn"] == "arn:new"

    # Matching expectation succeeds.
    await container_repo.update_fields(
        "org_exp", {"agentcore_runtime_arn": None}, expected_field="agentcore_runtime_arn", expected_value="arn:new"
    )
    assert _row(dynamodb_table, "org_exp")["agentcore_runtime_arn"] is None
