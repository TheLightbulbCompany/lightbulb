"""The owner's bare `slack` row carries every installed team id, so revoke-all can read the
installs strongly instead of from the eventually consistent by-owner GSI."""

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
            TableName="test-integrations",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "provider", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "provider", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-integrations")
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", client):
            yield table


@pytest.mark.asyncio
async def test_each_install_adds_its_team_and_keeps_the_others(dynamodb_table):
    from core.repositories import integrations_repo
    from core.services import slack_install_service

    await slack_install_service._mark_connected("org_1", "T1")
    await slack_install_service._mark_connected("org_1", "T2")
    rows = await integrations_repo.list_for_owner("org_1", consistent=True)
    assert [(r["provider"], r["status"], set(r["team_ids"])) for r in rows] == [("slack", "connected", {"T1", "T2"})]

    await integrations_repo.remove_connected_team("org_1", "slack", "T1")
    rows = await integrations_repo.list_for_owner("org_1", consistent=True)
    assert set(rows[0]["team_ids"]) == {"T2"}


@pytest.mark.asyncio
async def test_forgetting_a_team_never_creates_a_row(dynamodb_table):
    from core.repositories import integrations_repo

    await integrations_repo.remove_connected_team("org_1", "slack", "T1")
    assert await integrations_repo.list_for_owner("org_1", consistent=True) == []
