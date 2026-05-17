"""Tests for marketplace_listings router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext  # noqa: E402


@pytest.fixture
def unauth_client():
    """Plain TestClient without auth override (for endpoints that 401 unauthenticated).

    Defensively clears app.dependency_overrides on enter + exit so a prior
    suite (e.g. tests/contract/) that left an auth override behind cannot
    silently bypass auth here. Without this, test_create_draft_requires_auth
    sees a leaked override, the route runs unauthenticated, and the
    boto3.Table("") call surfaces as a misleading ParamValidationError.
    """
    from main import app

    app.dependency_overrides.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_create_draft_requires_auth(unauth_client):
    """Without a Clerk JWT, create draft must 401/403.

    Payload must satisfy ListingCreate min_length validators (slug≥2, name≥2,
    description_md≥1) — otherwise FastAPI 422s on the body before reaching
    the auth dependency, which is not what we're testing here.
    """
    resp = unauth_client.post(
        "/api/v1/marketplace/listings",
        json={
            "slug": "test-slug",
            "name": "Test Listing",
            "description_md": "x",
            "format": "openclaw",
            "price_cents": 0,
            "tags": [],
        },
    )
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_endpoint_passes_through_to_service(mock_search, unauth_client):
    mock_search.return_value = [{"slug": "foo", "name": "Foo"}]
    resp = unauth_client.get("/api/v1/marketplace/listings/search?q=foo&format=openclaw&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [{"slug": "foo", "name": "Foo"}]}
    mock_search.assert_called_once_with(
        query="foo",
        format="openclaw",
        limit=10,
        seller_id=None,
        category=None,
        include_unpublished=False,
        status_in=None,
    )
    # Cache header is set so CloudFront / browser respect the snapshot TTL.
    assert resp.headers["Cache-Control"] == "public, max-age=60"


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_endpoint_works_without_query(mock_search, unauth_client):
    mock_search.return_value = []
    resp = unauth_client.get("/api/v1/marketplace/listings/search")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}
    mock_search.assert_called_once_with(
        query=None,
        format=None,
        limit=24,
        seller_id=None,
        category=None,
        include_unpublished=False,
        status_in=None,
    )


def test_search_endpoint_rejects_invalid_format(unauth_client):
    # The format query param is constrained to openclaw|skillmd via regex.
    resp = unauth_client.get("/api/v1/marketplace/listings/search?format=bogus")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Get-by-slug endpoint
# ---------------------------------------------------------------------------


def _stub_s3_get_manifest(manifest: dict | bytes):
    """boto3.client('s3') stub whose get_object returns a manifest.json body."""
    import json as _json

    body = MagicMock()
    if isinstance(manifest, (bytes, bytearray)):
        body.read.return_value = bytes(manifest)
    else:
        body.read.return_value = _json.dumps(manifest).encode("utf-8")
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": body}
    return s3


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_returns_listing_and_manifest(mock_get_by_slug, mock_boto, unauth_client):
    """The new shape is ``{listing, manifest}``; manifest is fetched from S3."""
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    rich_manifest = {
        "name": "Demo Agent",
        "format": "openclaw",
        "emoji": "🐦",
        "vibe": "a friendly research assistant",
        "suggested_model": "us.anthropic.claude-3-5-sonnet",
        "required_skills": ["search", "summarize"],
        "required_plugins": ["web"],
        "required_tools": [],
        "suggested_channels": ["slack"],
    }
    mock_boto.return_value = _stub_s3_get_manifest(rich_manifest)

    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"listing", "manifest"}
    assert body["listing"]["slug"] == "demo-agent"
    assert body["listing"]["status"] == "published"
    assert body["manifest"] == rich_manifest
    # S3 fetched the manifest.json under the listing's s3_prefix.
    s3_call = mock_boto.return_value.get_object.call_args
    assert s3_call.kwargs["Key"].endswith("manifest.json")


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_when_s3_fails(mock_get_by_slug, mock_boto, unauth_client):
    """S3 failures must NOT 500 the listing endpoint — manifest falls back to None."""
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")
    mock_boto.return_value = s3

    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_for_malformed_json(mock_get_by_slug, mock_boto, unauth_client):
    """Malformed JSON in S3 must NOT 500 the listing endpoint — manifest falls back to None.

    Pins the json.JSONDecodeError branch of the narrowed exception catch
    (review M1). Previously a broad `except Exception` swallowed this; the
    narrowed catch keeps the same user-visible behaviour while letting
    programming errors propagate.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    # Body bytes that are not valid JSON.
    mock_boto.return_value = _stub_s3_get_manifest(b"not json at all")

    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_for_non_dict_body(mock_get_by_slug, mock_boto, unauth_client):
    """Valid JSON that isn't an object must also fall back to manifest=None.

    The router has an explicit `if not isinstance(manifest, dict): manifest = None`
    guard. This test pins it so a future refactor that drops the guard will
    surface a string/list/scalar as a manifest and trip this assertion.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    # Valid JSON, but a list — not a dict. The router guard converts to None.
    mock_boto.return_value = _stub_s3_get_manifest(b"[1, 2, 3]")

    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_404_for_taken_down(mock_get_by_slug, unauth_client):
    listing = _published_listing()
    listing["status"] = "taken_down"
    mock_get_by_slug.return_value = listing
    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["draft", "review", "retired"])
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_404_for_non_published_states(mock_get_by_slug, unauth_client, status):
    """Regression: only published listings are publicly visible. The previous
    blacklist allowed draft + review through, leaking unmoderated content if
    a slug was guessed (Codex P1 on PR #517, commit 23bdc518).
    """
    listing = _published_listing()
    listing["status"] = status
    mock_get_by_slug.return_value = listing
    resp = unauth_client.get("/api/v1/marketplace/listings/demo-agent")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_get_listing_404_for_unknown_slug(mock_get_by_slug, unauth_client):
    mock_get_by_slug.return_value = None
    resp = unauth_client.get("/api/v1/marketplace/listings/missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Deploy endpoint
# ---------------------------------------------------------------------------


def _published_listing(*, price_cents: int = 0, listing_id: str = "lst_1") -> dict:
    return {
        "listing_id": listing_id,
        "version": 1,
        "slug": "demo-agent",
        "name": "Demo Agent",
        "status": "published",
        "price_cents": price_cents,
        "s3_prefix": f"listings/{listing_id}/v1/",
        "manifest_json": {"name": "Demo Agent", "format": "openclaw"},
    }


def _stub_s3_get_object(
    _tar_bytes: bytes = b"fake-tar",
    _slice: dict | None = None,
    _slice_missing: bool = False,
):
    """Build a boto3.client('s3') stub that dispatches by S3 Key.

    deploy_listing fetches both ``workspace.tar.gz`` and ``openclaw-slice.json``;
    we have to return the right body for each. Pass ``_slice_missing=True``
    to simulate a legacy listing where the slice key doesn't exist (the
    route should tolerate it via NoSuchKey).
    """
    import json as _json

    s3 = MagicMock()

    def _get_object(*, Bucket: str, Key: str):
        if Key.endswith("workspace.tar.gz"):
            body = MagicMock()
            body.read.return_value = _tar_bytes
            return {"Body": body}
        if Key.endswith("openclaw-slice.json"):
            if _slice_missing:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "slice missing"}},
                    "GetObject",
                )
            body = MagicMock()
            body.read.return_value = _json.dumps(_slice or {}).encode("utf-8")
            return {"Body": body}
        raise AssertionError(f"unexpected S3 Key: {Key!r}")

    s3.get_object.side_effect = _get_object
    return s3


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_free_listing_succeeds(mock_get_by_slug, mock_get_catalog, mock_boto, client):
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    real_slice = {
        "agent": {"name": "Demo Agent", "skills": ["search"]},
        "plugins": {"entries": {"memory": {"enabled": True}}},
        "tools": {"profile": "default"},
        "cron_jobs": [],
    }
    mock_boto.return_value = _stub_s3_get_object(_slice=real_slice)
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "agent_abc123",
            "name": "Demo Agent",
            "skills_added": ["search"],
            "plugins_enabled": ["memory"],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_uuid"] == "agent_abc123"
    assert body["agent_id"] == "agent_abc123"
    assert body["config_registered"] is True
    catalog.deploy_from_artifact.assert_awaited_once()
    kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert kwargs["slug"] == "demo-agent"
    assert kwargs["tar_bytes"] == b"fake-tar"
    # Slice fetched from S3 and forwarded so the buyer's openclaw.json gets
    # patched (i.e. the agent auto-registers).
    assert kwargs["slice_"] == real_slice


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_legacy_listing_without_slice_falls_back(mock_get_by_slug, mock_get_catalog, mock_boto, client):
    """Listings published before slice-synthesis shipped have no
    openclaw-slice.json key. The deploy must tolerate NoSuchKey on the
    slice fetch and fall through to the no-config-registration path
    (matching the legacy behavior).
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    mock_boto.return_value = _stub_s3_get_object(_slice_missing=True)
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "agent_legacy_1",
            "name": "Demo Agent",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": False,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 200, resp.text
    kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert kwargs["slice_"] == {}
    assert resp.json()["config_registered"] is False


