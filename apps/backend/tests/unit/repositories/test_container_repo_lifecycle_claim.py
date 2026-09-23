"""The owner-lifecycle lease: one credential mutation in flight per owner, across TASKS.

WHY THIS EXISTS AS A DYNAMODB WRITE. The guard used to be an EFS file lock under
``settings.EFS_MOUNT_PATH``, which worked only because every backend task mounted the same
filesystem. Deleting the ECS runtime deletes that mount, and ``fcntl.lockf`` on an ephemeral
rootfs succeeds independently in each of the 2-4 tasks -- so the guard would have kept passing
while fencing nothing, silently, on the path that writes an owner's credentials.

Exercised against a REAL DynamoDB engine (moto), not a hand mock: the whole mechanism is a
conditional write, and a MagicMock would accept a condition expression DynamoDB rejects.
"""

import os
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
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
    from core.repositories import container_repo

    key = f"{container_repo.LIFECYCLE_KEY_PREFIX}{owner_id}"
    return table.get_item(Key={"owner_id": key}).get("Item") or {}


@pytest.mark.asyncio
async def test_two_callers_for_one_owner_serialize(dynamodb_table):
    from core.repositories import container_repo

    first = await container_repo.try_claim_owner_lifecycle("org_1")
    second = await container_repo.try_claim_owner_lifecycle("org_1")

    assert first is not None
    assert second is None, "a second caller must not enter the section while the first holds it"
    assert _row(dynamodb_table, "org_1")["tok"] == first


@pytest.mark.asyncio
async def test_different_owners_do_not_block_each_other(dynamodb_table):
    from core.repositories import container_repo

    assert await container_repo.try_claim_owner_lifecycle("org_1") is not None
    assert await container_repo.try_claim_owner_lifecycle("org_2") is not None


@pytest.mark.asyncio
async def test_release_frees_the_lease(dynamodb_table):
    from core.repositories import container_repo

    token = await container_repo.try_claim_owner_lifecycle("org_1")
    await container_repo.release_owner_lifecycle_claim("org_1", token=token)

    assert "tok" not in _row(dynamodb_table, "org_1")
    assert await container_repo.try_claim_owner_lifecycle("org_1") is not None


@pytest.mark.asyncio
async def test_a_stale_release_never_drops_the_successors_lease(dynamodb_table):
    """A caller that hung past the lease was taken over. Its own ``finally`` must not delete
    the live claim of whoever succeeded it -- that would let a THIRD caller in."""
    from core.repositories import container_repo

    stale = await container_repo.try_claim_owner_lifecycle("org_1")
    dynamodb_table.update_item(
        Key={"owner_id": f"{container_repo.LIFECYCLE_KEY_PREFIX}org_1"},
        UpdateExpression="SET claimed_at = :old",
        ExpressionAttributeValues={":old": int(time.time()) - container_repo.LIFECYCLE_CLAIM_STALE_S - 1},
    )
    successor = await container_repo.try_claim_owner_lifecycle("org_1")
    assert successor is not None and successor != stale

    await container_repo.release_owner_lifecycle_claim("org_1", token=stale)

    assert _row(dynamodb_table, "org_1")["tok"] == successor


@pytest.mark.asyncio
async def test_a_ddb_fault_refuses_rather_than_admitting_everyone(dynamodb_table):
    """FAIL CLOSED. A fault that admitted every caller would interleave two writers on one
    owner's credentials exactly when DynamoDB is already unhappy."""
    from core.repositories import container_repo

    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem"
    )
    with patch.object(container_repo, "_get_table", return_value=table):
        assert await container_repo.try_claim_owner_lifecycle("org_1") is None


@pytest.mark.asyncio
async def test_a_lease_row_is_never_mistaken_for_a_workspace(dynamodb_table):
    """It rides the containers table on its own reserved key, so every fleet consumer -- the
    operator console's grid, the runtime rolls -- would otherwise hand
    a lease row around as somebody's workspace."""
    from core.repositories import container_repo

    await container_repo.try_claim_owner_lifecycle("org_1")
    dynamodb_table.put_item(Item={"owner_id": "org_real", "status": "running"})

    rows = await container_repo.scan_all()

    assert [r["owner_id"] for r in rows] == ["org_real"]
    assert container_repo.LIFECYCLE_KEY_PREFIX.startswith(container_repo.RESERVED_KEY_PREFIX)


@pytest.mark.asyncio
async def test_an_owner_with_no_container_row_can_still_be_fenced(dynamodb_table):
    """A connect can precede provisioning, so the lease rides its OWN reserved-key row rather
    than the owner's containers row -- which does not exist yet for a first-time owner."""
    from core.repositories import container_repo

    assert dynamodb_table.get_item(Key={"owner_id": "org_brand_new"}).get("Item") is None

    assert await container_repo.try_claim_owner_lifecycle("org_brand_new") is not None
    assert await container_repo.try_claim_owner_lifecycle("org_brand_new") is None
