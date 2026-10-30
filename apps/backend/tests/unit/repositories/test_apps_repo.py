import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def apps_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-apps",
            KeySchema=[{"AttributeName": "app_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "app_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


@pytest.mark.asyncio
async def test_get_and_patch(apps_table):
    from core.repositories import apps_repo

    apps_table.put_item(Item={"app_id": "app_1", "name": "crew-schedule", "stripe_project_id": None})
    assert await apps_repo.get("app_missing") is None
    row = await apps_repo.patch("app_1", {"state": "live"})
    assert row["state"] == "live" and row["name"] == "crew-schedule" and row["updated_at"]
    assert (await apps_repo.get("app_1"))["state"] == "live"


@pytest.mark.asyncio
async def test_stripe_project_id_is_set_exactly_once(apps_table):
    """Two workers that both minted a Project: the first store wins, the
    second learns it lost and re-reads the winner."""
    from core.repositories import apps_repo

    # Task 7's create stores the field as None, so None counts as unset.
    apps_table.put_item(Item={"app_id": "app_1", "name": "crew-schedule", "stripe_project_id": None})
    assert await apps_repo.set_stripe_project_id("app_1", "proj_a") is True
    assert await apps_repo.set_stripe_project_id("app_1", "proj_b") is False
    assert (await apps_repo.get("app_1"))["stripe_project_id"] == "proj_a"
    # No record at all is a loss too, never an upsert.
    assert await apps_repo.set_stripe_project_id("app_missing", "proj_c") is False
    assert await apps_repo.get("app_missing") is None