@patch("routers.marketplace_listings._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_paid_listing_without_purchase_403(
    mock_get_by_slug, mock_get_catalog, mock_boto, mock_purchases, client
):
    mock_get_by_slug.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 403
    # We never reached S3 / catalog — entitlement gate hit first.
    mock_boto.assert_not_called()
    mock_get_catalog.assert_not_called()


@patch("routers.marketplace_listings._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_paid_listing_with_revoked_license_403(
    mock_get_by_slug, mock_get_catalog, mock_boto, mock_purchases, client
):
    mock_get_by_slug.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {
        "Items": [
            {
                "buyer_id": "user_test_123",
                "purchase_id": "p1",
                "listing_id": "lst_1",
                "license_key_revoked": True,
                "license_key_revoked_reason": "refunded",
            }
        ]
    }

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 403
    mock_get_catalog.assert_not_called()


@patch("routers.marketplace_listings._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_paid_listing_with_valid_purchase_succeeds(
    mock_get_by_slug, mock_get_catalog, mock_boto, mock_purchases, client
):
    mock_get_by_slug.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {
        "Items": [
            {
                "buyer_id": "user_test_123",
                "purchase_id": "p1",
                "listing_id": "lst_1",
                "license_key_revoked": False,
            }
        ]
    }
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo Agent"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "agent_xyz789",
            "name": "Demo Agent",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 200
    assert resp.json()["agent_uuid"] == "agent_xyz789"


