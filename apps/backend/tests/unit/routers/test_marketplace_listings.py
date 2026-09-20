"""Tests for marketplace_listings router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from core.services.hermes_provisioning import HermesProfileError  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_deploy_claim():
    """Keep the marketplace-deploy claim off the network for every test here.

    ``container_repo`` resolves its table through ``core.dynamodb.get_table`` —
    a DIFFERENT boto3 handle from the ``boto3.client`` these tests patch — so a
    test that drives ``/deploy`` without stubbing the claim makes a REAL
    DynamoDB call and only "passes" by riding ``try_claim_owner_deploy``'s
    fail-open branch (verified: it logs ``DDB fault
    (ResourceNotFoundException) … admitting the deploy UNFENCED``). That made
    two tests quietly depend on outbound AWS access, and meant neither was
    exercising the path it claimed to.

    Autouse so a future ``/deploy`` test can't reintroduce it. Tests needing a
    specific claim outcome still override with their own ``@patch`` — a
    decorator patch is applied after this fixture, so it wins for that test. The
    real conditional-write semantics are covered against a live DynamoDB engine
    in ``tests/unit/repositories/test_container_repo_deploy_claim.py``.
    """
    with (
        patch(
            "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
            new_callable=AsyncMock,
            return_value=("test-claim-token", None),
        ),
        patch(
            "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
            new_callable=AsyncMock,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _stub_provisioned_container():
    """A provisioned workspace row for every test here.

    The deploy route reads the container row and refuses (503) any row without an
    ``agentcore_runtime_arn`` -- the install writes into that workspace's Hermes profiles, so a
    row with no runtime has nowhere to write. Same posture as ``_stub_deploy_claim``: the repo
    builds its own boto3 handle, so the ``boto3.client`` patches in these tests do not cover it,
    and an unstubbed read makes a real DynamoDB call. Tests about the refusal override with their
    own ``@patch`` (a decorator patch is applied after this fixture, so it wins).
    """
    with patch(
        "routers.marketplace_listings.container_repo.get_by_owner_id",
        new_callable=AsyncMock,
        return_value={
            "owner_id": "org_test_456",
            "status": "running",
            "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
        },
    ) as m:
        yield m


@pytest.fixture(autouse=True)
def _stub_listing_install_counter():
    """Keep the listing-row install_count bump off the network for every test
    here — same posture as ``_stub_deploy_claim`` above (marketplace_repo
    builds its own boto3 handle, so the ``boto3.client`` patches don't cover
    it). Tests asserting the bump request this fixture by name."""
    with patch(
        "core.repositories.marketplace_repo.increment_listing_install_count",
        new_callable=AsyncMock,
    ) as m:
        yield m


@pytest.fixture(autouse=True)
def _stub_marketplace_marker():
    with patch("routers.marketplace_listings.write_marketplace_provenance", new_callable=AsyncMock):
        yield


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


@patch("routers.marketplace_listings.marketplace_service.create_draft", new_callable=AsyncMock)
@patch("routers.marketplace_listings.seller_profile.get_profile", new_callable=AsyncMock)
def test_create_draft_requires_seller_profile(mock_profile, mock_create):
    """Authenticated callers without a marketplace profile get 409
    seller_profile_required — the wizard uses this to route to the
    handle picker before retrying."""
    from main import app
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="human_caller", org_id="user_caller", org_role="org:admin"
    )
    try:
        mock_profile.return_value = None
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.post(
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
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json()["detail"] == "seller_profile_required"
    mock_create.assert_not_called()


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_endpoint_passes_through_to_service(mock_search, unauth_client):
    mock_search.return_value = [{"slug": "foo", "name": "Foo"}]
    resp = unauth_client.get("/api/v1/marketplace/listings/search?q=foo&format=openclaw&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    # seller_display.attach_seller_names sets the byline to FALLBACK_NAME
    # ("Community member") on items without a seller_id, so the storefront
    # never sees a missing byline.
    assert body == {"items": [{"slug": "foo", "name": "Foo", "seller_display_name": "Community member"}]}
    mock_search.assert_called_once_with(
        query="foo",
        format="openclaw",
        limit=10,
        seller_id=None,
        category=None,
        free_only=False,
        viewer_id=None,
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
        free_only=False,
        viewer_id=None,
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
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_returns_listing_and_manifest(mock_get_published, mock_boto, unauth_client):
    """The new shape is ``{listing, manifest}``; manifest is fetched from S3."""
    mock_get_published.return_value = _published_listing(price_cents=0)
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

    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
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
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_when_s3_fails(mock_get_published, mock_boto, unauth_client):
    """S3 failures must NOT 500 the listing endpoint — manifest falls back to None."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")
    mock_boto.return_value = s3

    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_for_malformed_json(mock_get_published, mock_boto, unauth_client):
    """Malformed JSON in S3 must NOT 500 the listing endpoint — manifest falls back to None.

    Pins the json.JSONDecodeError branch of the narrowed exception catch
    (review M1). Previously a broad `except Exception` swallowed this; the
    narrowed catch keeps the same user-visible behaviour while letting
    programming errors propagate.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    # Body bytes that are not valid JSON.
    mock_boto.return_value = _stub_s3_get_manifest(b"not json at all")

    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_returns_null_manifest_for_non_dict_body(mock_get_published, mock_boto, unauth_client):
    """Valid JSON that isn't an object must also fall back to manifest=None.

    The router has an explicit `if not isinstance(manifest, dict): manifest = None`
    guard. This test pins it so a future refactor that drops the guard will
    surface a string/list/scalar as a manifest and trip this assertion.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    # Valid JSON, but a list — not a dict. The router guard converts to None.
    mock_boto.return_value = _stub_s3_get_manifest(b"[1, 2, 3]")

    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["manifest"] is None
    assert body["listing"]["slug"] == "demo-agent"


