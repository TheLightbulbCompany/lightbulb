"""Shared fixtures for service-level tests.

Most service tests mock at the boto3 call boundary (MagicMock the
table accessor). A few tests exercise multi-step flows where the
behavior under test depends on real DynamoDB semantics — conditional
puts, query ordering by sort key, GSI projection, slug-reservation
rows. For those, mocking would either hardcode the responses (which
defeats the point) or grow a re-implementation of the conditional-put
semantics.

This file owns the moto-backed fixtures + small helpers used across
test_marketplace_service and test_takedown_service so the helpers
don't drift between files. New moto-backed service tests should pull
from here.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from core.services import marketplace_service  # noqa: E402


@pytest.fixture
def dynamodb_table():
    """Moto-backed listings table with the prod schema (HASH listing_id +
    RANGE version) and the slug-version GSI that get_by_slug needs.

    Also stubs out the S3 artifact upload so create_draft can run without
    a real bucket — the actual S3 path is exercised by the catalog tests.

    Yields the listings table object so tests can do direct mutations
    (e.g. flipping artifact_uploaded=True without going through
    replace_artifact's S3 round-trip).
    """
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        listings = client.create_table(
            TableName="test-marketplace-listings",
            KeySchema=[
                {"AttributeName": "listing_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "listing_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
                {"AttributeName": "slug", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "slug-version-index",
                    "KeySchema": [
                        {"AttributeName": "slug", "KeyType": "HASH"},
                        {"AttributeName": "version", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        listings.meta.client.get_waiter("table_exists").wait(TableName="test-marketplace-listings")

        # Purchases + takedowns tables — needed by takedown_service tests
        # but cheap to build alongside listings. The purchases table
        # carries the listing-created-index GSI so the cascade's
        # paginated query reaches it; the takedowns table is keyed by
        # (listing_id, takedown_id).
        client.create_table(
            TableName="test-marketplace-purchases",
            KeySchema=[
                {"AttributeName": "buyer_id", "KeyType": "HASH"},
                {"AttributeName": "purchase_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "buyer_id", "AttributeType": "S"},
                {"AttributeName": "purchase_id", "AttributeType": "S"},
                {"AttributeName": "listing_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "listing-created-index",
                    "KeySchema": [
                        {"AttributeName": "listing_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        ).meta.client.get_waiter("table_exists").wait(TableName="test-marketplace-purchases")

        client.create_table(
            TableName="test-marketplace-takedowns",
            KeySchema=[
                {"AttributeName": "listing_id", "KeyType": "HASH"},
                {"AttributeName": "takedown_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "listing_id", "AttributeType": "S"},
                {"AttributeName": "takedown_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        ).meta.client.get_waiter("table_exists").wait(TableName="test-marketplace-takedowns")

        # Wire the marketplace_service module to point at the moto tables.
        # The takedown service reads its table names from the same
        # settings object, so a single patch covers both. We patch the
        # imported settings reference each service holds.
        from core.services import takedown_service

        with (
            patch.object(
                marketplace_service.settings,
                "MARKETPLACE_LISTINGS_TABLE",
                "test-marketplace-listings",
            ),
            patch.object(
                marketplace_service.settings,
                "MARKETPLACE_ARTIFACTS_BUCKET",
                "test-bucket",
            ),
            patch.object(
                takedown_service.settings,
                "MARKETPLACE_LISTINGS_TABLE",
                "test-marketplace-listings",
            ),
            patch.object(
                takedown_service.settings,
                "MARKETPLACE_PURCHASES_TABLE",
                "test-marketplace-purchases",
            ),
            patch.object(
                takedown_service.settings,
                "MARKETPLACE_TAKEDOWNS_TABLE",
                "test-marketplace-takedowns",
            ),
            patch(
                "core.services.marketplace_service._upload_artifact_to_s3",
                new=AsyncMock(return_value=("listings/x/v1/", "sha-stub")),
            ),
        ):
            yield listings


async def _create_and_publish(
    table,
    listing_id: str,
    seller_id: str,
    slug: str,
    *,
    name: str = "X",
    description_md: str = "d",
    price_cents: int = 0,
    tags: list[str] | None = None,
    category: str | None = "agent",
) -> dict:
    """Create a v=1 draft, mark its artifact uploaded, submit, approve.

    Mirrors the real flow because downstream guards depend on the row
    carrying real status transitions — synthesizing a 'published' row
    via raw put_item would bypass artifact_uploaded and skip the
    pieces a real publish writes (published_at, published_by).

    `listing_id` is accepted for caller-side documentation but is
    ignored: create_draft generates its own UUID.
    """
    draft = await marketplace_service.create_draft(
        seller_id=seller_id,
        slug=slug,
        name=name,
        description_md=description_md,
        format="openclaw_agent",
        price_cents=price_cents,
        tags=tags or [],
        category=category,
        artifact_bytes=b"",
        manifest={"name": name},
    )
    # create_draft writes artifact_uploaded=False; submit_for_review's
    # guard rejects that. Flip it so the test can move through the state
    # machine end to end.
    table.update_item(
        Key={"listing_id": draft["listing_id"], "version": 1},
        UpdateExpression="SET artifact_uploaded = :t",
        ExpressionAttributeValues={":t": True},
    )
    await marketplace_service.submit_for_review(listing_id=draft["listing_id"], seller_id=seller_id, version=1)
    await marketplace_service.approve(listing_id=draft["listing_id"], version=1, approved_by="admin_test")
    return draft


def _upload_dummy_artifact(table, listing_id: str, version: int) -> None:
    """Flip artifact_uploaded=True on the row so submit_for_review accepts
    it. The real flow runs through replace_artifact + S3; this is a test
    short-circuit because replace_artifact also writes S3 (mocked away by
    the fixture) and we don't need the artifact bytes for these tests.
    Sync because moto's put/update are sync; called from async tests is
    fine since boto3 itself is sync."""
    table.update_item(
        Key={"listing_id": listing_id, "version": version},
        UpdateExpression="SET artifact_uploaded = :t",
        ExpressionAttributeValues={":t": True},
    )