@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_taken_down_listing_404(mock_get_by_slug, client):
    listing = _published_listing()
    listing["status"] = "taken_down"
    mock_get_by_slug.return_value = listing

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_retired_listing_404(mock_get_by_slug, client):
    listing = _published_listing()
    listing["status"] = "retired"
    mock_get_by_slug.return_value = listing

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_unknown_slug_404(mock_get_by_slug, client):
    mock_get_by_slug.return_value = None

    resp = client.post("/api/v1/marketplace/listings/no-such-thing/deploy")
    assert resp.status_code == 404


def test_deploy_requires_auth(unauth_client):
    """Without a Clerk JWT, deploy must 401."""
    resp = unauth_client.post("/api/v1/marketplace/listings/anything/deploy")
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_listings._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_entitlement_check_uses_listing_gsi(mock_get_by_slug, mock_get_catalog, mock_boto, mock_purchases, client):
    """Pin the entitlement helper's query shape.

    Regression guard for review I-1: a previous implementation queried the
    buyer_id partition with Limit=200, which silently failed open at scale.
    The fix scopes by listing_id via the listing-created-index GSI. Anyone
    "improving" this helper to a partition-scan-on-buyer (or a full-table
    scan) will trip this assertion.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    # Caller has no purchase row, so this 403s — that's fine for this test;
    # we only care about how the helper queried DynamoDB.
    assert resp.status_code == 403

    mock_purchases.return_value.query.assert_called_once()
    call_kwargs = mock_purchases.return_value.query.call_args.kwargs
    assert call_kwargs["IndexName"] == "listing-created-index"
    assert call_kwargs["KeyConditionExpression"] == "listing_id = :l"
    assert call_kwargs["ExpressionAttributeValues"] == {":l": "lst_1"}
    # Newest first so power buyers' newest purchase row is reached fast.
    assert call_kwargs["ScanIndexForward"] is False


# ---------------------------------------------------------------------------
# Seller-scoped search (drawer's "My Listings" tab) + /me/agents-summary
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client_factory(app):
    """Yield a callable that returns a TestClient authenticated as a given user.

    Mirrors the conftest-level ``client`` fixture pattern but lets the test
    pin the authed user_id (and email) explicitly. The drawer endpoints
    ``/listings/search?seller_id=…`` and ``/me/agents-summary`` derive
    behaviour from the caller's identity, so test cases need to swap users
    rather than always being ``user_test_123``.
    """
    from core.auth import get_current_user, get_optional_user

    created: list[TestClient] = []

    def _make(user_id: str, email: str | None = None) -> TestClient:
        ctx = AuthContext(user_id=user_id, email=email)

        async def _override():
            return ctx

        async def _override_optional():
            return ctx

        app.dependency_overrides[get_current_user] = _override
        app.dependency_overrides[get_optional_user] = _override_optional
        tc = TestClient(app)
        created.append(tc)
        return tc

    try:
        yield _make
    finally:
        app.dependency_overrides.clear()
        for tc in created:
            tc.close()


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_seller_scoped_with_status_in(mock_search, authed_client_factory):
    """Authed seller can read their own listings across statuses."""
    mock_search.return_value = [
        {"listing_id": "l1", "status": "draft", "seller_id": "u_seller", "name": "A"},
    ]
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/listings/search?seller_id=u_seller&status_in=draft,review,published")
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] == "u_seller"
    assert call_kwargs["include_unpublished"] is True
    # Seller-scoped responses can include private drafts — the response
    # MUST NOT be cacheable by a CDN. Otherwise CloudFront could serve
    # one seller's drafts to a different user.
    assert resp.headers["Cache-Control"] == "private, max-age=0, no-store"


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_category_filter_passes_through(mock_search, unauth_client):
    """Browse view category chips wire through to the search service."""
    mock_search.return_value = []
    resp = unauth_client.get("/api/v1/marketplace/listings/search?category=engineering")
    assert resp.status_code == 200
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["category"] == "engineering"


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_seller_scoped_rejects_other_users_unpublished(mock_search, authed_client_factory):
    """status_in is ignored when seller_id != caller — enforces published-only semantics."""
    mock_search.return_value = []
    tc = authed_client_factory("u_attacker", "attacker@example.com")
    resp = tc.get("/api/v1/marketplace/listings/search?seller_id=u_victim&status_in=draft")
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] == "u_victim"
    assert call_kwargs.get("include_unpublished", False) is False


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_anonymous_published_only(mock_search, unauth_client):
    """No auth + no seller_id = today's behavior — include_unpublished stays False."""
    mock_search.return_value = []
    resp = unauth_client.get("/api/v1/marketplace/listings/search?format=openclaw&limit=5")
    assert resp.status_code == 200
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] is None
    assert call_kwargs["include_unpublished"] is False


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_seller_scoped_status_in_filters_results(mock_search, authed_client_factory):
    """status_in narrows the result set to the requested statuses.

    The router pushes ``status_in`` into ``marketplace_search.search`` so the
    filter runs BEFORE the limit slice — otherwise a seller with many
    published rows ahead of a small status (e.g., a single review) loses the
    match to the limit cut. The service does the filtering; the router's
    job is just to parse the comma-separated string into a list.
    """
    mock_search.return_value = [
        {"listing_id": "l1", "status": "draft", "seller_id": "u_seller", "name": "A"},
    ]
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/listings/search?seller_id=u_seller&status_in=draft,published")
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["status_in"] == ["draft", "published"]
    assert call_kwargs["include_unpublished"] is True


