"""Tests for takedown_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import takedown_service  # noqa: E402


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
