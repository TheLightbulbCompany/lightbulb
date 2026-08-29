import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def integrations_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
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
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


def _res(rid="res_1", status="active"):
    return {
        "resource_id": rid,
        "provider": "posthog",
        "service": "analytics",
        "status": status,
        "resource_name": "Northwind analytics",
        "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
        "dashboard_url": None,
        "level": "company",
    }


@pytest.mark.asyncio
async def test_record_creates_the_row_with_the_projects_actor(integrations_table):
    from core.repositories import integrations_repo

    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    assert out == [_res()]
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})["Item"]
    assert item["actor"] == "projects"
    assert item["status"] == "connected"
    assert item["connected_at"]


@pytest.mark.asyncio
async def test_record_appends_and_is_idempotent_per_resource_id(integrations_table):
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1", status="paused"))
    assert [r["resource_id"] for r in out] == ["res_1", "res_2"]
    assert out[0]["status"] == "paused"


@pytest.mark.asyncio
async def test_record_preserves_a_pre_existing_status_row(integrations_table):
    """put_status is a full-replace put_item; these writers must not be, or an
    identity or a Gmail watch binding is silently dropped."""
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_a", "posthog", "connected", "founder@example.com")
    await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})["Item"]
    assert item["identity"] == "founder@example.com"
    assert len(item["provider_resources"]) == 1


@pytest.mark.asyncio
async def test_record_rejects_a_resource_with_no_id(integrations_table):
    from core.repositories import integrations_repo

    with pytest.raises(ValueError):
        await integrations_repo.record_provider_resource("org_a", "posthog", {"provider": "posthog"})


@pytest.mark.asyncio
async def test_list_and_remove(integrations_table):
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    assert len(await integrations_repo.list_provider_resources("org_a", "posthog")) == 2
    left = await integrations_repo.remove_provider_resource("org_a", "posthog", "res_1")
    assert [r["resource_id"] for r in left] == ["res_2"]
    # Removing something that is not there is a no-op, not an error.
    assert await integrations_repo.remove_provider_resource("org_a", "posthog", "res_nope") == left
    assert await integrations_repo.list_provider_resources("org_b", "posthog") == []