@patch("routers.marketplace_listings.marketplace_service.create_draft", new_callable=AsyncMock)
def test_create_listing_persists_category(mock_create, authed_client_factory):
    """Wizard's category selection (Step 4 of the publish flow) must thread
    through to ``marketplace_service.create_draft`` so the listing's DDB row
    carries a ``category`` field — without that, the new browse-by-category
    chip filter never matches the just-published listing.
    """
    mock_create.return_value = {"listing_id": "l1", "slug": "atlas", "version": 1}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings",
        json={
            "slug": "atlas",
            "name": "Atlas",
            "description_md": "x",
            "format": "openclaw",
            "price_cents": 0,
            "tags": [],
            "category": "engineering",
        },
    )
    assert resp.status_code == 200, resp.text
    assert mock_create.call_args.kwargs["category"] == "engineering"


@patch("routers.marketplace_listings.get_workspace")
def test_my_agents_summary_returns_per_agent_counts(mock_workspace_factory, authed_client_factory):
    workspace = mock_workspace_factory.return_value
    workspace.read_openclaw_config.return_value = {
        "agents": {
            "list": [
                {
                    "id": "agent_main",
                    "identity": {"name": "main"},
                    "skills": {"list": [{"name": "s1"}, {"name": "s2"}]},
                    "channels": {"list": [{"name": "slack"}]},
                },
                {
                    "id": "agent_pulse",
                    "identity": {"name": "Pulse"},
                    "skills": {"list": [{"name": "x"}, {"name": "y"}, {"name": "z"}]},
                    "channels": {"list": []},
                },
            ]
        }
    }
    workspace.read_cron_jobs.return_value = [
        {"agent_id": "agent_pulse", "schedule": "0 9 * * *"},
        {"agent_id": "agent_pulse", "schedule": "0 17 * * *"},
        {"agent_id": "agent_main", "schedule": "*/5 * * * *"},
    ]
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_id = {a["agent_id"]: a for a in body["agents"]}
    assert by_id["agent_main"]["skill_count"] == 2
    assert by_id["agent_main"]["channel_count"] == 1
    assert by_id["agent_main"]["cron_count"] == 1
    assert by_id["agent_pulse"]["skill_count"] == 3
    assert by_id["agent_pulse"]["channel_count"] == 0
    assert by_id["agent_pulse"]["cron_count"] == 2


