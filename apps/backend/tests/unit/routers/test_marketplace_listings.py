"""Tests for marketplace_listings router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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
    mock_search.assert_called_once_with(query="foo", format="openclaw", limit=10)
    # Cache header is set so CloudFront / browser respect the snapshot TTL.
    assert resp.headers["Cache-Control"] == "public, max-age=60"


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_endpoint_works_without_query(mock_search, unauth_client):
    mock_search.return_value = []
    resp = unauth_client.get("/api/v1/marketplace/listings/search")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}
    mock_search.assert_called_once_with(query=None, format=None, limit=24)


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
