"""company_slug_repo: atomic slug reservation via conditional put."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def slug_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-company-slugs",
            KeySchema=[{"AttributeName": "slug", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "slug", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-company-slugs")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_reserve_claims_a_free_slug(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="Acme", owner_id="user_1") is True

    row = await company_slug_repo.get_by_slug("acme")
    assert row is not None
    assert row["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_reserve_rejects_a_slug_held_by_another_owner(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True
    assert await company_slug_repo.reserve(slug="acme", owner_id="user_2") is False

    row = await company_slug_repo.get_by_slug("acme")
    assert row["owner_id"] == "user_1"  # original holder untouched


@pytest.mark.asyncio
async def test_reserve_is_idempotent_for_the_same_owner(slug_table):
    """Provisioning can re-run; re-claiming your own slug is a success."""
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True
    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True


@pytest.mark.asyncio
async def test_get_by_slug_missing_returns_none(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.get_by_slug("nobody") is None


@pytest.mark.asyncio
async def test_get_by_slug_is_case_insensitive_on_read(slug_table):
    """Stored slug is already lowercase; the query casing must still normalize.

    Regression guard for dropping `normalize()` from `get_by_slug`'s
    `Key=` construction -- a mutation that every other test here misses
    because they all query with an already-lowercase slug.
    """
    from core.repositories import company_slug_repo

    await company_slug_repo.reserve(slug="acme", owner_id="user_1")

    row = await company_slug_repo.get_by_slug("AcMe")
    assert row is not None
    assert row["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_get_by_owner_returns_the_owners_slug(slug_table):
    from core.repositories import company_slug_repo

    await company_slug_repo.reserve(slug="acme", owner_id="user_1")
    await company_slug_repo.reserve(slug="other", owner_id="user_2")

    row = await company_slug_repo.get_by_owner("user_1")
    assert row is not None
    assert row["slug"] == "acme"
    assert await company_slug_repo.get_by_owner("user_missing") is None


# -- holds taken at "Create your workspace", handed to the org by organization.created ---


@pytest.mark.asyncio
async def test_a_hold_is_exclusive_until_it_goes_stale(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.hold(slug="bench-club", user_id="user_1") is True
    assert await company_slug_repo.hold(slug="bench-club", user_id="user_1") is True  # re-hold is fine
    assert await company_slug_repo.hold(slug="bench-club", user_id="user_2") is False
    assert await company_slug_repo.reserve(slug="bench-club", owner_id="org_x") is False

    slug_table.update_item(
        Key={"slug": "bench-club"}, UpdateExpression="SET held_at = :z", ExpressionAttributeValues={":z": 0}
    )
    assert await company_slug_repo.hold(slug="bench-club", user_id="user_2") is True


@pytest.mark.asyncio
async def test_a_hold_never_takes_a_workspaces_slug(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="acme", owner_id="org_1") is True
    assert await company_slug_repo.hold(slug="acme", user_id="user_1") is False


@pytest.mark.asyncio
async def test_claim_hands_the_newest_hold_to_the_org_once(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.hold(slug="old-pick", user_id="user_1") is True
    slug_table.update_item(
        Key={"slug": "old-pick"}, UpdateExpression="SET held_at = :t", ExpressionAttributeValues={":t": 1}
    )
    assert await company_slug_repo.hold(slug="bench-club", user_id="user_1") is True

    assert await company_slug_repo.claim_hold(user_id="user_1", owner_id="org_1") == "bench-club"
    row = await company_slug_repo.get_by_slug("bench-club")
    assert row["owner_id"] == "org_1" and "held_at" not in row
    # The workspace's slug is now the one allocate_company_slug returns, and a replay is a no-op.
    assert (await company_slug_repo.get_by_owner("org_1"))["slug"] == "bench-club"
    assert await company_slug_repo.claim_hold(user_id="user_2", owner_id="org_2") is None