@patch("routers.marketplace_listings.get_workspace")
def test_my_agents_summary_no_config_returns_empty(mock_workspace_factory, authed_client_factory):
    workspace = mock_workspace_factory.return_value
    workspace.read_openclaw_config.return_value = None
    workspace.read_cron_jobs.return_value = []
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agents": []}


def test_my_agents_summary_unauthorized(unauth_client):
    resp = unauth_client.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Friday-publish guard on /listings/{id}/artifact-from-agent
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_efs")
def test_artifact_from_agent_blocks_friday(
    mock_export,
    mock_is_friday,
    authed_client_factory,
):
    """When the picked agent is the owner's Paperclip CEO (Friday), the
    publish endpoint must 400 and never call export_agent_from_efs.
    """
    mock_is_friday.return_value = True
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "friday-uuid-001"},
    )
    assert resp.status_code == 400
    assert "default agent" in resp.json()["detail"]
    mock_export.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_efs")
def test_artifact_from_agent_allows_non_friday(
    mock_export,
    mock_is_friday,
    mock_replace,
    authed_client_factory,
):
    """Non-CEO agent_id passes the guard and proceeds to export + replace."""
    from core.services.catalog_package import CatalogPackage

    mock_is_friday.return_value = False
    mock_export.return_value = CatalogPackage(
        format="openclaw",
        manifest={"name": "x", "agent_id": "custom-1"},
        openclaw_slice={"agent": {"name": "x"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake",
        tarball_contents=["./openclaw.json"],
    )
    mock_replace.return_value = {
        "listing_id": "lst_42",
        "version": 1,
        "manifest_sha256": "abc123",
    }
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1"},
    )
    assert resp.status_code == 200
    mock_export.assert_called_once()
    mock_replace.assert_called_once()


@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_efs")
def test_artifact_from_agent_passes_bundle_selection_through(
    mock_export,
    mock_is_friday,
    mock_replace,
    authed_client_factory,
):
    """The Bundle step's selection (MCPs + skills) must reach export_agent_from_efs
    as keyword args so the slice carries them with secrets scrubbed."""
    from core.services.catalog_package import CatalogPackage

    mock_is_friday.return_value = False
    mock_export.return_value = CatalogPackage(
        format="openclaw",
        manifest={"name": "x", "agent_id": "custom-1"},
        openclaw_slice={"agent": {"name": "x"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake",
        tarball_contents=[],
    )
    mock_replace.return_value = {"listing_id": "lst_42", "version": 1, "manifest_sha256": "x"}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={
            "agent_id": "custom-1",
            "bundle": {"mcps": ["jira-mcp"], "skills": ["search", "jira"]},
        },
    )
    assert resp.status_code == 200
    call_kwargs = mock_export.call_args.kwargs
    assert call_kwargs["selected_mcps"] == ["jira-mcp"]
    assert call_kwargs["selected_skills"] == ["search", "jira"]


# ---------------------------------------------------------------------------
# GET /me/agents/{agent_id}/artifact-preview
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.enumerate_for_preview")
def test_artifact_preview_returns_available_mcps_skills_and_flags(
    mock_enumerate,
    mock_is_friday,
    authed_client_factory,
):
    mock_is_friday.return_value = False
    mock_enumerate.return_value = {
        "agent_id": "custom-1",
        "available_mcps": ["jira-mcp", "slack-mcp"],
        "available_skills": [
            {"name": "search", "bucket": "bundled"},
            {"name": "jira", "bucket": "entries"},
            {"name": "my-skill", "bucket": "workspace"},
        ],
        "persona_files": ["IDENTITY.md", "SOUL.md"],
        "safety_flags": [
            {
                "pattern": "email-address",
                "severity": "medium",
                "file": "USER.md",
                "line": 3,
                "snippet": "Email: alice@example.com",
            }
        ],
    }
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "custom-1"
    assert body["available_mcps"] == ["jira-mcp", "slack-mcp"]
    by_name = {s["name"]: s["bucket"] for s in body["available_skills"]}
    assert by_name["search"] == "bundled"
    assert by_name["jira"] == "entries"
    assert by_name["my-skill"] == "workspace"
    assert "IDENTITY.md" in body["persona_files"]
    assert body["safety_flags"][0]["pattern"] == "email-address"
    # An MCP exists → needs_config_patch flag flips on so the wizard's
    # Review step warns the seller their listing patches the buyer's config.
    assert body["needs_config_patch"] is True


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.enumerate_for_preview")
def test_artifact_preview_needs_config_patch_false_for_pure_workspace_listing(
    mock_enumerate,
    mock_is_friday,
    authed_client_factory,
):
    """A listing that only ships workspace files (no MCPs, no entries/bundled
    skills) doesn't touch the buyer's openclaw.json → no warning."""
    mock_is_friday.return_value = False
    mock_enumerate.return_value = {
        "agent_id": "custom-1",
        "available_mcps": [],
        "available_skills": [{"name": "my-skill", "bucket": "workspace"}],
        "persona_files": ["IDENTITY.md"],
        "safety_flags": [],
    }
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 200
    assert resp.json()["needs_config_patch"] is False


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
def test_artifact_preview_blocks_friday(mock_is_friday, authed_client_factory):
    mock_is_friday.return_value = True
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/friday-uuid/artifact-preview")
    assert resp.status_code == 400
    assert "default agent" in resp.json()["detail"]


def test_artifact_preview_unauthorized(unauth_client):
    resp = unauth_client.get("/api/v1/marketplace/me/agents/x/artifact-preview")
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_listings.get_workspace")
def test_my_agents_summary_handles_array_shape_skills(mock_workspace_factory, authed_client_factory):
    """Real agent configs (e.g. atlas) store skills/channels as plain arrays.

    The endpoint must return correct counts for both array and object shapes
    without raising AttributeError. Codex P2 (PR #567 round 7) flagged that
    ``len((a.get("skills") or {}).get("list", []))`` blows up on non-empty
    array-shaped fields because the ``or`` short-circuit doesn't fire.
    """
    workspace = mock_workspace_factory.return_value
    workspace.read_openclaw_config.return_value = {
        "agents": {
            "list": [
                # Array shape (real-world configs like atlas/openclaw.json)
                {
                    "id": "agent_atlas",
                    "identity": {"name": "Atlas"},
                    "skills": ["s1", "s2", "s3"],
                    "channels": ["slack", "email"],
                },
                # Object shape (older / hand-built configs)
                {
                    "id": "agent_pulse",
                    "identity": {"name": "Pulse"},
                    "skills": {"list": [{"name": "s1"}]},
                    "channels": {"list": []},
                },
                # Missing fields (defensive)
                {
                    "id": "agent_min",
                    "identity": {"name": "Min"},
                },
            ]
        }
    }
    workspace.read_cron_jobs.return_value = []

    client = authed_client_factory("u_seller", "seller@example.com")
    resp = client.get("/api/v1/marketplace/me/agents-summary")
    assert resp.status_code == 200
    body = resp.json()
    by_id = {a["agent_id"]: a for a in body["agents"]}
    # Array shape
    assert by_id["agent_atlas"]["skill_count"] == 3
    assert by_id["agent_atlas"]["channel_count"] == 2
    # Object shape
    assert by_id["agent_pulse"]["skill_count"] == 1
    assert by_id["agent_pulse"]["channel_count"] == 0
    # Missing fields
    assert by_id["agent_min"]["skill_count"] == 0
    assert by_id["agent_min"]["channel_count"] == 0


# ---------------------------------------------------------------------------
# PATCH /listings/{listing_id} (seller edit)
# ---------------------------------------------------------------------------


def test_patch_listing_requires_auth(unauth_client):
    resp = unauth_client.patch(
        "/api/v1/marketplace/listings/lst_42",
        json={"name": "New Name"},
    )
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_listings.marketplace_service.update_draft", new_callable=AsyncMock)
def test_patch_listing_forwards_only_provided_fields(mock_update, authed_client_factory):
    """Wizard's edit mode sends a sparse patch — the route must thread only
    the supplied keys into the service so unspecified fields aren't clobbered."""
    mock_update.return_value = {"listing_id": "lst_42", "name": "New Name"}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.patch(
        "/api/v1/marketplace/listings/lst_42",
        json={"name": "New Name", "price_cents": 500},
    )
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_update.call_args.kwargs
    assert call_kwargs["seller_id"] == "u_seller"
    assert call_kwargs["name"] == "New Name"
    assert call_kwargs["price_cents"] == 500
    # Fields the wizard didn't touch are passed as None — the service's
    # update_draft skips them, leaving the row's existing values intact.
    assert call_kwargs["description_md"] is None
    assert call_kwargs["tags"] is None
    assert call_kwargs["category"] is None


@patch("routers.marketplace_listings.marketplace_service.update_draft", new_callable=AsyncMock)
def test_patch_listing_translates_invalid_state_to_409(mock_update, authed_client_factory):
    """Published / in-review listings reject the patch — surface as 409 so
    the wizard can show a coherent error instead of a 500."""
    from core.services.marketplace_service import InvalidStateError

    mock_update.side_effect = InvalidStateError("listing is not in 'draft' state")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.patch(
        "/api/v1/marketplace/listings/lst_42",
        json={"name": "New Name"},
    )
    assert resp.status_code == 409


def test_patch_listing_rejects_slug_field(authed_client_factory):
    """Slug is immutable post-create (reserved via a sentinel row). The
    ListingUpdate schema doesn't include `slug`, so FastAPI silently drops
    it — but the test pins that behavior so a future refactor can't
    accidentally surface it.
    """
    tc = authed_client_factory("u_seller", "seller@example.com")
    # Patch payloads carrying an unknown `slug` field must still validate
    # (Pydantic's default is to ignore extras), and the slug must NOT be
    # forwarded to the service. Test by mocking the service and asserting
    # `slug` isn't in the call.
    with patch(
        "routers.marketplace_listings.marketplace_service.update_draft",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_update.return_value = {"listing_id": "lst_42"}
        resp = tc.patch(
            "/api/v1/marketplace/listings/lst_42",
            json={"name": "Renamed", "slug": "new-slug"},
        )
        assert resp.status_code == 200
        assert "slug" not in mock_update.call_args.kwargs
