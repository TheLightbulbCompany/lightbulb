"""The Meta-user-id lookup, against a real (moto) DynamoDB.

Deliberately not mocked. This lookup rides a real paginated table scan, and the
failure modes that matter — an empty needle matching legacy rows, a match on a
later page being missed — are ones a MagicMock table happily returns a value
for. The same class of bug has shipped here before, so the round trip is
exercised for real.
"""

import boto3
import pytest
from moto import mock_aws

from core.dynamodb import table_name
from core.repositories import integrations_repo


@pytest.fixture
def integrations_table():
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=table_name("integrations"),
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
        yield


@pytest.mark.asyncio
async def test_put_status_persists_the_provider_user_id(integrations_table):
    await integrations_repo.put_status("org_1", "instagram", "connected", provider_user_id="1784100")
    row = await integrations_repo.get("org_1", "instagram")
    assert row["provider_user_id"] == "1784100"


@pytest.mark.asyncio
async def test_put_status_omits_the_field_when_absent(integrations_table):
    """Providers with no provider-side id must not get an empty attribute —
    an empty string would match a lookup for the empty id."""
    await integrations_repo.put_status("org_1", "stripe", "connected")
    row = await integrations_repo.get("org_1", "stripe")
    assert "provider_user_id" not in row


@pytest.mark.asyncio
async def test_finds_the_owner_who_owns_that_meta_id(integrations_table):
    await integrations_repo.put_status("org_a", "instagram", provider_user_id="111")
    await integrations_repo.put_status("org_b", "instagram", provider_user_id="222")
    found = await integrations_repo.find_by_provider_user_id("instagram", "222")
    assert found["owner_id"] == "org_b"


@pytest.mark.asyncio
async def test_does_not_cross_providers(integrations_table):
    """Meta app-scoped ids are per-app, so the same digits can legitimately
    belong to different owners on instagram vs meta_ads. Matching the id alone
    would revoke the wrong owner's connection."""
    await integrations_repo.put_status("org_a", "instagram", provider_user_id="777")
    await integrations_repo.put_status("org_b", "meta_ads", provider_user_id="777")
    assert (await integrations_repo.find_by_provider_user_id("meta_ads", "777"))["owner_id"] == "org_b"
    assert (await integrations_repo.find_by_provider_user_id("instagram", "777"))["owner_id"] == "org_a"


@pytest.mark.asyncio
async def test_unknown_id_returns_none(integrations_table):
    await integrations_repo.put_status("org_a", "instagram", provider_user_id="111")
    assert await integrations_repo.find_by_provider_user_id("instagram", "999") is None


@pytest.mark.asyncio
async def test_rows_without_the_id_never_match(integrations_table):
    """A connection made before we captured the id must not be matched by an
    empty/absent value — that would tear down an arbitrary owner."""
    await integrations_repo.put_status("org_legacy", "instagram")
    assert await integrations_repo.find_by_provider_user_id("instagram", "") is None
    assert await integrations_repo.find_by_provider_user_id("instagram", "111") is None


@pytest.mark.asyncio
async def test_matches_across_a_paginated_scan(integrations_table):
    """The scan pages; a match on a later page must still be found."""
    for i in range(30):
        await integrations_repo.put_status(f"org_{i}", "instagram", provider_user_id=str(1000 + i))
    found = await integrations_repo.find_by_provider_user_id("instagram", "1029")
    assert found["owner_id"] == "org_29"
