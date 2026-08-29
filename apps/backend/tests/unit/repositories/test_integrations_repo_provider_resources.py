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
async def test_record_creates_a_NAMESPACED_row_with_the_projects_actor(integrations_table):
    from core.repositories import integrations_repo

    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    assert out == [_res()]
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "projects/posthog"})["Item"]
    assert item["actor"] == "projects"
    assert item["status"] == "connected"
    assert item["connected_at"]
    # The bare key belongs to the OAuth rail and must stay untouched.
    assert "Item" not in integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})


@pytest.mark.asyncio
async def test_record_appends_and_is_idempotent_per_resource_id(integrations_table):
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1", status="paused"))
    assert [r["resource_id"] for r in out] == ["res_1", "res_2"]
    assert out[0]["status"] == "paused"


@pytest.mark.asyncio
async def test_record_never_touches_the_oauth_rails_row_for_the_same_provider(integrations_table):
    """`posthog` is a registry provider AND a Projects provider. The OAuth row
    must survive a landing untouched — sharing the key made put_status's
    full-replace put_item clobber provider_resources, and made every OAuth
    reader treat the provider as connected."""
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_a", "posthog", "connected", "founder@example.com")
    await integrations_repo.record_provider_resource("org_a", "posthog", _res())

    oauth = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})["Item"]
    assert oauth["identity"] == "founder@example.com"
    assert "provider_resources" not in oauth
    assert "actor" not in oauth

    ours = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "projects/posthog"})["Item"]
    assert len(ours["provider_resources"]) == 1


@pytest.mark.asyncio
async def test_write_preserves_its_own_rows_other_attributes(integrations_table):
    """UpdateExpression, not put_item: a second landing must not blow away
    anything already on OUR row."""
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    integrations_table.update_item(
        Key={"owner_id": "org_a", "provider": "projects/posthog"},
        UpdateExpression="SET note = :n",
        ExpressionAttributeValues={":n": "keep me"},
    )
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "projects/posthog"})["Item"]
    assert item["note"] == "keep me"
    assert len(item["provider_resources"]) == 2


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


@pytest.mark.asyncio
async def test_the_wipe_sweeps_the_namespaced_row(integrations_table):
    """`delete_all_for_owner` lists rows and deletes each `row["provider"]`, so
    the namespaced value flows through unchanged — the wipe needs no prefix
    handling of its own."""
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_a", "linear")
    await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    assert sorted(await integrations_repo.delete_all_for_owner("org_a")) == ["linear", "projects/posthog"]
    assert await integrations_repo.list_for_owner("org_a") == []
