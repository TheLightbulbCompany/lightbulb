"""Tests for marketplace_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import marketplace_service  # noqa: E402

# Shared moto-backed fixtures live in conftest.py so test_takedown_service
# can reuse them without duplication.
from tests.unit.services.conftest import _create_and_publish, _upload_dummy_artifact  # noqa: E402, F401


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
    """Regression: during publish_new_version flow, a v2 row exists in 'review' state
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
async def test_publish_new_version_translates_transaction_cancel_to_invalid_state(mock_client):
    """Regression: publish_new_version raises ClientError(TransactionCanceledException)
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
        await marketplace_service.publish_new_version(
            listing_id="l1",
            prev_version=1,
            new_version=2,
            approved_by="admin_xyz",
        )


@pytest.mark.asyncio
@patch("core.services.marketplace_service._dynamodb_client")
async def test_publish_new_version_atomically_retires_prev_and_publishes_new(mock_client):
    """Publishing v_new atomically flips prev_version -> retired AND
    new_version -> published in a single TransactWriteItems on the
    listings table."""
    mock_client.return_value.transact_write_items = MagicMock(return_value={})
    await marketplace_service.publish_new_version(
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


# ---------------------------------------------------------------------------
# moto-backed tests for create_new_version
#
# Unlike the mock-based tests above, create_new_version's behavior depends on
# the full row set for a listing (query → filter → max(version) → conditional
# put). Mocking that with MagicMock would either hardcode the responses (which
# defeats the point) or grow a re-implementation of the conditional-put
# semantics. moto gives real DDB behavior — slug-reservation rows, conditional
# puts, query semantics — so the test exercises the actual code path.
#
# The `dynamodb_table` fixture + `_create_and_publish` / `_upload_dummy_artifact`
# helpers live in tests/unit/services/conftest.py so test_takedown_service
# can reuse them.
# ---------------------------------------------------------------------------


class TestCreateNewVersion:
    @pytest.mark.asyncio
    async def test_clones_published_row_to_next_version_draft(self, dynamodb_table):
        draft = await _create_and_publish(
            dynamodb_table,
            listing_id="ignored",  # listing_id is generated by create_draft
            seller_id="seller_1",
            slug="my-agent",
            name="My Agent",
            description_md="hello",
            price_cents=500,
            tags=["productivity"],
            category="agent",
        )
        listing_id = draft["listing_id"]

        result = await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")

        assert result["version"] == 2
        assert result["status"] == "draft"
        assert result["seller_id"] == "seller_1"
        assert result["name"] == "My Agent"
        assert result["description_md"] == "hello"
        assert int(result["price_cents"]) == 500
        assert list(result["tags"]) == ["productivity"]
        assert result["category"] == "agent"
        # Artifact fields intentionally NOT carried over — seller must
        # call /artifact-from-agent before submit_for_review will accept
        # the new draft.
        assert "manifest_sha256" not in result
        assert "s3_prefix" not in result
        assert result["artifact_uploaded"] is False
        # v=1 is still published (still the buyer-visible row).
        v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
        assert v1["status"] == "published"

    @pytest.mark.asyncio
    async def test_preserves_agent_and_bundle_metadata_on_clone(self, dynamodb_table):
        """Republish drafts MUST carry agent_id + bundle_mcps + bundle_skills
        forward so the wizard locks the right agent and prefills the bundle
        checkboxes. Without these, a metadata-only republish silently strips
        integrations the live version shipped."""
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        # Real flow sets these via replace_artifact (which also writes S3);
        # we stamp them directly so the test stays scoped to the cloning logic.
        dynamodb_table.update_item(
            Key={"listing_id": listing_id, "version": 1},
            UpdateExpression="SET agent_id = :a, bundle_mcps = :m, bundle_skills = :s, manifest_json = :mj",
            ExpressionAttributeValues={
                ":a": "agent_abc",
                ":m": ["filesystem", "github"],
                ":s": ["calendar"],
                ":mj": {"agent_id": "agent_abc", "name": "X"},
            },
        )

        result = await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")

        assert result["agent_id"] == "agent_abc"
        assert list(result["bundle_mcps"]) == ["filesystem", "github"]
        assert list(result["bundle_skills"]) == ["calendar"]
        assert result["manifest_json"]["agent_id"] == "agent_abc"

    @pytest.mark.asyncio
    async def test_raises_no_published_version_when_only_draft_exists(self, dynamodb_table):
        draft = await marketplace_service.create_draft(
            seller_id="seller_1",
            slug="x",
            name="X",
            description_md="d",
            format="openclaw_agent",
            price_cents=0,
            tags=[],
            category="agent",
            artifact_bytes=b"",
            manifest={"name": "X"},
        )
        with pytest.raises(marketplace_service.NoPublishedVersionError):
            await marketplace_service.create_new_version(listing_id=draft["listing_id"], seller_id="seller_1")

    @pytest.mark.asyncio
    async def test_raises_when_existing_draft_in_progress(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        # Second call must fail — there's already a draft v=2 in flight.
        with pytest.raises(marketplace_service.RepublishAlreadyInProgressError) as exc:
            await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        # Error message names the in-flight version so the seller UI can
        # render a "you have a draft v2" affordance.
        assert "v2" in str(exc.value)

    @pytest.mark.asyncio
    async def test_raises_when_existing_review_in_progress(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        _upload_dummy_artifact(dynamodb_table, listing_id, version=2)
        await marketplace_service.submit_for_review(listing_id=listing_id, seller_id="seller_1", version=2)
        with pytest.raises(marketplace_service.RepublishAlreadyInProgressError):
            await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")

    @pytest.mark.asyncio
    async def test_raises_permission_when_seller_mismatch(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        with pytest.raises(marketplace_service.PermissionError):
            await marketplace_service.create_new_version(listing_id=draft["listing_id"], seller_id="other_seller")

    @pytest.mark.asyncio
    async def test_race_detected_via_conditional_put(self, dynamodb_table, monkeypatch):
        """The scan-side guard and the conditional-put rescue are
        independent defenses: the scan catches the common case where a
        prior draft already exists, the conditional put catches the rare
        case where two callers both pass the scan and try to insert the
        same v(N+1). This test exercises the rescue branch by forcing
        put_item to raise as if a parallel caller won the write.

        We monkeypatch `_listings_table` (not the table instance) because
        `_listings_table()` returns a fresh `.Table(...)` resource on
        every call, so patching the instance returned to the test
        wouldn't stick across calls. The stub proxies reads to the real
        moto table so the scan-side guard still passes, then raises
        ConditionalCheckFailedException on put_item.
        """
        import botocore.exceptions

        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]

        class _FailingPutTable:
            """Delegates everything to the real moto table except
            put_item, which raises as if a concurrent caller already
            inserted v(N+1)."""

            def __init__(self, real_table):
                self._real_table = real_table

            def __getattr__(self, name):
                return getattr(self._real_table, name)

            def put_item(self, *args, **kwargs):
                raise botocore.exceptions.ClientError(
                    error_response={
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "simulated race",
                        }
                    },
                    operation_name="PutItem",
                )

        monkeypatch.setattr(
            marketplace_service,
            "_listings_table",
            lambda: _FailingPutTable(dynamodb_table),
        )

        with pytest.raises(marketplace_service.RepublishAlreadyInProgressError) as exc:
            await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        # The rescue path's message is distinct from the scan-side
        # guard's "republish already in progress for vN" so the API
        # layer (and any future log scraping) can tell them apart.
        assert "race detected" in str(exc.value)
        assert "v2" in str(exc.value)

    @pytest.mark.asyncio
    async def test_scales_to_v3(self, dynamodb_table):
        """v=1 published → v=2 republished+approved → v=3 republish must
        succeed. Guards against an off-by-one in max(version)+1 that would
        only show up on the second republish cycle."""
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
        # v=2 is now published; v=1 is retired. Republish to v=3.
        v3 = await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        assert v3["version"] == 3
        assert v3["status"] == "draft"


class TestDiscardDraft:
    @pytest.mark.asyncio
    async def test_discards_draft_row(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        await marketplace_service.discard_draft(listing_id=listing_id, seller_id="seller_1", version=2)
        # v=2 row gone, v=1 still published
        assert await marketplace_service.get_by_id(listing_id=listing_id, version=2) is None
        v1 = await marketplace_service.get_by_id(listing_id=listing_id, version=1)
        assert v1["status"] == "published"

    @pytest.mark.asyncio
    async def test_rejects_when_not_draft(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        _upload_dummy_artifact(dynamodb_table, listing_id, version=2)
        await marketplace_service.submit_for_review(listing_id=listing_id, seller_id="seller_1", version=2)
        with pytest.raises(marketplace_service.InvalidStateError):
            await marketplace_service.discard_draft(listing_id=listing_id, seller_id="seller_1", version=2)

    @pytest.mark.asyncio
    async def test_rejects_when_not_owner(self, dynamodb_table):
        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")
        with pytest.raises(marketplace_service.PermissionError):
            await marketplace_service.discard_draft(listing_id=listing_id, seller_id="other", version=2)

    @pytest.mark.asyncio
    async def test_cannot_discard_v1(self, dynamodb_table):
        """v=1 carries the slug reservation; full listing retraction is a
        separate flow not in scope here."""
        result = await marketplace_service.create_draft(
            seller_id="seller_1",
            slug="x",
            name="X",
            description_md="",
            format="openclaw_agent",
            price_cents=0,
            tags=[],
            category="agent",
            artifact_bytes=b"",
            manifest={"name": "X"},
        )
        with pytest.raises(marketplace_service.InvalidStateError):
            await marketplace_service.discard_draft(listing_id=result["listing_id"], seller_id="seller_1", version=1)

    @pytest.mark.asyncio
    async def test_race_handled_when_state_changes_between_read_and_delete(self, dynamodb_table, monkeypatch):
        """If the row's status flips between get_item and delete_item (e.g.
        a parallel submit_for_review wins), the conditional delete throws
        ConditionalCheckFailedException — map it to InvalidStateError so
        the router returns 409 instead of 500.

        Uses the same `_FailingTable` proxy pattern as
        test_race_detected_via_conditional_put — `_listings_table()` returns
        a fresh resource each call, so we monkeypatch the factory itself.
        Reads (get_item) proxy through to real moto; delete_item raises.
        """
        import botocore.exceptions

        draft = await _create_and_publish(dynamodb_table, "ignored", "seller_1", "x")
        listing_id = draft["listing_id"]
        await marketplace_service.create_new_version(listing_id=listing_id, seller_id="seller_1")

        class _FailingDeleteTable:
            def __init__(self, real_table):
                self._real_table = real_table

            def __getattr__(self, name):
                return getattr(self._real_table, name)

            def delete_item(self, *args, **kwargs):
                raise botocore.exceptions.ClientError(
                    error_response={
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "simulated race",
                        }
                    },
                    operation_name="DeleteItem",
                )

        monkeypatch.setattr(
            marketplace_service,
            "_listings_table",
            lambda: _FailingDeleteTable(dynamodb_table),
        )

        with pytest.raises(marketplace_service.InvalidStateError) as exc:
            await marketplace_service.discard_draft(listing_id=listing_id, seller_id="seller_1", version=2)
        assert "changed state during discard" in str(exc.value)
