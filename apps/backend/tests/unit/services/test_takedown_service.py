"""Tests for takedown_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import marketplace_service, takedown_service  # noqa: E402

# Shared moto-backed fixtures live in conftest.py; reuse them for the
# cascade tests so we exercise the real DDB state machine end to end.
from tests.unit.services.conftest import _create_and_publish, _upload_dummy_artifact  # noqa: E402, F401


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_execute_full_takedown_revokes_all_licenses(mock_takedowns, mock_listings, mock_purchases):
    mock_purchases.return_value.query.return_value = {
        "Items": [
            {"buyer_id": "b1", "purchase_id": "p1"},
            {"buyer_id": "b2", "purchase_id": "p2"},
            {"buyer_id": "b3", "purchase_id": "p3"},
        ]
    }
    mock_listings.return_value.update_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()

    await takedown_service.execute_full_takedown(listing_id="l1", takedown_id="t1", decided_by="admin_xyz")

    assert takedown_service.license_service.revoke.await_count == 3
    mock_listings.return_value.update_item.assert_called_once()
    update_kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert ":taken" in update_kwargs["ExpressionAttributeValues"]


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_execute_full_takedown_pages_through_all_purchases(mock_takedowns, mock_listings, mock_purchases):
    """Regression: takedown cascade must page through every purchase row.
    DynamoDB caps Query results at 1MB per page, and a single .query() call
    leaves later buyers with still-valid licenses on a high-volume listing
    (Codex P2 on PR #517, commit bee2fa1c).
    """
    takedown_service.license_service.revoke.reset_mock()
    mock_purchases.return_value.query = MagicMock(
        side_effect=[
            {
                "Items": [{"buyer_id": "b1", "purchase_id": "p1"}, {"buyer_id": "b2", "purchase_id": "p2"}],
                "LastEvaluatedKey": {"buyer_id": "b2", "purchase_id": "p2"},
            },
            {"Items": [{"buyer_id": "b3", "purchase_id": "p3"}]},
        ]
    )
    mock_listings.return_value.update_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()

    await takedown_service.execute_full_takedown(listing_id="l1", takedown_id="t1", decided_by="admin_xyz")

    assert mock_purchases.return_value.query.call_count == 2
    assert takedown_service.license_service.revoke.await_count == 3
    second_call_kwargs = mock_purchases.return_value.query.call_args_list[1].kwargs
    assert second_call_kwargs.get("ExclusiveStartKey") == {"buyer_id": "b2", "purchase_id": "p2"}


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_execute_full_takedown_flips_currently_published_version(mock_takedowns, mock_listings, mock_purchases):
    """Regression: takedown must flip the currently-published row, not
    hardcode v=1. Under publish_new_version, v1 is retired and v2 is
    published; a hardcoded v=1 update marks the already-retired row, leaving v2 live —
    admin "success" but listing remains purchasable
    (Codex P1 on PR #517, commit ba89f60c).
    """
    takedown_service.license_service.revoke.reset_mock()
    mock_purchases.return_value.query = MagicMock(return_value={"Items": []})
    mock_listings.return_value.query = MagicMock(
        return_value={
            "Items": [
                {"listing_id": "l1", "version": 2, "status": "published"},
                {"listing_id": "l1", "version": 1, "status": "retired"},
            ]
        }
    )
    mock_listings.return_value.update_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()

    await takedown_service.execute_full_takedown(listing_id="l1", takedown_id="t1", decided_by="admin_xyz")

    update_kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert update_kwargs["Key"] == {"listing_id": "l1", "version": 2}


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_takedowns_grant_update_guards_with_attribute_exists(mock_takedowns, mock_listings, mock_purchases):
    """Pre-emptive: the takedown-row 'grant' update must guard with
    attribute_exists(takedown_id), mirroring the listings-row guard added
    earlier. Without it, a malformed grant on a fabricated takedown_id
    would upsert a partial row.
    """
    takedown_service.license_service.revoke.reset_mock()
    mock_purchases.return_value.query = MagicMock(return_value={"Items": []})
    mock_listings.return_value.query = MagicMock(
        return_value={"Items": [{"listing_id": "l1", "version": 1, "status": "published"}]}
    )
    mock_listings.return_value.update_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()

    await takedown_service.execute_full_takedown(listing_id="l1", takedown_id="t1", decided_by="admin_xyz")

    grant_kwargs = mock_takedowns.return_value.update_item.call_args.kwargs
    assert grant_kwargs.get("ConditionExpression") == "attribute_exists(takedown_id)"


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_admin_initiated_takedown_404s_before_writing_pending_row(mock_takedowns, mock_listings, mock_purchases):
    """Regression: validate listing exists BEFORE writing the pending
    takedown row, so a phantom listing_id doesn't leave an orphan row
    polluting the audit view (Codex P2 on PR #517, commit 977bf178)."""
    takedown_service.license_service.revoke.reset_mock()
    # Listing query returns empty → ListingNotFoundError before put_item.
    mock_listings.return_value.query = MagicMock(return_value={"Items": []})
    mock_takedowns.return_value.put_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()

    with pytest.raises(takedown_service.ListingNotFoundError):
        await takedown_service.execute_admin_initiated_takedown(
            listing_id="l-ghost",
            reason="policy",
            basis_md="Long enough basis to pass min_length=10 validator.",
            decided_by="user_admin1",
        )

    # Critical: no orphan pending row was written.
    mock_takedowns.return_value.put_item.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.takedown_service._takedowns_table")
async def test_file_takedown_creates_row(mock_table):
    mock_table.return_value.put_item = MagicMock()
    tid = await takedown_service.file_takedown(
        listing_id="l1",
        reason="dmca",
        claimant_name="Alice",
        claimant_email="alice@example.com",
        basis_md="...",
    )
    assert tid is not None
    mock_table.return_value.put_item.assert_called_once()


@pytest.mark.asyncio
@patch("core.services.takedown_service._purchases_table")
@patch("core.services.takedown_service._listings_table")
@patch("core.services.takedown_service._takedowns_table")
@patch("core.services.takedown_service.license_service.revoke", new=AsyncMock())
async def test_execute_admin_initiated_takedown(mock_takedowns, mock_listings, mock_purchases):
    """Admin-initiated takedown writes the row + cascades all four side effects."""
    # Reset the shared AsyncMock so other tests' await counts don't leak in.
    takedown_service.license_service.revoke.reset_mock()

    mock_purchases.return_value.query.return_value = {
        "Items": [
            {"buyer_id": "b1", "purchase_id": "p1"},
            {"buyer_id": "b2", "purchase_id": "p2"},
        ]
    }
    mock_takedowns.return_value.put_item = MagicMock()
    mock_takedowns.return_value.update_item = MagicMock()
    mock_listings.return_value.update_item = MagicMock()

    result = await takedown_service.execute_admin_initiated_takedown(
        listing_id="l-abc",
        reason="policy",
        basis_md="Listing violates platform policy section 4.",
        decided_by="user_admin1",
    )

    # 1. Returned envelope is well-formed.
    assert result["listing_id"] == "l-abc"
    assert result["affected_purchases"] == 2
    assert isinstance(result["takedown_id"], str) and result["takedown_id"]

    # 2. Takedown row was written as `pending` with admin sentinel email —
    #    the cascade flips it to `granted` + stamps `decided_by/decided_at`
    #    in a single update_item, so the put intentionally omits decision
    #    metadata to avoid a double-write.
    mock_takedowns.return_value.put_item.assert_called_once()
    put_item = mock_takedowns.return_value.put_item.call_args.kwargs["Item"]
    assert put_item["listing_id"] == "l-abc"
    assert put_item["decision"] == "pending"
    assert "decided_by" not in put_item
    assert "decided_at" not in put_item
    assert put_item["filed_by_name"] == "admin"
    assert put_item["filed_by_email"] == takedown_service.ADMIN_FILED_BY_EMAIL
    assert put_item["reason"] == "policy"
    assert put_item["basis_md"] == "Listing violates platform policy section 4."
    assert put_item["takedown_id"] == result["takedown_id"]

    # 3. Every purchase had its license revoked.
    assert takedown_service.license_service.revoke.await_count == 2

    # 4. Listing status flipped to taken_down.
    mock_listings.return_value.update_item.assert_called_once()
    update_kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":taken"] == "taken_down"

    # 5. Takedown row stamped with affected_purchases count.
    mock_takedowns.return_value.update_item.assert_called_once()
    stamp_kwargs = mock_takedowns.return_value.update_item.call_args.kwargs
    assert stamp_kwargs["ExpressionAttributeValues"][":n"] == 2
    assert stamp_kwargs["ExpressionAttributeValues"][":granted"] == "granted"
    assert stamp_kwargs["ExpressionAttributeValues"][":by"] == "user_admin1"


# ---------------------------------------------------------------------------
# Cascade-to-non-terminal-v(N+1) tests (moto-backed)
#
# When admin takes down v(N), any in-flight v(N+1) draft/review row must
# also flip to taken_down. Without the cascade the seller's UI surfaces a
# draft on a taken-down listing as if it were still actionable, and a
# later publish_new_version would silently fail because the prev_version
# is no longer 'published'.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takedown_cascades_to_non_terminal_v2_draft(dynamodb_table):
    """v=1 published + v=2 draft (mid-republish) → both end as taken_down."""
    draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
    listing_id = draft["listing_id"]
    await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")

    await takedown_service.execute_admin_initiated_takedown(
        listing_id=listing_id,
        reason="policy",
        basis_md="Listing violates platform policy section 4.",
        decided_by="admin_1",
    )

    v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
    v2 = await marketplace_service.get_by_id(listing_id=listing_id, version=2)
    assert v1["status"] == "taken_down"
    assert v2["status"] == "taken_down"


@pytest.mark.asyncio
async def test_takedown_cascades_to_v2_in_review(dynamodb_table):
    """v=1 published + v=2 review (post-submit, pre-approve) → both taken_down."""
    draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
    listing_id = draft["listing_id"]
    await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
    _upload_dummy_artifact(dynamodb_table, listing_id, version=2)
    await marketplace_service.submit_for_review(listing_id=listing_id, seller_id="seller_1", version=2)

    await takedown_service.execute_admin_initiated_takedown(
        listing_id=listing_id,
        reason="policy",
        basis_md="Listing violates platform policy section 4.",
        decided_by="admin_1",
    )

    v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
    v2 = await marketplace_service.get_by_id(listing_id=listing_id, version=2)
    assert v1["status"] == "taken_down"
    assert v2["status"] == "taken_down"


@pytest.mark.asyncio
async def test_takedown_no_cascade_when_no_v2_exists(dynamodb_table):
    """Listings with no in-flight republish behave exactly as before —
    only the v=1 published row flips. This guards against the cascade
    loop accidentally double-writing the target row when there is no
    v(N+1)."""
    draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
    listing_id = draft["listing_id"]

    await takedown_service.execute_admin_initiated_takedown(
        listing_id=listing_id,
        reason="policy",
        basis_md="Listing violates platform policy section 4.",
        decided_by="admin_1",
    )

    v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
    assert v1["status"] == "taken_down"
    # No v=2 row should have been conjured by the cascade loop.
    v2 = await marketplace_service.get_by_id(listing_id=listing_id, version=2)
    assert v2 is None


@pytest.mark.asyncio
async def test_takedown_cascade_skips_already_terminal_rows(dynamodb_table):
    """A retired v=1 + published v=2 (post second-publish cycle) →
    takedown flips v=2 to taken_down, leaves v=1 retired. The cascade
    must NOT clobber the retired row back to taken_down — its history
    matters for buyer license resolution."""
    draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
    listing_id = draft["listing_id"]
    await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
    _upload_dummy_artifact(dynamodb_table, listing_id, version=2)
    await marketplace_service.submit_for_review(listing_id=listing_id, seller_id="seller_1", version=2)
    await marketplace_service.publish_new_version(
        listing_id=listing_id,
        prev_version=1,
        new_version=2,
        approved_by="admin",
    )
    # Now: v=1 retired, v=2 published — no non-terminal rows.

    await takedown_service.execute_admin_initiated_takedown(
        listing_id=listing_id,
        reason="policy",
        basis_md="Listing violates platform policy section 4.",
        decided_by="admin_1",
    )

    v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
    v2 = await marketplace_service.get_by_id(listing_id=listing_id, version=2)
    assert v1["status"] == "retired", "retired row must not be clobbered to taken_down"
    assert v2["status"] == "taken_down"
