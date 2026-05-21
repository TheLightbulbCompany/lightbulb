"""Tests for marketplace_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import marketplace_service  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.marketplace_service._dynamodb_client")
@patch(
    "core.services.marketplace_service._upload_artifact_to_s3",
    new=AsyncMock(return_value=("listings/abc/v1/", "sha-1")),
)
async def test_create_draft_listing(mock_client):
    mock_client.return_value.transact_write_items = MagicMock(return_value={})
    listing = await marketplace_service.create_draft(
        seller_id="user_abc",
        slug="my-agent",
        name="My Agent",
        description_md="cool",
        format="openclaw",
        price_cents=2000,
        tags=["sales"],
        artifact_bytes=b"tar bytes",
        manifest={"name": "My Agent"},
    )
    assert listing["status"] == "draft"
    assert listing["seller_id"] == "user_abc"
    assert listing["slug"] == "my-agent"
    assert listing["version"] == 1
    # Slug-reservation row is in the same TransactWrite as the listing.
    transact_items = mock_client.return_value.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(transact_items) == 2
    reservation = transact_items[0]["Put"]
    assert reservation["Item"]["listing_id"]["S"] == "slug:my-agent"
    assert reservation["Item"]["version"]["N"] == "0"
    assert reservation["ConditionExpression"] == "attribute_not_exists(listing_id)"


@pytest.mark.asyncio
@patch("core.services.marketplace_service._dynamodb_client")
@patch(
    "core.services.marketplace_service._upload_artifact_to_s3",
    new=AsyncMock(return_value=("listings/abc/v1/", "sha-1")),
)
async def test_create_draft_rejects_duplicate_slug(mock_client):
    """Regression: slug uniqueness must be atomic via TransactWriteItems +
    a slug-reservation row, not a read-then-put. Two concurrent create_draft
    calls for the same slug both pass the read but TransactWrite fails for
    the loser."""
    from botocore.exceptions import ClientError

    mock_client.return_value.transact_write_items = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "TransactionCanceledException", "Message": "x"}},
            "TransactWriteItems",
        )
    )
    with pytest.raises(marketplace_service.SlugCollisionError):
        await marketplace_service.create_draft(
            seller_id="user_abc",
            slug="my-agent",
            name="My Agent",
            description_md="cool",
            format="openclaw",
            price_cents=2000,
            tags=["sales"],
            artifact_bytes=b"x",
            manifest={"name": "x"},
        )


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_submit_listing_transitions_draft_to_review(mock_listings):
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"status": "review"}})
    result = await marketplace_service.submit_for_review(listing_id="l1", seller_id="user_abc")
    assert result["status"] == "review"
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    # ConditionExpression must require status='draft' to prevent
    # double-submit or submit from a wrong state.
    values = kwargs.get("ExpressionAttributeValues", {})
    assert ":draft" in values or "draft" in str(values)


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_submit_for_review_populates_published_at_for_moderation_gsi(mock_listings):
    """Regression: status-published-index has published_at as its sort key.
    DynamoDB sparse-GSI semantics exclude items whose sort key is None, so
    submit_for_review MUST set published_at or the moderation queue is
    structurally empty."""
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"status": "review"}})
    await marketplace_service.submit_for_review(listing_id="l1", seller_id="user_abc")
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert "published_at" in kwargs["UpdateExpression"]


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_submit_rejects_when_not_in_draft(mock_listings):
    from botocore.exceptions import ClientError

    mock_listings.return_value.update_item = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
            "UpdateItem",
        )
    )
    with pytest.raises(marketplace_service.InvalidStateError):
        await marketplace_service.submit_for_review(listing_id="l1", seller_id="user_abc")


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_reject_uses_conditional_status_check(mock_listings):
    """Regression: reject must guard with ConditionExpression so a malformed
    admin request can't upsert a fabricated listing (DDB UpdateItem upserts
    by default) or transition a non-review row.
    """
    mock_listings.return_value.update_item = MagicMock()
    await marketplace_service.reject(listing_id="l1", version=1, notes="incomplete docs", rejected_by="admin_xyz")
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert "ConditionExpression" in kwargs
    assert ":review" in kwargs["ExpressionAttributeValues"]


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_reject_rejects_non_review_state(mock_listings):
    from botocore.exceptions import ClientError

    mock_listings.return_value.update_item = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
            "UpdateItem",
        )
    )
    with pytest.raises(marketplace_service.InvalidStateError):
        await marketplace_service.reject(listing_id="l1", version=1, notes="x", rejected_by="admin_xyz")


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_get_by_slug_prefers_published_over_higher_review_version(mock_listings):
    """Regression: during publish_v2 flow, a v2 row exists in 'review' state
    while v1 is still 'published'. Returning the highest version
    unconditionally would 404 the still-live v1 on the public storefront for
    the duration of moderation.
    """
    mock_listings.return_value.query.return_value = {
        "Items": [
            {"version": 2, "status": "review", "slug": "demo"},
            {"version": 1, "status": "published", "slug": "demo"},
        ]
    }
    result = await marketplace_service.get_by_slug(slug="demo")
    assert result is not None
    assert result["version"] == 1
    assert result["status"] == "published"


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_get_by_slug_returns_highest_when_no_published(mock_listings):
    """If nothing is published yet (v1 in draft/review), fall back to the
    highest-version row so admin/seller paths still find the listing."""
    mock_listings.return_value.query.return_value = {
        "Items": [
            {"version": 1, "status": "draft", "slug": "demo"},
        ]
    }
    result = await marketplace_service.get_by_slug(slug="demo")
    assert result is not None
    assert result["version"] == 1
    assert result["status"] == "draft"


@pytest.mark.asyncio
@patch("core.services.marketplace_service._dynamodb_client")
async def test_publish_v2_translates_transaction_cancel_to_invalid_state(mock_client):
    """Regression: publish_v2 raises ClientError(TransactionCanceledException)
    when either ConditionExpression fails (prev not 'published' or new not
    'review'). Translate to InvalidStateError so the route turns it into a
    409 instead of a 500."""
    from botocore.exceptions import ClientError

    mock_client.return_value.transact_write_items = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "TransactionCanceledException", "Message": "x"}},
            "TransactWriteItems",
        )
    )
    with pytest.raises(marketplace_service.InvalidStateError):
        await marketplace_service.publish_v2(
            listing_id="l1",
            prev_version=1,
            new_version=2,
            approved_by="admin_xyz",
        )


@pytest.mark.asyncio
@patch("core.services.marketplace_service._dynamodb_client")
async def test_publish_v2_atomically_retires_prev_and_publishes_new(mock_client):
    """Publishing v_new atomically flips prev_version -> retired AND
    new_version -> published in a single TransactWriteItems on the
    listings table."""
    mock_client.return_value.transact_write_items = MagicMock(return_value={})
    await marketplace_service.publish_v2(
        listing_id="l1",
        prev_version=1,
        new_version=2,
        approved_by="admin_xyz",
    )
    mock_client.return_value.transact_write_items.assert_called_once()
    items = mock_client.return_value.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(items) == 2
    # Both items are Updates on the listings table (single-table design).
    actions = [list(item.keys())[0] for item in items]
    assert actions == ["Update", "Update"]
    # First update retires the previous version (UpdateExpression sets
    # status to :retired); second publishes the new version (UpdateExpression
    # sets status to :pub). Asserting on the UpdateExpression directly is
    # more reliable than reading ExpressionAttributeValues, since each item
    # carries multiple status values (one for the condition, one for the set).
    update_exprs = [item["Update"]["UpdateExpression"] for item in items]
    assert any(":retired" in expr for expr in update_exprs)
    assert any(":pub" in expr and ":retired" not in expr for expr in update_exprs)
    # Verify per-item key bindings.
    retire_item = next(item for item in items if ":retired" in item["Update"]["UpdateExpression"])
    publish_item = next(
        item
        for item in items
        if ":pub" in item["Update"]["UpdateExpression"] and ":retired" not in item["Update"]["UpdateExpression"]
    )
    assert retire_item["Update"]["ExpressionAttributeValues"][":retired"]["S"] == "retired"
    assert publish_item["Update"]["ExpressionAttributeValues"][":pub"]["S"] == "published"


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_submit_for_review_clears_prior_rejection_fields(mock_listings):
    """Regression: once a seller resubmits a rejected draft, EVERY
    moderation field must come off the row — rejection_notes,
    rejected_at, and rejected_by.
    marketplace_search returns raw listing dicts, so anything left on the
    item leaks to buyers once the listing is approved (including the
    internal admin identifier in rejected_by)."""
    mock_listings.return_value.get_item = MagicMock(return_value={"Item": {"artifact_uploaded": True}})
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"status": "review"}})
    await marketplace_service.submit_for_review(listing_id="l1", seller_id="user_abc")
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    expr = kwargs["UpdateExpression"]
    assert "REMOVE" in expr
    assert "rejection_notes" in expr
    assert "rejected_at" in expr
    assert "rejected_by" in expr


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_reject_writes_rejected_at_timestamp(mock_listings):
    """Seller UI surfaces 'Rejected · MM-DD' on the listing row; without
    rejected_at we'd have to fall back to updated_at, which mutates on
    every patch and stops reflecting the rejection moment."""
    mock_listings.return_value.update_item = MagicMock()
    result = await marketplace_service.reject(
        listing_id="l1", version=1, notes="needs more docs", rejected_by="admin_xyz"
    )
    assert "rejected_at" in result
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert "rejected_at = :now" in kwargs["UpdateExpression"]


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_update_draft_writes_only_provided_fields(mock_listings):
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"name": "New Name"}})
    result = await marketplace_service.update_draft(
        listing_id="l1",
        seller_id="user_abc",
        name="New Name",
        price_cents=500,
    )
    assert result["name"] == "New Name"
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    # Only the two fields we passed should appear in the SET clause
    # (besides the always-written updated_at).
    expr = kwargs["UpdateExpression"]
    # name, price_cents, status (auto-demoted to draft), updated_at
    assert expr.count("=") == 4
    values = kwargs["ExpressionAttributeValues"]
    # Field values are bound via :c0/:c1 placeholders to defeat reserved-word
    # collisions ("name" is reserved in DDB).
    bound_values = {v for k, v in values.items() if k.startswith(":c")}
    assert bound_values == {"New Name", 500}


@pytest.mark.asyncio
async def test_update_draft_rejects_empty_patch():
    """No fields = no-op write would still bump updated_at and look like
    a real edit. Forbid it so callers can't waste a write."""
    with pytest.raises(marketplace_service.InvalidStateError):
        await marketplace_service.update_draft(listing_id="l1", seller_id="user_abc")


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_update_draft_targets_specified_version(mock_listings):
    """Regression: hardcoding version=1 means a rejected v_new=2 row
    (future republish flow) gets 409'd or mutates the wrong row. The
    version arg threads through.
    """
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"name": "X"}})
    await marketplace_service.update_draft(listing_id="l1", seller_id="user_abc", name="X", version=2)
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert kwargs["Key"] == {"listing_id": "l1", "version": 2}


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_submit_for_review_targets_specified_version(mock_listings):
    """Same regression as update_draft: a v_new=2 resubmit must target v=2."""
    mock_listings.return_value.get_item = MagicMock(return_value={"Item": {"artifact_uploaded": True}})
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"status": "review"}})
    await marketplace_service.submit_for_review(listing_id="l1", seller_id="user_abc", version=2)
    update_kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert update_kwargs["Key"] == {"listing_id": "l1", "version": 2}
    get_kwargs = mock_listings.return_value.get_item.call_args.kwargs
    assert get_kwargs["Key"] == {"listing_id": "l1", "version": 2}


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_update_draft_rejects_non_draft_or_wrong_seller(mock_listings):
    """ConditionExpression check failure surfaces as InvalidStateError so
    the route translates to 409 — same shape as submit_for_review."""
    from botocore.exceptions import ClientError

    mock_listings.return_value.update_item = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
            "UpdateItem",
        )
    )
    with pytest.raises(marketplace_service.InvalidStateError):
        await marketplace_service.update_draft(listing_id="l1", seller_id="user_abc", name="x")


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
async def test_update_draft_allows_review_state_and_demotes_to_draft(mock_listings):
    """Edit-during-review: a seller editing a listing currently in
    moderation review pulls it back to draft. Without that demotion, the
    moderation queue holds a row whose metadata is mid-mutation — a
    moderator could approve the stale snapshot. The seller resubmits after
    editing to re-enter review.
    """
    mock_listings.return_value.update_item = MagicMock(return_value={"Attributes": {"status": "draft"}})
    await marketplace_service.update_draft(listing_id="l1", seller_id="user_abc", name="New Name")
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    # IN list permits both draft and review as starting states.
    assert kwargs["ConditionExpression"] == "seller_id = :sid AND #s IN (:draft, :review)"
    # The SET clause always pins status to draft — no-op on draft, demotes on review.
    assert "#s = :draft" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":review"] == "review"


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
@patch(
    "core.services.marketplace_service._upload_artifact_to_s3",
    new=AsyncMock(return_value=("listings/abc/v1/", "sha-1")),
)
async def test_replace_artifact_persists_bundle_selection(mock_listings):
    """Regression: the bundle selection lives in the artifact tarball but
    isn't recoverable from the
    listing row alone. Without persisted columns, edit-mode would prefill
    bundleMcps=[]/bundleSkills=[] and silently re-export the artifact
    without the integrations buyers previously received."""
    mock_listings.return_value.get_item = MagicMock(return_value={"Item": {"seller_id": "user_abc", "status": "draft"}})
    mock_listings.return_value.update_item = MagicMock()
    await marketplace_service.replace_artifact(
        listing_id="l1",
        seller_id="user_abc",
        artifact_bytes=b"x",
        manifest={"name": "x"},
        bundle_mcps=["jira-mcp", "slack-mcp"],
        bundle_skills=["search", "jira"],
    )
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert "bundle_mcps = :bmcps" in kwargs["UpdateExpression"]
    assert "bundle_skills = :bskills" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":bmcps"] == ["jira-mcp", "slack-mcp"]
    assert kwargs["ExpressionAttributeValues"][":bskills"] == ["search", "jira"]


@pytest.mark.asyncio
@patch("core.services.marketplace_service._listings_table")
@patch(
    "core.services.marketplace_service._upload_artifact_to_s3",
    new=AsyncMock(return_value=("listings/abc/v1/", "sha-1")),
)
async def test_replace_artifact_persists_agent_id_when_provided(mock_listings):
    """Edit mode reads agent_id off the listing row to lock the wizard's
    PickAgent step. Persist it as a top-level column on replace_artifact
    so the frontend doesn't have to parse manifest_json."""
    mock_listings.return_value.get_item = MagicMock(return_value={"Item": {"seller_id": "user_abc", "status": "draft"}})
    mock_listings.return_value.update_item = MagicMock()
    await marketplace_service.replace_artifact(
        listing_id="l1",
        seller_id="user_abc",
        artifact_bytes=b"x",
        manifest={"name": "x"},
        agent_id="agent_ray",
    )
    kwargs = mock_listings.return_value.update_item.call_args.kwargs
    assert "agent_id = :aid" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":aid"] == "agent_ray"