@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_404_for_taken_down(mock_get_published, unauth_client):
    listing = _published_listing()
    listing["status"] = "taken_down"
    mock_get_published.return_value = listing
    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["draft", "review", "retired"])
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_404_for_non_published_states(mock_get_published, unauth_client, status):
    """Regression: only published listings are publicly visible. A blacklist
    approach that allows draft + review through leaks unmoderated content
    if a slug is guessed.
    """
    listing = _published_listing()
    listing["status"] = status
    mock_get_published.return_value = listing
    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_404_for_unknown_id(mock_get_published, unauth_client):
    mock_get_published.return_value = None
    resp = unauth_client.get("/api/v1/marketplace/listings/lst_unknown")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Safety-scan snippet leak (issue #1778)
#
# ``safety_flags[].snippet`` is 80 raw chars of the seller's private workspace
# — by construction the field most likely to hold a credential. This route is
# UNAUTHENTICATED and sets ``Cache-Control: public``. The write path no longer
# persists snippets, but published artifacts are immutable, so the read path
# must scrub what is already at rest in S3 + on the listing row.
# ---------------------------------------------------------------------------

_LEAKY_FLAGS = [
    {"pattern": "aws-secret-key", "severity": "high", "file": "MEMORY.md", "line": 4, "snippet": "AKIAIOSFODNN7X"},
    {"pattern": "email-address", "severity": "medium", "file": "SOUL.md", "line": 19, "snippet": "a@example.com"},
]


@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_get_listing_strips_snippets_from_both_manifest_copies(mock_get_published, mock_boto, unauth_client):
    listing = _published_listing(price_cents=0)
    listing["manifest_json"] = {"name": "Demo Agent", "safety_flags": [dict(f) for f in _LEAKY_FLAGS]}
    mock_get_published.return_value = listing
    mock_boto.return_value = _stub_s3_get_manifest({"name": "Demo Agent", "safety_flags": _LEAKY_FLAGS})

    resp = unauth_client.get("/api/v1/marketplace/listings/lst_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Nothing snippet-shaped survives anywhere in the response…
    assert "AKIAIOSFODNN7X" not in resp.text
    assert "a@example.com" not in resp.text
    # …in either copy of the manifest…
    for flags in (body["manifest"]["safety_flags"], body["listing"]["manifest_json"]["safety_flags"]):
        assert all("snippet" not in f for f in flags), flags
        # …while pattern/severity/file/line still render the moderation summary.
        assert [f["pattern"] for f in flags] == ["aws-secret-key", "email-address"]
        assert flags[0]["file"] == "MEMORY.md" and flags[0]["line"] == 4


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_strips_snippets_from_manifest_json(mock_search, unauth_client):
    """The default (non-compact) search view returns whole rows, ``manifest_json``
    included, under the same public cache header."""
    mock_search.return_value = [
        {"slug": "foo", "name": "Foo", "manifest_json": {"safety_flags": [dict(f) for f in _LEAKY_FLAGS]}}
    ]
    resp = unauth_client.get("/api/v1/marketplace/listings/search")
    assert resp.status_code == 200, resp.text
    assert "AKIAIOSFODNN7X" not in resp.text
    flags = resp.json()["items"][0]["manifest_json"]["safety_flags"]
    assert all("snippet" not in f for f in flags), flags


@patch("routers.marketplace_listings.curator.get_curator_user_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_featured_listings_strips_snippets_from_manifest_json(mock_search, mock_curator, unauth_client):
    """Same rows, same public cache header, different route."""
    mock_curator.return_value = "user_curator"
    mock_search.return_value = [
        {"slug": "foo", "name": "Foo", "manifest_json": {"safety_flags": [dict(f) for f in _LEAKY_FLAGS]}}
    ]
    resp = unauth_client.get("/api/v1/marketplace/featured-listings")
    assert resp.status_code == 200, resp.text
    assert "AKIAIOSFODNN7X" not in resp.text
    flags = resp.json()["items"][0]["manifest_json"]["safety_flags"]
    assert all("snippet" not in f for f in flags), flags


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


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_free_listing_succeeds(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, client, monkeypatch
):
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_abc123", "name": "Demo Agent"}
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
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
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
    assert kwargs["listing_id"] == "lst_1"
    assert "idempotency_replay_only" not in mock_create_pc.await_args.kwargs


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_threads_the_resolved_container_through_to_deploy_from_artifact(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, client, monkeypatch, _stub_provisioned_container
):
    """Fix round 1, item 1: the container row is already read right above the
    ``deploy_from_artifact`` call, so ``deploy_listing`` threads it through rather than making the
    install read it a second time (and risk a different answer mid-deploy)."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_abc123", "name": "Demo Agent"}
    agentcore_container = {
        "owner_id": "org_test_456",
        "status": "running",
        "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
    }
    _stub_provisioned_container.return_value = agentcore_container
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {}, "cron_jobs": []})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={"slug": "demo-agent", "version": 1, "agent_id": "agent_abc123", "name": "Demo Agent"}
    )
    mock_get_catalog.return_value = catalog
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert kwargs["container"] == agentcore_container


@patch("routers.marketplace_listings._recreate_routines_for_buyer", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_recreates_captured_routines_remapped_to_buyer_agent(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_recreate, client
):
    """A listing whose manifest carries ``routines`` recreates them on the
    buyer, remapped to the freshly-minted agent id (and DORMANT — the helper
    always emits enabled:false). Here we assert the deploy hook wires the
    manifest routines + minted id into the recreate helper."""
    listing = _published_listing(price_cents=0)
    listing["manifest_json"] = {
        "name": "Demo Agent",
        "format": "openclaw",
        "routines": [
            {"title": "Morning sweep", "description": "do it", "cron": "0 6 * * *", "timezone": "UTC"},
            {"title": "Weekly report", "description": "wk", "cron": "0 8 * * 1", "timezone": "UTC"},
        ],
    }
    mock_get_published.return_value = listing
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {"name": "Demo Agent"}, "cron_jobs": []})
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "paperclip-uuid-1234",
            "name": "Demo Agent",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text

    mock_recreate.assert_awaited_once()
    rk = mock_recreate.await_args.kwargs
    # Remapped to the minted buyer agent.
    assert rk["new_agent_id"] == "paperclip-uuid-1234"
    # The captured routines flow through verbatim.
    assert [r["title"] for r in rk["manifest_routines"]] == ["Morning sweep", "Weekly report"]


def _catalog_stub(agent_id: str = "paperclip-uuid-1234"):
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": agent_id,
            "name": "Demo Agent",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    return catalog


@patch("routers.marketplace_listings._recreate_routines_for_buyer", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_returns_the_webhook_automations_the_buyer_must_wire(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_recreate, client
):
    """Webhook automations bind a SELLER-specific resource, so the deploy arms
    nothing and hands the buyer the to-do list instead. The response field is
    what the hire toast counts — without it the agent arrives silently
    incomplete."""
    listing = _published_listing(price_cents=0)
    listing["manifest_json"] = {
        "name": "Demo Agent",
        "format": "openclaw",
        "routines": [
            {"title": "Nightly", "cron": "0 6 * * *"},
            {
                "title": "On failed invoice",
                "kind": "webhook",
                "provider": "stripe",
                "event_id": "invoice.payment_failed",
            },
        ],
    }
    mock_get_published.return_value = listing
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {"name": "Demo Agent"}, "cron_jobs": []})
    mock_get_catalog.return_value = _catalog_stub()

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["automations_pending"] == [
        {"provider": "stripe", "event_id": "invoice.payment_failed", "needs_resource": False}
    ]


@patch("routers.marketplace_listings.emit_agent_deployed")
@patch("routers.marketplace_listings._recreate_routines_for_buyer", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_emits_the_funnel_event_after_the_commit_steps(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_recreate, mock_emit, client, monkeypatch
):
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {"name": "Demo Agent"}, "cron_jobs": []})
    mock_get_catalog.return_value = _catalog_stub()
    order = []

    async def marker(**_kwargs):
        order.append("marker")

    async def routines(**_kwargs):
        order.append("routines")

    mock_recreate.side_effect = routines
    monkeypatch.setattr("routers.marketplace_listings.write_marketplace_provenance", marker)

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    mock_emit.assert_called_once()
    assert mock_emit.call_args.kwargs["agent_id"] == "paperclip-uuid-1234"
    assert mock_emit.call_args.kwargs["slug"] == "demo-agent"
    assert order == ["routines", "marker"]


@patch("routers.marketplace_listings.emit_agent_deployed")
@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings._recreate_routines_for_buyer", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_does_not_report_deployed_when_a_commit_step_fails(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_recreate,
    mock_delete_pc,
    mock_emit,
    client,
    monkeypatch,
):
    """The routine recreate runs INSIDE the rollback envelope: a failure there
    unwinds the container install and deletes the Paperclip agent. A hire that
    was rolled back is not a deployment, so the funnel event must not fire."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {"name": "Demo Agent"}, "cron_jobs": []})
    catalog = _catalog_stub()
    mock_get_catalog.return_value = catalog
    mock_recreate.side_effect = RuntimeError("no paperclip company for owner")
    marker = AsyncMock()
    monkeypatch.setattr("routers.marketplace_listings.write_marketplace_provenance", marker)

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 500
    mock_emit.assert_not_called()
    marker.assert_not_awaited()


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_legacy_listing_without_slice_falls_back(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, client
):
    """Listings published before slice-synthesis shipped have no
    openclaw-slice.json key. The deploy must tolerate NoSuchKey on the
    slice fetch and fall through to the no-config-registration path
    (matching the legacy behavior).
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_legacy_1", "name": "Demo Agent"}
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

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert kwargs["slice_"] == {}
    assert resp.json()["config_registered"] is False


@patch(
    "routers.marketplace_listings.container_repo.get_by_owner_id",
    new_callable=AsyncMock,
)
@patch(
    "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
    new_callable=AsyncMock,
)
@patch(
    "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_409s_deploy_busy_when_a_different_listing_holds_the_claim(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_claim,
    mock_release,
    mock_get_container,
    client,
):
    """Overlapping deploys must not both mint an agent.

    The sidecar read can't catch this — it is a read, not a lock, and deploy #2
    can start before #1 has extracted anything, so #2 sees "not installed" while
    #1 is still running. That is how prod grew "Pulse 2". A conditional write on the
    per-owner containers row fences it across ECS tasks — owner-wide now, so
    this also covers the case that used to slip through: two DIFFERENT
    listings hired seconds apart.

    DIFFERENT listing holds the claim here (held_listing_id != the requested
    lst_1) — retrying after a beat is the correct, necessary advice, so this
    is the RETRYABLE deploy_busy shape, not deploy_in_progress (see the
    same-listing test below for the opposite case).
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    catalog.deploy_from_artifact = AsyncMock()
    mock_get_catalog.return_value = catalog
    mock_boto.return_value = _stub_s3_get_object()
    mock_claim.return_value = (None, "lst_other")  # a DIFFERENT listing holds it
    mock_get_container.return_value = {"owner_id": "user_test"}  # container exists

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {"error": "deploy_busy", "retry_after_s": 20}
    # Refused BEFORE the mint — that's the whole point.
    mock_create_pc.assert_not_awaited()
    catalog.deploy_from_artifact.assert_not_awaited()
    # Never took the claim, so it must not release someone else's.
    mock_release.assert_not_awaited()


@patch(
    "routers.marketplace_listings.container_repo.get_by_owner_id",
    new_callable=AsyncMock,
)
@patch(
    "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
    new_callable=AsyncMock,
)
@patch(
    "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_409s_deploy_in_progress_when_the_same_listing_holds_the_claim(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_claim,
    mock_release,
    mock_get_container,
    client,
):
    """THE INVERSE case, and the one that matters most: the buyer's OWN hire of
    THIS listing is still running. Retrying is exactly what mints a duplicate
    agent — the original "Pulse 2" bug. This must be the non-retryable
    deploy_in_progress shape, never deploy_busy + retry_after_s.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    catalog.deploy_from_artifact = AsyncMock()
    mock_get_catalog.return_value = catalog
    mock_boto.return_value = _stub_s3_get_object()
    mock_claim.return_value = (None, "lst_1")  # the SAME listing holds it
    mock_get_container.return_value = {"owner_id": "user_test"}  # container exists

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {"error": "deploy_in_progress"}
    # Refused BEFORE the mint — that's the whole point.
    mock_create_pc.assert_not_awaited()
    catalog.deploy_from_artifact.assert_not_awaited()
    # Never took the claim, so it must not release someone else's.
    mock_release.assert_not_awaited()


@patch(
    "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
    new_callable=AsyncMock,
)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_releases_the_claim_even_when_the_paperclip_mint_fails(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_release, client
):
    """A leaked claim would wedge the listing for DEPLOY_CLAIM_STALE_S.

    The mint raises before the rollback-wrapped block, so the release has to sit
    in a ``finally`` that spans it — not inside the inner try/except.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    mock_get_catalog.return_value = catalog
    mock_boto.return_value = _stub_s3_get_object()
    mock_create_pc.side_effect = RuntimeError("paperclip down")

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 503, resp.text
    mock_release.assert_awaited_once()
    assert mock_release.await_args.kwargs["listing_id"] == "lst_1"
    # Fenced release: the token from the claim must be threaded back, so a
    # taken-over straggler can't delete its successor's live claim.
    assert mock_release.await_args.kwargs["token"]


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_success_bumps_listing_install_count(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, client, _stub_listing_install_counter
):
    """The seller dashboard reads install_count off the LISTING row; a deploy
    that never writes it leaves the dashboard at 0 forever."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_abc123", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(_slice={"agent": {"name": "Demo Agent"}})
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    catalog.deploy_from_artifact = AsyncMock(
        return_value={"slug": "demo-agent", "version": 1, "agent_id": "agent_abc123", "name": "Demo Agent"}
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    _stub_listing_install_counter.assert_awaited_once_with(listing_id="lst_1", version=1)


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_failure_does_not_bump_install_count(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_delete_pc,
    client,
    _stub_listing_install_counter,
):
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_abc123", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object()
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("efs exploded"))
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 500
    _stub_listing_install_counter.assert_not_awaited()


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_maps_a_failed_hermes_profile_to_503_not_500(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_delete_pc,
    client,
    _stub_listing_install_counter,
):
    """Fix round 2, gap 1 (M6-R11) / round 3, M6-R13: _build_and_upload_agent_profile's
    hard failures (missing BYO key, failed Paperclip mint) must reach the
    buyer as a retryable 503, not the blanket 500 every other unexpected
    deploy_from_artifact failure gets -- discriminated by the typed
    HermesProfileError, not a bare RuntimeError (which also legitimately
    surfaces from create_paperclip_for_marketplace / deploy_tarball_to_profile /
    ensure_operator_device_paired in the same try block and must keep
    mapping to 500 -- see test_deploy_failure_does_not_bump_install_count
    above, which asserts exactly that with a plain RuntimeError)."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent_abc123", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object()
    catalog = MagicMock()
    catalog.list_deployed_for_user.return_value = []
    catalog.deploy_from_artifact = AsyncMock(
        side_effect=HermesProfileError("hermes profile for owner=owner_1 agent=agent_abc123: byo api key read failed")
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 503, resp.text
    _stub_listing_install_counter.assert_not_awaited()
    mock_delete_pc.assert_awaited_once()


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_paid_listing_without_purchase_403(
    mock_get_published, mock_get_catalog, mock_boto, mock_purchases, client
):
    mock_get_published.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 403
    # We never reached S3 / catalog — entitlement gate hit first.
    mock_boto.assert_not_called()
    mock_get_catalog.assert_not_called()


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_paid_listing_seller_bypass_succeeds(
    mock_get_published, mock_get_catalog, mock_boto, mock_purchases, mock_create_pc, client
):
    """Regression: a seller deploying their own paid listing must bypass
    the entitlement check. Without the bypass, sellers are 403'd by their
    own listings — surfaced as an indefinite "Setting up the workspace…"
    spinner.
    """
    listing = _published_listing(price_cents=1000)
    # ``client`` fixture authenticates as user_test_123 — match seller_id
    # to that so the bypass fires.
    listing["seller_id"] = "user_test_123"
    mock_get_published.return_value = listing
    mock_create_pc.return_value = {"id": "agent_self_deployed", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo Agent"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "agent_self_deployed",
            "name": "Demo Agent",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_uuid"] == "agent_self_deployed"
    # Entitlement helper was never called — bypass kicked in BEFORE the
    # purchases-table lookup, so even sellers with no purchase row deploy.
    mock_purchases.return_value.query.assert_not_called()


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_paid_listing_with_revoked_license_403(
    mock_get_published, mock_get_catalog, mock_boto, mock_purchases, client
):
    mock_get_published.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {
        "Items": [
            {
                "buyer_id": "org_test_456",
                "purchase_id": "p1",
                "listing_id": "lst_1",
                "license_key_revoked": True,
                "license_key_revoked_reason": "refunded",
            }
        ]
    }

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 403
    mock_get_catalog.assert_not_called()


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_paid_listing_with_valid_purchase_succeeds(
    mock_get_published, mock_get_catalog, mock_boto, mock_purchases, mock_create_pc, client
):
    mock_get_published.return_value = _published_listing(price_cents=2000)
    mock_create_pc.return_value = {"id": "agent_xyz789", "name": "Demo Agent"}
    mock_purchases.return_value.query.return_value = {
        "Items": [
            {
                "buyer_id": "org_test_456",
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

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200
    assert resp.json()["agent_uuid"] == "agent_xyz789"


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_mints_paperclip_agent_first_and_threads_uuid_to_catalog(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """Spec change #4: marketplace deploy must call Paperclip's
    create_agent BEFORE invoking the catalog service, and pass Paperclip's
    UUID as ``new_agent_id`` so OpenClaw and Paperclip share the agent id.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo"}
    real_slice = {
        "agent": {"name": "Demo", "role": "general"},
        "plugins": {},
        "tools": {},
        "cron_jobs": [],
    }
    mock_boto.return_value = _stub_s3_get_object(_slice=real_slice)
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "paperclip-uuid-1234",
            "name": "Demo",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 200, resp.text

    # Paperclip mint was called first with the slice's agent name. Role
    # now lives on the ``meta`` kwarg (PaperclipMeta) — see T5 of the
    # paperclip-meta roundtrip plan; the top-level ``role`` kwarg is
    # passed as ``None`` so meta.role drives, matching the chat path.
    mock_create_pc.assert_awaited_once()
    pc_kwargs = mock_create_pc.await_args.kwargs
    assert pc_kwargs["name"] == "Demo"
    assert pc_kwargs["role"] is None
    assert pc_kwargs["meta"] is not None
    assert pc_kwargs["meta"].role == "general"

    # Catalog deploy received the Paperclip-minted UUID.
    catalog.deploy_from_artifact.assert_awaited_once()
    deploy_kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert deploy_kwargs["new_agent_id"] == "paperclip-uuid-1234"

    # No body → reports_to defaults to None (unparented), backward-compatible.
    assert pc_kwargs["reports_to"] is None

    # Rollback NOT called on success.
    mock_delete_pc.assert_not_awaited()


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_threads_reports_to_into_paperclip_create(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """Org placement: the buyer's chosen manager (reports_to) in the deploy
    body is threaded into create_paperclip_for_marketplace so the new hire
    lands under that agent in the Paperclip org chart."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo", "role": "general"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "paperclip-uuid-1234",
            "name": "Demo",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post(
        "/api/v1/marketplace/listings/lst_1/deploy",
        json={"reports_to": "friday-agent-id"},
    )
    assert resp.status_code == 200, resp.text
    assert mock_create_pc.await_args.kwargs["reports_to"] == "friday-agent-id"
    assert mock_create_pc.await_args.kwargs["idempotency_key"].startswith(
        "lightbulb-agent-create:v1:marketplace-deploy:"
    )


def test_deploy_request_blank_reports_to_coerced_to_none():
    """Paperclip's reportsTo rejects "" (accepts null) — the schema coerces a
    blank string to None so a top-level hire never trips Paperclip validation."""
    from schemas.marketplace import DeployListingRequest

    assert DeployListingRequest(reports_to="").reports_to is None
    assert DeployListingRequest(reports_to="   ").reports_to is None
    assert DeployListingRequest(reports_to="agent-x").reports_to == "agent-x"
    assert DeployListingRequest().reports_to is None


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_rolls_back_paperclip_on_catalog_failure(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """If Paperclip create succeeds but catalog deploy fails (e.g. the
    gateway RPC raises or the aux merge errors), delete the orphan
    Paperclip agent so a retry doesn't leave a phantom Paperclip record.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("catalog boom"))
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 500, resp.text
    mock_delete_pc.assert_awaited_once()
    delete_kwargs = mock_delete_pc.await_args.kwargs
    assert delete_kwargs["agent_id"] == "paperclip-uuid-1234"


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_timeout_keeps_both_sides_so_the_half_install_is_disposable(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """A TimeoutError means 'no answer yet', not 'failed' — the runtime may still be
    finishing the profile write, and the Paperclip agent may already exist and work.

    So the install is left standing, and the Paperclip row is left standing WITH it. Deleting
    only the row is what produced an unownable ghost: a live profile plus (when the create
    landed and only the follow-up timed out) a runtime-side agent, with no row left for the
    owner to see or delete it by, and nothing that would ever clean it up. Keeping the row makes
    it an ordinary agent in Teams — usable or deletable — and the retry resolves to it instead
    of minting a duplicate beside it.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-timeout", "name": "Echo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Echo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")

    # Still surfaced as an error to the caller — a timeout is not success.
    assert resp.status_code == 500, resp.text
    # The load-bearing assertion: the Paperclip row stays, so the half-install has an owner and
    # a disposal story.
    mock_delete_pc.assert_not_awaited()


@patch("routers.marketplace_listings.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_timeout_holds_the_owner_claim_instead_of_freeing_it(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, mock_release, client
):
    """The claim must not become reusable while the timed-out call may still be running.

    The install is left standing on a timeout precisely because the runtime may still be
    writing it; a ``finally`` that freed the owner-wide claim in the same breath let the retry
    take it and run a SECOND install concurrently with the first — the overlap the claim exists
    to prevent, reintroduced in the one case the branch was written for. The cause rides into
    the release, which holds the claim and lets the lease settle it (see the repo test for that
    half).
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-timeout", "name": "Echo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Echo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    mock_get_catalog.return_value = catalog

    assert client.post("/api/v1/marketplace/listings/lst_1/deploy").status_code == 500

    mock_release.assert_awaited_once()
    assert isinstance(mock_release.await_args.kwargs["cause"], TimeoutError)


@patch("routers.marketplace_listings.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_non_timeout_failure_frees_the_owner_claim_at_once(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, mock_release, client
):
    """The other half, and the reason the hold is scoped to ``TimeoutError``:
    an ordinary failed hire must free the owner immediately, or one 500 wedges
    every hire they try for the lease's full 600s.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-failed", "name": "Echo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Echo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("gateway rejected schema"))
    mock_get_catalog.return_value = catalog

    assert client.post("/api/v1/marketplace/listings/lst_1/deploy").status_code == 500

    mock_release.assert_awaited_once()
    assert not isinstance(mock_release.await_args.kwargs.get("cause"), TimeoutError)


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_non_timeout_error_removes_the_paperclip_row(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """The other side of the carve-out: an ordinary failure DOES remove the row, so a retry does
    not accumulate phantom Paperclip records beside the one it mints."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-nontimeout", "name": "Echo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Echo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("gateway rejected schema"))
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")

    assert resp.status_code == 500, resp.text
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "paperclip-uuid-nontimeout"


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_503s_when_paperclip_unavailable(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    client,
):
    """If Paperclip's admin API fails, surface 503 immediately and never
    touch the catalog. No orphan Paperclip record to clean up because
    no Paperclip agent was created.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    mock_create_pc.side_effect = RuntimeError("paperclip 502")
    catalog = MagicMock()
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 503, resp.text
    # Catalog never invoked.
    catalog.deploy_from_artifact.assert_not_called()


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_503s_for_a_workspace_with_no_runtime_and_rolls_back_paperclip(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client, _stub_provisioned_container
):
    """The install writes into the workspace's Hermes profiles, so a row with no
    ``agentcore_runtime_arn`` has nowhere to write -- it is mid-provision, not broken. The router
    must 503 (retryable) and roll back the orphan Paperclip agent it just minted, or a retry
    accumulates phantom Paperclip records.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-9999", "name": "Demo"}
    _stub_provisioned_container.return_value = {"owner_id": "org_test_456", "status": "provisioning"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 503, resp.text
    # Never invoked — we short-circuited on the row, before any profile write.
    catalog.deploy_from_artifact.assert_not_awaited()
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "paperclip-uuid-9999"


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_rolls_back_paperclip_when_the_container_row_read_raises(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client, _stub_provisioned_container
):
    """The row read can RAISE on a transient DynamoDB fault, not just answer "not ready".
    Without the post-mint rollback envelope that exception bubbled into a 500 while silently
    leaving an orphan Paperclip agent -- the rollback has to fire on ANY post-mint path.
    """
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-9999", "name": "Demo"}
    _stub_provisioned_container.side_effect = RuntimeError("ddb throttled")
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 500, resp.text
    catalog.deploy_from_artifact.assert_not_awaited()
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "paperclip-uuid-9999"


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_passes_paperclip_meta_from_slice(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """When the slice's agent entry has ``params.paperclip``, the deploy
    forwards a PaperclipMeta with those values to
    ``create_paperclip_for_marketplace``. Sellers who explicitly tagged
    their agent with a role/title/capabilities get those values through
    to the buyer's Paperclip profile."""
    mock_get_published.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "agent-uuid-1234", "name": "Ray"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={
            "agent": {
                "name": "Ray",
                "params": {
                    "paperclip": {
                        "role": "engineer",
                        "title": "Restaurant booking specialist",
                        "capabilities": "Books reservations on Resy.",
                    }
                },
            },
            "plugins": {},
            "tools": {},
            "cron_jobs": [],
        }
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "agent-uuid-1234"})
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")

    assert resp.status_code == 200, resp.text
    mock_create_pc.assert_awaited_once()
    kwargs = mock_create_pc.await_args.kwargs
    # meta carried the seller's tags
    assert kwargs["meta"] is not None
    assert kwargs["meta"].role == "engineer"
    assert kwargs["meta"].title == "Restaurant booking specialist"
    assert kwargs["meta"].capabilities == "Books reservations on Resy."


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_derives_paperclip_meta_from_listing_when_slice_missing_block(
    mock_get_published, mock_get_catalog, mock_boto, mock_create_pc, mock_delete_pc, client
):
    """Listings published before this feature shipped have no
    ``params.paperclip`` in the slice. Deploy falls back to
    ``derive_from_listing`` — title=listing.name,
    capabilities=listing.description_md, role="general"."""
    listing = _published_listing(price_cents=0)
    # Override with a clearly recognizable description so we can assert on it.
    listing["description_md"] = "Books Resy reservations. Knows my taste."
    listing["name"] = "Ray"
    mock_get_published.return_value = listing
    mock_create_pc.return_value = {"id": "agent-uuid-2", "name": "Ray"}
    # Slice has agent block but NO params.paperclip
    mock_boto.return_value = _stub_s3_get_object(
        _slice={
            "agent": {"name": "Ray"},
            "plugins": {},
            "tools": {},
            "cron_jobs": [],
        }
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "agent-uuid-2"})
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")

    assert resp.status_code == 200, resp.text
    kwargs = mock_create_pc.await_args.kwargs
    assert kwargs["meta"] is not None
    # role defaults to "general" — no signal to pick something else
    assert kwargs["meta"].role == "general"
    # derived from listing fields
    assert kwargs["meta"].title == "Ray"
    assert kwargs["meta"].capabilities == "Books Resy reservations. Knows my taste."


@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_taken_down_listing_404(mock_get_published, client):
    listing = _published_listing()
    listing["status"] = "taken_down"
    mock_get_published.return_value = listing

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_retired_listing_404(mock_get_published, client):
    listing = _published_listing()
    listing["status"] = "retired"
    mock_get_published.return_value = listing

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 404


@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_unknown_id_404(mock_get_published, client):
    mock_get_published.return_value = None

    resp = client.post("/api/v1/marketplace/listings/lst_unknown/deploy")
    assert resp.status_code == 404


def test_deploy_requires_auth(unauth_client):
    """Without a Clerk JWT, deploy must 401."""
    resp = unauth_client.post("/api/v1/marketplace/listings/lst_unknown/deploy")
    assert resp.status_code in (401, 403)


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_entitlement_check_uses_listing_gsi(mock_get_published, mock_get_catalog, mock_boto, mock_purchases, client):
    """Pin the entitlement helper's query shape.

    Regression guard for review I-1: a previous implementation queried the
    buyer_id partition with Limit=200, which silently failed open at scale.
    The fix scopes by listing_id via the listing-created-index GSI. Anyone
    "improving" this helper to a partition-scan-on-buyer (or a full-table
    scan) will trip this assertion.
    """
    mock_get_published.return_value = _published_listing(price_cents=2000)
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")
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
        ctx = AuthContext(user_id=f"human_{user_id}", org_id=user_id, org_role="org:admin", email=email)

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
    resp = tc.get("/api/v1/marketplace/listings/search?seller_id=human_u_seller&status_in=draft,review,published")
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["seller_id"] == "human_u_seller"
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
def test_search_free_only_passes_through(mock_search, unauth_client):
    """The mobile marketplace's free-only browse wires through to the service.

    It has to be pushed down rather than filtered client-side: the service
    applies it before the ranked limit slice, so free listings below a window
    full of paid ones still surface. Default stays off for every other caller.
    """
    mock_search.return_value = []
    resp = unauth_client.get("/api/v1/marketplace/listings/search?free_only=true")
    assert resp.status_code == 200
    assert mock_search.call_args.kwargs["free_only"] is True
    # Anonymous: nobody's own listings to spare, so the body is URL-derived
    # and a shared cache may hold it.
    assert mock_search.call_args.kwargs["viewer_id"] is None
    assert resp.headers["Cache-Control"] == "public, max-age=60"

    unauth_client.get("/api/v1/marketplace/listings/search")
    assert mock_search.call_args.kwargs["free_only"] is False


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_free_only_spares_the_caller_their_own_paid_rows(mock_search, authed_client_factory):
    """An authed free_only read is caller-dependent, so it must not be shared-cached.

    The caller's own paid listings deploy free (seller-self bypass) and stay
    in their results; serving that body to the next viewer from a CDN would
    put a paid row on someone else's phone.
    """
    mock_search.return_value = []
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/listings/search?free_only=true")
    assert resp.status_code == 200, resp.text
    assert mock_search.call_args.kwargs["viewer_id"] == "human_u_seller"
    assert resp.headers["Cache-Control"] == "private, max-age=60"

    # Without free_only the body is caller-independent again.
    resp = tc.get("/api/v1/marketplace/listings/search")
    assert mock_search.call_args.kwargs["viewer_id"] is None
    assert resp.headers["Cache-Control"] == "public, max-age=60"


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
    resp = tc.get("/api/v1/marketplace/listings/search?seller_id=human_u_seller&status_in=draft,published")
    assert resp.status_code == 200, resp.text
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["status_in"] == ["draft", "published"]
    assert call_kwargs["include_unpublished"] is True


@patch(
    "routers.marketplace_listings.seller_profile.get_profile",
    new=AsyncMock(return_value={"seller_id": "u_seller", "handle": "atlas"}),
)
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


def test_my_agents_summary_lists_the_paperclip_roster(authed_client_factory):
    """The roster is Paperclip's, which is the master record -- it used to be the ``agents.list``
    of the owner's ``openclaw.json``, a file no workspace has."""
    with patch(
        "core.services.messaging.load_agent_roster",
        new_callable=AsyncMock,
        return_value=[
            {"id": "agent_main", "name": "main"},
            {"id": "agent_pulse", "name": "Pulse"},
            {"name": "no id -- skipped"},
            "not a dict",
        ],
    ):
        tc = authed_client_factory("u_seller", "seller@example.com")
        resp = tc.get("/api/v1/marketplace/me/agents-summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["agent_id"] for a in body["agents"]] == ["agent_main", "agent_pulse"]
    assert [a["name"] for a in body["agents"]] == ["main", "Pulse"]


def test_my_agents_summary_reports_zero_counts_with_the_keys_intact(authed_client_factory):
    """The three counts came off the owner's ``openclaw.json`` (per-agent ``skills``/``channels``
    arrays and ``cron/jobs.json``) and a Hermes profile has none of them: skills resolve from
    SHARED ``skills.external_dirs``, channels are later, agent scheduling is pinned off. The keys
    have to stay -- the Publish wizard's sublines read them, and dropping them crashes the drawer
    rather than showing a zero."""
    with patch(
        "core.services.messaging.load_agent_roster",
        new_callable=AsyncMock,
        return_value=[{"id": "agent_main", "name": "main"}],
    ):
        tc = authed_client_factory("u_seller", "seller@example.com")
        resp = tc.get("/api/v1/marketplace/me/agents-summary")

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == [
        {"agent_id": "agent_main", "name": "main", "skill_count": 0, "channel_count": 0, "cron_count": 0}
    ]


def test_my_agents_summary_empty_roster_returns_empty(authed_client_factory):
    with patch("core.services.messaging.load_agent_roster", new_callable=AsyncMock, return_value=[]):
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
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_blocks_friday(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    authed_client_factory,
):
    """When the picked agent is the owner's Paperclip CEO (Friday), the
    publish endpoint must 400 and never call export_agent_from_profile.
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


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_503s_when_friday_guard_cannot_reach_paperclip(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    authed_client_factory,
):
    """The Friday guard must fail CLOSED: a Paperclip outage used to read as
    "no agents" → allow, letting a seller export Friday (owner-keyed identity
    and memory into an immutable artifact). Now the export blocks with a
    retryable 503 and never snapshots the dir."""
    from core.services.agent_export import PaperclipUnavailableError

    mock_is_friday.side_effect = PaperclipUnavailableError("could not verify the agent against Paperclip")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1"},
    )
    assert resp.status_code == 503
    assert "retry" in resp.json()["detail"] or "Paperclip" in resp.json()["detail"]
    mock_export.assert_not_called()


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
def test_artifact_preview_503s_when_friday_guard_cannot_reach_paperclip(mock_is_friday, authed_client_factory):
    from core.services.agent_export import PaperclipUnavailableError

    mock_is_friday.side_effect = PaperclipUnavailableError("could not verify the agent against Paperclip")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 503


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_artifact_preview_503s_when_container_lookup_fails(mock_get_container, mock_is_friday, authed_client_factory):
    """Fix round 2, gap 2: a transient container-row read failure must
    surface as a retryable 503, not silently degrade to the EFS route (which
    then 404s an AgentCore seller as 'agent not found'). ``mock_is_friday`` is
    asserted un-called: the container lookup is the FIRST line of the
    handler, so a real fix raises before the Friday guard ever runs --
    without that assertion this test would pass for the wrong reason (an
    unmocked Paperclip fetch inside the Friday guard 503s on its own, per
    ``test_artifact_preview_503s_when_friday_guard_cannot_reach_paperclip``
    right above)."""
    mock_get_container.side_effect = RuntimeError("dynamodb blip")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 503
    mock_is_friday.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_allows_non_friday(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    authed_client_factory,
):
    """Non-CEO agent_id passes the guard and proceeds to export + replace."""
    from core.services.catalog_package import CatalogPackage

    mock_find_active.return_value = None
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


@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
def test_artifact_from_agent_threads_the_resolved_container_through(
    mock_export,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    mock_get_container,
    authed_client_factory,
):
    """Fix round 1, item 1: the upload-from-agent route fetches the seller's
    container row up front and must thread it through to
    export_agent_from_profile so an AgentCore seller reads their Hermes S3
    profile instead of assuming EFS."""
    from core.services.catalog_package import CatalogPackage

    mock_find_active.return_value = None
    mock_is_friday.return_value = False
    agentcore_container = {"runtime_host": "agentcore"}
    mock_get_container.return_value = agentcore_container
    mock_export.return_value = CatalogPackage(
        format="openclaw",
        manifest={"name": "x", "agent_id": "custom-1"},
        openclaw_slice={"agent": {"name": "x"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake",
        tarball_contents=["./openclaw.json"],
    )
    mock_replace.return_value = {"listing_id": "lst_42", "version": 1, "manifest_sha256": "abc123"}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1"},
    )
    assert resp.status_code == 200, resp.text
    assert mock_export.call_args.kwargs["container"] == agentcore_container


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_passes_bundle_selection_through(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    authed_client_factory,
):
    """The Bundle step's selection (MCPs + skills) must reach export_agent_from_profile
    as keyword args so the slice carries them with secrets scrubbed."""
    from core.services.catalog_package import CatalogPackage

    mock_find_active.return_value = None
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
# Publish-dedupe guard on /listings/{id}/artifact-from-agent
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_blocks_duplicate_publish(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    authed_client_factory,
):
    """When the agent already has a published listing under a DIFFERENT
    listing_id, the fresh-publish upload 409s with the live listing_id so the
    frontend can redirect into republish-under-the-same-id. Export/replace must
    not run."""
    mock_is_friday.return_value = False
    mock_find_active.return_value = {"listing_id": "lst_live", "status": "published"}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_new_dup/artifact-from-agent",
        json={"agent_id": "custom-1"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "agent_already_published"
    assert detail["existing_listing_id"] == "lst_live"
    mock_export.assert_not_called()
    mock_replace.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_allows_republish_same_listing(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    authed_client_factory,
):
    """A genuine republish targets the SAME listing_id (create_new_version path),
    so the dedupe guard must NOT fire even though a published listing exists for
    the agent."""
    from core.services.catalog_package import CatalogPackage

    mock_is_friday.return_value = False
    # The live listing IS the one being versioned — same listing_id.
    mock_find_active.return_value = {"listing_id": "lst_42", "status": "published"}
    mock_export.return_value = CatalogPackage(
        format="openclaw",
        manifest={"name": "x", "agent_id": "custom-1"},
        openclaw_slice={"agent": {"name": "x"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake",
        tarball_contents=[],
    )
    mock_replace.return_value = {"listing_id": "lst_42", "version": 2, "manifest_sha256": "x"}
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent?version=2",
        json={"agent_id": "custom-1"},
    )
    assert resp.status_code == 200
    mock_export.assert_called_once()
    mock_replace.assert_called_once()


# ---------------------------------------------------------------------------
# GET /me/agents/{agent_id}/artifact-preview
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.enumerate_for_preview")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_preview_returns_available_mcps_skills_and_flags(
    _mock_get_container,
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
        "auto_ship_skills": ["my-skill"],
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


@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.enumerate_for_preview")
def test_artifact_preview_threads_the_resolved_container_through(
    mock_enumerate,
    mock_is_friday,
    mock_get_container,
    authed_client_factory,
):
    """Fix round 1, item 1: the preview route fetches the seller's container
    row up front and must thread it through to enumerate_for_preview so an
    AgentCore seller reads their Hermes S3 profile instead of assuming EFS."""
    mock_is_friday.return_value = False
    agentcore_container = {"runtime_host": "agentcore"}
    mock_get_container.return_value = agentcore_container
    mock_enumerate.return_value = {
        "agent_id": "custom-1",
        "available_mcps": [],
        "available_skills": [],
        "auto_ship_skills": [],
        "persona_files": [],
        "safety_flags": [],
    }
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 200, resp.text
    assert mock_enumerate.call_args.kwargs["container"] == agentcore_container


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.enumerate_for_preview")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_preview_needs_config_patch_false_for_pure_workspace_listing(
    _mock_get_container,
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
        "auto_ship_skills": ["my-skill"],
        "persona_files": ["IDENTITY.md"],
        "safety_flags": [],
    }
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/custom-1/artifact-preview")
    assert resp.status_code == 200
    assert resp.json()["needs_config_patch"] is False


@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_preview_blocks_friday(_mock_get_container, mock_is_friday, authed_client_factory):
    mock_is_friday.return_value = True
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.get("/api/v1/marketplace/me/agents/friday-uuid/artifact-preview")
    assert resp.status_code == 400
    assert "default agent" in resp.json()["detail"]


def test_artifact_preview_unauthorized(unauth_client):
    resp = unauth_client.get("/api/v1/marketplace/me/agents/x/artifact-preview")
    assert resp.status_code in (401, 403)


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
    assert call_kwargs["seller_id"] == "human_u_seller"
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


# ---------------------------------------------------------------------------
# require_org_admin gate on writes + hire
# ---------------------------------------------------------------------------


@pytest.fixture
def org_member_client():
    """TestClient authed as a non-admin org member.

    Pins org_role to ``org:member`` so ``require_org_admin`` raises
    403 — verifying the marketplace write endpoints reject shadow-publish /
    shadow-hire attempts from non-admin members of an org.
    """
    from main import app
    from core.auth import get_current_user

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_test",
        org_id="org_test",
        org_role="org:member",
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("routers.marketplace_listings.marketplace_service.create_draft", new_callable=AsyncMock)
@patch("routers.marketplace_listings.seller_profile.get_profile", new_callable=AsyncMock)
def test_create_listing_non_admin_org_member_403(mock_profile, mock_create, org_member_client):
    """A non-admin org member cannot create draft listings — the gate fires
    before profile or create-draft logic runs.
    """
    resp = org_member_client.post(
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
    assert resp.status_code == 403
    mock_profile.assert_not_called()
    mock_create.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.update_draft", new_callable=AsyncMock)
def test_patch_listing_non_admin_org_member_403(mock_update, org_member_client):
    resp = org_member_client.patch(
        "/api/v1/marketplace/listings/lst_42",
        json={"name": "New Name"},
    )
    assert resp.status_code == 403
    mock_update.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.submit_for_review", new_callable=AsyncMock)
def test_submit_listing_non_admin_org_member_403(mock_submit, org_member_client):
    resp = org_member_client.post("/api/v1/marketplace/listings/lst_42/submit")
    assert resp.status_code == 403
    mock_submit.assert_not_called()


@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
def test_artifact_from_agent_non_admin_org_member_403(mock_is_friday, mock_export, mock_replace, org_member_client):
    resp = org_member_client.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "agent_x"},
    )
    assert resp.status_code == 403
    # Gate fires before the Friday check + export — none of the underlying
    # service calls should have run.
    mock_is_friday.assert_not_called()
    mock_export.assert_not_called()
    mock_replace.assert_not_called()


@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_listing_non_admin_org_member_403(mock_get_published, mock_get_catalog, org_member_client):
    """Non-admin org members cannot hire (deploy) listings into the shared
    org container. Gate fires before slug lookup or catalog service is hit.
    """
    resp = org_member_client.post("/api/v1/marketplace/listings/lst_1/deploy")
    assert resp.status_code == 403
    mock_get_published.assert_not_called()
    mock_get_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# POST /listings/{listing_id}/republish
# ---------------------------------------------------------------------------


def _new_draft_row(*, listing_id: str = "lst_1", seller_id: str = "u_seller", version: int = 2) -> dict:
    """Shape of a row returned by ``marketplace_service.create_new_version``.

    Mirrors what the service writes — the response model is ``schemas.Listing``
    so the row must carry every required field (status, version, format,
    created_at, etc.) or FastAPI's response validation will trip.
    """
    return {
        "listing_id": listing_id,
        "version": version,
        "status": "draft",
        "seller_id": seller_id,
        "slug": "demo-agent",
        "name": "Demo Agent",
        "description_md": "An agent.",
        "format": "openclaw",
        "price_cents": 0,
        "tags": [],
        "artifact_uploaded": False,
        "created_at": "2026-05-23T10:00:00Z",
        "updated_at": "2026-05-23T10:00:00Z",
        "published_at": None,
    }


@patch("routers.marketplace_listings.marketplace_service.create_new_version", new_callable=AsyncMock)
def test_republish_creates_v2_draft(mock_create_new_version, authed_client_factory):
    """Happy path: service returns the new draft row at v=N+1 and the
    router forwards it through ``attach_seller_name`` to the response."""
    mock_create_new_version.return_value = _new_draft_row(listing_id="lst_1", seller_id="u_seller", version=2)
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "draft"
    assert body["seller_id"] == "u_seller"
    assert body["name"] == "Demo Agent"
    # Service was called with the listing_id from the path and the
    # caller's user_id as seller_id.
    mock_create_new_version.assert_awaited_once_with(
        listing_id="lst_1",
        seller_id="human_u_seller",
    )


@patch("routers.marketplace_listings.marketplace_service.create_new_version", new_callable=AsyncMock)
def test_republish_returns_409_when_no_live_version(mock_create_new_version, authed_client_factory):
    """Service raises NoPublishedVersionError -> 409 with a stable detail
    string the frontend can match on to render the no-live-version UI."""
    from core.services.marketplace_service import NoPublishedVersionError

    mock_create_new_version.side_effect = NoPublishedVersionError("listing lst_1 has no published version to republish")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code == 409
    assert "no live version" in resp.json()["detail"]


@patch("routers.marketplace_listings.marketplace_service.create_new_version", new_callable=AsyncMock)
def test_republish_returns_409_when_already_in_progress(mock_create_new_version, authed_client_factory):
    """Service raises RepublishAlreadyInProgressError -> 409 surfacing the
    offending version so the wizard can route the seller to that draft."""
    from core.services.marketplace_service import RepublishAlreadyInProgressError

    mock_create_new_version.side_effect = RepublishAlreadyInProgressError("republish already in progress for v2")
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code == 409
    assert "v2" in resp.json()["detail"]


@patch("routers.marketplace_listings.marketplace_service.create_new_version", new_callable=AsyncMock)
def test_republish_returns_403_for_non_owner(mock_create_new_version, authed_client_factory):
    """Service raises PermissionError -> 403. Ownership is enforced inside
    the service (before the in-flight check) so a non-owner can't probe
    for an in-flight republish on a listing they don't own.
    """
    from core.services.marketplace_service import PermissionError as MpPermissionError

    mock_create_new_version.side_effect = MpPermissionError("seller u_attacker does not own listing lst_1")
    tc = authed_client_factory("u_attacker", "attacker@example.com")
    resp = tc.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code == 403


def test_republish_requires_auth(unauth_client):
    """Without a Clerk JWT, republish must 401/403 before reaching the service."""
    resp = unauth_client.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code in (401, 403)


@patch("routers.marketplace_listings.marketplace_service.create_new_version", new_callable=AsyncMock)
def test_republish_non_admin_org_member_403(mock_create_new_version, org_member_client):
    """Non-admin org members cannot republish — gate fires before the
    service runs (matches the other marketplace write endpoints)."""
    resp = org_member_client.post("/api/v1/marketplace/listings/lst_1/republish")
    assert resp.status_code == 403
    mock_create_new_version.assert_not_called()


# ---------------------------------------------------------------------------
# DELETE /listings/{listing_id}?version=N (discard pending draft)
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.marketplace_service.discard_draft", new_callable=AsyncMock)
def test_discard_removes_draft_row(mock_discard, authed_client_factory):
    mock_discard.return_value = None
    client = authed_client_factory("u_seller", "seller@example.com")
    r = client.delete("/api/v1/marketplace/listings/lst_1?version=2")
    assert r.status_code == 204
    mock_discard.assert_awaited_once_with(listing_id="lst_1", seller_id="human_u_seller", version=2)


@patch("routers.marketplace_listings.marketplace_service.discard_draft", new_callable=AsyncMock)
def test_discard_returns_409_when_in_review(mock_discard, authed_client_factory):
    from core.services.marketplace_service import InvalidStateError

    mock_discard.side_effect = InvalidStateError("v2 is in status 'review', not draft")
    client = authed_client_factory("u_seller", "seller@example.com")
    r = client.delete("/api/v1/marketplace/listings/lst_1?version=2")
    assert r.status_code == 409


@patch("routers.marketplace_listings.marketplace_service.discard_draft", new_callable=AsyncMock)
def test_discard_returns_403_for_non_owner(mock_discard, authed_client_factory):
    from core.services.marketplace_service import PermissionError as MpPermissionError

    mock_discard.side_effect = MpPermissionError("not owner")
    client = authed_client_factory("u_other", "other@example.com")
    r = client.delete("/api/v1/marketplace/listings/lst_1?version=2")
    assert r.status_code == 403


def test_discard_requires_version_query_param(authed_client_factory):
    client = authed_client_factory("u_seller", "seller@example.com")
    r = client.delete("/api/v1/marketplace/listings/lst_1")
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Manifest routines: schedules recreate DORMANT, webhooks come back as pending
# --------------------------------------------------------------------------- #


def test_pending_automations_maps_webhook_entries_only():
    from routers.marketplace_listings import _pending_automations

    assert _pending_automations(
        [
            {"title": "Morning sweep", "cron": "0 6 * * *", "timezone": "UTC"},
            {"title": "GitHub: Issue opened or changed", "kind": "webhook", "provider": "github", "event_id": "issues"},
            {
                "title": "Stripe: Charge refunded",
                "kind": "webhook",
                "provider": "stripe",
                "event_id": "charge.refunded",
            },
        ]
    ) == [
        {"provider": "github", "event_id": "issues", "needs_resource": True},
        {"provider": "stripe", "event_id": "charge.refunded", "needs_resource": False},
    ]


def test_pending_automations_dedupes_and_drops_unknown_pairs():
    from routers.marketplace_listings import _pending_automations

    assert _pending_automations(
        [
            {"kind": "webhook", "provider": "github", "event_id": "issues"},
            {"kind": "webhook", "provider": "github", "event_id": "issues"},
            {"kind": "webhook", "provider": "github", "event_id": "gone.from.registry"},
            {"kind": "webhook"},
            "not-a-dict",
        ]
    ) == [{"provider": "github", "event_id": "issues", "needs_resource": True}]


@pytest.mark.asyncio
async def test_recreate_routines_never_arms_a_webhook_entry():
    """Webhook manifest entries carry no cron, so the dormant-schedule
    recreate path drops them before it ever resolves a company — a deploy
    must never silently arm a live trigger the buyer didn't ask for."""
    from routers.marketplace_listings import _recreate_routines_for_buyer

    with patch("routers.marketplace_listings.create_agent_routines", new_callable=AsyncMock) as mock_create:
        await _recreate_routines_for_buyer(
            owner_id="u_buyer",
            new_agent_id="agent_new",
            manifest_routines=[
                {
                    "title": "GitHub: Issue opened or changed",
                    "kind": "webhook",
                    "provider": "github",
                    "event_id": "issues",
                }
            ],
        )
    mock_create.assert_not_awaited()


def test_json_safe_variables_coerces_ddb_decimals():
    """Manifest numbers come back from DynamoDB as Decimal; the admin client's
    json.dumps refuses them (rolled back a prod deploy). Integral Decimals
    stay ints so Paperclip number variables round-trip exactly."""
    from decimal import Decimal

    from routers.marketplace_listings import _json_safe_variables

    out = _json_safe_variables(
        [
            {"name": "DAILY_PROSPECT_LIMIT", "type": "number", "defaultValue": Decimal("10"), "required": True},
            {"name": "RATE", "type": "number", "defaultValue": Decimal("2.5"), "required": True},
            {"name": "ICP", "type": "text", "defaultValue": "founders", "required": True},
        ]
    )
    assert out[0]["defaultValue"] == 10 and isinstance(out[0]["defaultValue"], int)
    assert out[1]["defaultValue"] == 2.5 and isinstance(out[1]["defaultValue"], float)
    assert out[2]["defaultValue"] == "founders"
    assert _json_safe_variables(None) is None
    assert _json_safe_variables([]) is None


# ---------------------------------------------------------------------------
# artifact-from-agent: off-loop export, explicit ``paths``, size cap
# ---------------------------------------------------------------------------


def _fake_package(**overrides):
    from core.services.catalog_package import CatalogPackage

    kwargs = dict(
        format="openclaw",
        manifest={"name": "x", "agent_id": "custom-1"},
        openclaw_slice={"agent": {"name": "x"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake",
        tarball_contents=["./openclaw.json"],
    )
    kwargs.update(overrides)
    return CatalogPackage(**kwargs)


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.replace_artifact", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.run_in_thread", new_callable=AsyncMock)
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_exports_off_loop_and_reports_pruned_paths(
    _mock_get_container,
    mock_run_in_thread,
    mock_is_friday,
    mock_replace,
    mock_find_active,
    authed_client_factory,
):
    """The EFS walk + tar is synchronous; the router must hand it to the
    thread helper (not await it inline) and pass ``paths`` through."""
    from core.services import agent_export

    mock_find_active.return_value = None
    mock_is_friday.return_value = False
    mock_run_in_thread.return_value = _fake_package(pruned_paths=["isol8"])
    mock_replace.return_value = {"listing_id": "lst_42", "version": 1, "manifest_sha256": "abc123"}

    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1", "paths": ["README.md", "skills"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pruned_paths"] == ["isol8"]
    mock_run_in_thread.assert_called_once()
    assert mock_run_in_thread.call_args.args[0] is agent_export.export_agent_from_profile
    assert mock_run_in_thread.call_args.kwargs["paths"] == ["README.md", "skills"]


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_413s_when_workspace_exceeds_cap(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_find_active,
    authed_client_factory,
):
    from core.services.agent_export import ExportTooLargeError

    mock_find_active.return_value = None
    mock_is_friday.return_value = False
    mock_export.side_effect = ExportTooLargeError(file_count=2001, byte_count=1, largest=[("isol8", 70 << 20)])

    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post("/api/v1/marketplace/listings/lst_42/artifact-from-agent", json={"agent_id": "custom-1"})
    assert resp.status_code == 413
    assert "isol8" in resp.json()["detail"]
    assert "paths" in resp.json()["detail"]


@patch("routers.marketplace_listings.marketplace_service.find_active_listing_for_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.is_reserved_friday_agent", new_callable=AsyncMock)
@patch("routers.marketplace_listings.agent_export.export_agent_from_profile")
@patch("routers.marketplace_listings.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=None)
def test_artifact_from_agent_400s_on_invalid_paths(
    _mock_get_container,
    mock_export,
    mock_is_friday,
    mock_find_active,
    authed_client_factory,
):
    from core.services.agent_export import InvalidExportPathsError

    mock_find_active.return_value = None
    mock_is_friday.return_value = False
    mock_export.side_effect = InvalidExportPathsError("invalid export paths: '../x'")

    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1", "paths": ["../x"]},
    )
    assert resp.status_code == 400
    assert "../x" in resp.json()["detail"]


def test_artifact_from_agent_rejects_empty_paths_list(authed_client_factory):
    """``paths: []`` would ship nothing — a wizard bug, not a seller intent."""
    tc = authed_client_factory("u_seller", "seller@example.com")
    resp = tc.post(
        "/api/v1/marketplace/listings/lst_42/artifact-from-agent",
        json={"agent_id": "custom-1", "paths": []},
    )
    assert resp.status_code == 422
