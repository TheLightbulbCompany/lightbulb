"""Tests for the marketplace DynamoDB repository (moto-backed).

Covers the money-path invariants that must survive the repo seam:
transaction atomicity (publish flip, purchase fulfillment), set-once
conditional writes, never-negative balance decrements, pagination loops,
the v0 sentinel exclusion, moderation-GSI visibility, and the sellers
missing-table tolerance.
"""

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.repositories import marketplace_repo  # noqa: E402


NOW = "2026-07-17T00:00:00Z"

_LISTINGS = "test-marketplace-listings"
_PURCHASES = "test-marketplace-purchases"
_PAYOUTS = "test-marketplace-payout-accounts"
_TAKEDOWNS = "test-marketplace-takedowns"
# Deliberately never created: the sellers getters must tolerate a missing
# table (ResourceNotFoundException -> None), e.g. early after a CDK deploy.
_SELLERS = "test-marketplace-sellers-missing"


@pytest.fixture
def marketplace_tables():
    """Moto tables with the prod key schemas + the GSIs the repo queries."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        listings = resource.create_table(
            TableName=_LISTINGS,
            KeySchema=[
                {"AttributeName": "listing_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "listing_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
                {"AttributeName": "seller_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "published_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "seller-created-index",
                    "KeySchema": [
                        {"AttributeName": "seller_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-published-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "published_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        purchases = resource.create_table(
            TableName=_PURCHASES,
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
        )
        payouts = resource.create_table(
            TableName=_PAYOUTS,
            KeySchema=[{"AttributeName": "seller_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "seller_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for name in (_LISTINGS, _PURCHASES, _PAYOUTS):
            resource.meta.client.get_waiter("table_exists").wait(TableName=name)

        with (
            patch.object(settings, "MARKETPLACE_LISTINGS_TABLE", _LISTINGS),
            patch.object(settings, "MARKETPLACE_PURCHASES_TABLE", _PURCHASES),
            patch.object(settings, "MARKETPLACE_PAYOUT_ACCOUNTS_TABLE", _PAYOUTS),
            patch.object(settings, "MARKETPLACE_TAKEDOWNS_TABLE", _TAKEDOWNS),
            patch.object(settings, "MARKETPLACE_SELLERS_TABLE", _SELLERS),
        ):
            yield {"listings": listings, "purchases": purchases, "payouts": payouts}


def _put_listing(table, listing_id: str, version: int, status: str, **extra) -> None:
    table.put_item(
        Item={
            "listing_id": listing_id,
            "version": version,
            "status": status,
            "slug": "demo",
            **extra,
        }
    )


# ---------------------------------------------------------------------------
# 1. transact_publish_flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transact_publish_flip_success_flips_both(marketplace_tables):
    listings = marketplace_tables["listings"]
    _put_listing(listings, "l1", 1, "published")
    _put_listing(listings, "l1", 2, "review")

    await marketplace_repo.transact_publish_flip(
        listing_id="l1", prev_version=1, new_version=2, approved_by="admin_1", now_iso=NOW
    )

    v1 = await marketplace_repo.get_listing("l1", 1)
    v2 = await marketplace_repo.get_listing("l1", 2)
    assert v1["status"] == "retired"
    assert v2["status"] == "published"
    assert v2["published_by"] == "admin_1"
    assert v2["published_at"] == NOW


@pytest.mark.asyncio
async def test_transact_publish_flip_failure_cancels_both_and_raises(marketplace_tables):
    """One failing condition (new row not in review) cancels the ENTIRE
    transaction — the prev row must NOT be retired."""
    listings = marketplace_tables["listings"]
    _put_listing(listings, "l1", 1, "published")
    _put_listing(listings, "l1", 2, "draft")  # not 'review' -> second leg fails

    with pytest.raises(ClientError):
        await marketplace_repo.transact_publish_flip(
            listing_id="l1", prev_version=1, new_version=2, approved_by="admin_1", now_iso=NOW
        )

    v1 = await marketplace_repo.get_listing("l1", 1)
    v2 = await marketplace_repo.get_listing("l1", 2)
    assert v1["status"] == "published", "prev row must survive a canceled transaction"
    assert v2["status"] == "draft"


@pytest.mark.asyncio
async def test_transact_publish_flip_carries_install_count_forward(marketplace_tables):
    """A version republish must not reset the visible install_count: the new
    published row inherits the retiring row's lifetime count and keeps
    accumulating."""
    listings = marketplace_tables["listings"]
    # v1 published, then 3 real installs recorded against it.
    _put_listing(listings, "l1", 1, "published")
    for _ in range(3):
        await marketplace_repo.increment_listing_install_count(listing_id="l1", version=1)
    assert (await marketplace_repo.get_listing("l1", 1))["install_count"] == 3

    # Seller republishes: v2 goes to review, then admin flips.
    _put_listing(listings, "l1", 2, "review")
    await marketplace_repo.transact_publish_flip(
        listing_id="l1", prev_version=1, new_version=2, approved_by="admin_1", now_iso=NOW
    )

    v2 = await marketplace_repo.get_listing("l1", 2)
    assert v2["status"] == "published"
    assert v2["install_count"] == 3, "republish reset the displayed install count"

    # New installs land on the now-published row and keep the running total.
    await marketplace_repo.increment_listing_install_count(listing_id="l1", version=2)
    assert (await marketplace_repo.get_listing("l1", 2))["install_count"] == 4


@pytest.mark.asyncio
async def test_transact_publish_flip_no_prior_installs_flips_cleanly(marketplace_tables):
    """First-ever publish (no prior install_count) flips without writing a
    spurious counter and does not error."""
    listings = marketplace_tables["listings"]
    _put_listing(listings, "l1", 1, "published")
    _put_listing(listings, "l1", 2, "review")

    await marketplace_repo.transact_publish_flip(
        listing_id="l1", prev_version=1, new_version=2, approved_by="admin_1", now_iso=NOW
    )

    v2 = await marketplace_repo.get_listing("l1", 2)
    assert v2["status"] == "published"
    assert "install_count" not in v2


# ---------------------------------------------------------------------------
# 2. transact_record_purchase_and_credit_seller
# ---------------------------------------------------------------------------


async def _fulfill(purchase_id: str = "cs_sess_1") -> bool:
    return await marketplace_repo.transact_record_purchase_and_credit_seller(
        purchase_id=purchase_id,
        buyer_id="b1",
        seller_id="s1",
        listing_id="l1",
        version=1,
        amount_cents=2000,
        payment_intent_id="pi_1",
        session_id="sess_1",
        transfer_group="purchase_l1_b1_111",
        license_key="iml_test",
        now_iso=NOW,
    )


@pytest.mark.asyncio
async def test_transact_record_purchase_writes_row_and_credits_seller(marketplace_tables):
    assert await _fulfill() is True

    purchase = await marketplace_repo.get_purchase("b1", "cs_sess_1")
    assert purchase is not None
    assert purchase["license_key"] == "iml_test"
    assert purchase["license_key_revoked"] is False
    assert int(purchase["price_paid_cents"]) == 2000

    pa = await marketplace_repo.get_payout_account("s1")
    assert int(pa["balance_held_cents"]) == 2000


@pytest.mark.asyncio
async def test_transact_record_purchase_replay_is_noop(marketplace_tables):
    """Replay (same deterministic purchase_id) -> False, and the seller's
    balance is credited exactly once — the whole transaction cancels."""
    assert await _fulfill() is True
    assert await _fulfill() is False

    pa = await marketplace_repo.get_payout_account("s1")
    assert int(pa["balance_held_cents"]) == 2000, "replay must not double-credit"


# ---------------------------------------------------------------------------
# 3. set_seller_transfer_if_absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_seller_transfer_if_absent_is_set_once(marketplace_tables):
    purchases = marketplace_tables["purchases"]
    purchases.put_item(Item={"buyer_id": "b1", "purchase_id": "p1", "listing_id": "l1", "created_at": NOW})

    first = await marketplace_repo.set_seller_transfer_if_absent(
        buyer_id="b1", purchase_id="p1", transfer_id="tr_1", now_iso=NOW
    )
    second = await marketplace_repo.set_seller_transfer_if_absent(
        buyer_id="b1", purchase_id="p1", transfer_id="tr_2", now_iso=NOW
    )
    assert first is True
    assert second is False

    row = await marketplace_repo.get_purchase("b1", "p1")
    assert row["seller_transfer_id"] == "tr_1", "second stamp must not overwrite the first"


# ---------------------------------------------------------------------------
# 4. decrement_held_balance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decrement_held_balance_never_goes_negative(marketplace_tables):
    payouts = marketplace_tables["payouts"]
    payouts.put_item(Item={"seller_id": "s1", "balance_held_cents": 1000})

    assert await marketplace_repo.decrement_held_balance(seller_id="s1", amount_cents=600) is True
    pa = await marketplace_repo.get_payout_account("s1")
    assert int(pa["balance_held_cents"]) == 400

    # Balance short of the amount -> False, row untouched.
    assert await marketplace_repo.decrement_held_balance(seller_id="s1", amount_cents=600) is False
    pa = await marketplace_repo.get_payout_account("s1")
    assert int(pa["balance_held_cents"]) == 400

    # No row at all -> the condition also fails; no phantom row is written.
    assert await marketplace_repo.decrement_held_balance(seller_id="ghost", amount_cents=1) is False
    assert await marketplace_repo.get_payout_account("ghost") is None


# ---------------------------------------------------------------------------
# 5. has_unrevoked_purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_unrevoked_purchase_revoked_and_other_buyer_fail(marketplace_tables):
    purchases = marketplace_tables["purchases"]
    purchases.put_item(
        Item={
            "buyer_id": "b1",
            "purchase_id": "p1",
            "listing_id": "l1",
            "created_at": NOW,
            "license_key_revoked": True,
        }
    )
    assert await marketplace_repo.has_unrevoked_purchase(buyer_id="b1", listing_id="l1") is False
    assert await marketplace_repo.has_unrevoked_purchase(buyer_id="b2", listing_id="l1") is False

    purchases.put_item(
        Item={
            "buyer_id": "b1",
            "purchase_id": "p2",
            "listing_id": "l1",
            "created_at": NOW,
            "license_key_revoked": False,
        }
    )
    assert await marketplace_repo.has_unrevoked_purchase(buyer_id="b1", listing_id="l1") is True


@pytest.mark.asyncio
async def test_has_unrevoked_purchase_matches_across_pagination_boundary():
    """The matching row on page 2 must be reached — moto won't produce a 1MB
    page split, so the boundary is simulated at the table seam."""
    table = MagicMock()
    last_key = {"listing_id": "l1", "created_at": NOW, "buyer_id": "bX", "purchase_id": "pX"}
    table.query.side_effect = [
        {
            "Items": [{"buyer_id": "other", "purchase_id": "p1", "license_key_revoked": False}],
            "LastEvaluatedKey": last_key,
        },
        {"Items": [{"buyer_id": "b1", "purchase_id": "p2", "license_key_revoked": False}]},
    ]
    with patch.object(marketplace_repo, "_purchases_table", return_value=table):
        assert await marketplace_repo.has_unrevoked_purchase(buyer_id="b1", listing_id="l1") is True

    assert table.query.call_count == 2
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == last_key


# ---------------------------------------------------------------------------
# 6. list_all_versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_versions_excludes_v0_sentinel(marketplace_tables):
    listings = marketplace_tables["listings"]
    # Legacy slug-reservation sentinel: v=0, no status/seller_id.
    listings.put_item(Item={"listing_id": "l1", "version": 0, "slug": "demo"})
    _put_listing(listings, "l1", 1, "published")
    _put_listing(listings, "l1", 2, "draft")

    rows = await marketplace_repo.list_all_versions("l1")
    assert sorted(int(r["version"]) for r in rows) == [1, 2]


@pytest.mark.asyncio
async def test_list_all_versions_paginates():
    table = MagicMock()
    last_key = {"listing_id": "l1", "version": 1}
    table.query.side_effect = [
        {"Items": [{"listing_id": "l1", "version": 1}], "LastEvaluatedKey": last_key},
        {"Items": [{"listing_id": "l1", "version": 2}]},
    ]
    with patch.object(marketplace_repo, "_listings_table", return_value=table):
        rows = await marketplace_repo.list_all_versions("l1")

    assert [r["version"] for r in rows] == [1, 2]
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == last_key


# ---------------------------------------------------------------------------
# 7. mark_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_review_strips_rejection_residue_and_enters_queue(marketplace_tables):
    listings = marketplace_tables["listings"]
    _put_listing(
        listings,
        "l1",
        1,
        "draft",
        seller_id="s1",
        rejection_notes="needs docs",
        rejected_at="2026-07-01T00:00:00Z",
        rejected_by="admin_old",
    )

    attrs = await marketplace_repo.mark_review(listing_id="l1", version=1, seller_id="s1", now_iso=NOW)

    assert attrs["status"] == "review"
    assert attrs["published_at"] == NOW
    for residue in ("rejection_notes", "rejected_at", "rejected_by"):
        assert residue not in attrs, f"{residue} must be REMOVEd on resubmit"

    # published_at is the moderation GSI's sort key — the row must actually
    # surface in the review queue (sparse-GSI visibility).
    queue = await marketplace_repo.get_review_queue_page()
    assert [r["listing_id"] for r in queue] == ["l1"]


# ---------------------------------------------------------------------------
# 8. seller getters — missing-table tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seller_getters_return_none_when_table_missing(marketplace_tables):
    # The fixture never creates the sellers table, so both reads raise
    # ResourceNotFoundException inside boto — the repo maps that to None.
    assert await marketplace_repo.get_seller_profile("s1") is None
    assert await marketplace_repo.find_seller_by_handle("aden") is None


@pytest.mark.asyncio
async def test_seller_roundtrip_once_table_exists(marketplace_tables):
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName=_SELLERS,
        KeySchema=[{"AttributeName": "seller_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "seller_id", "AttributeType": "S"},
            {"AttributeName": "handle", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "handle-index",
                "KeySchema": [{"AttributeName": "handle", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    ).meta.client.get_waiter("table_exists").wait(TableName=_SELLERS)

    await marketplace_repo.put_seller_profile({"seller_id": "s1", "handle": "aden"})
    assert (await marketplace_repo.get_seller_profile("s1"))["handle"] == "aden"
    assert (await marketplace_repo.find_seller_by_handle("aden"))["seller_id"] == "s1"
    assert await marketplace_repo.find_seller_by_handle("ghost") is None


# ---------------------------------------------------------------------------
# 9. mark_taken_down_if_nonterminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_taken_down_if_nonterminal_flips_only_draft_or_review(marketplace_tables):
    listings = marketplace_tables["listings"]
    _put_listing(listings, "l1", 1, "draft")
    _put_listing(listings, "l1", 2, "review")
    _put_listing(listings, "l1", 3, "published")
    _put_listing(listings, "l1", 4, "taken_down")

    assert await marketplace_repo.mark_taken_down_if_nonterminal(listing_id="l1", version=1, now_iso=NOW) is True
    assert await marketplace_repo.mark_taken_down_if_nonterminal(listing_id="l1", version=2, now_iso=NOW) is True
    assert await marketplace_repo.mark_taken_down_if_nonterminal(listing_id="l1", version=3, now_iso=NOW) is False
    assert await marketplace_repo.mark_taken_down_if_nonterminal(listing_id="l1", version=4, now_iso=NOW) is False

    assert (await marketplace_repo.get_listing("l1", 1))["status"] == "taken_down"
    assert (await marketplace_repo.get_listing("l1", 2))["status"] == "taken_down"
    assert (await marketplace_repo.get_listing("l1", 3))["status"] == "published", (
        "published rows go through mark_taken_down, never this conditional flip"
    )
