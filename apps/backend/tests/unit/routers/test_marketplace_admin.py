"""Tests for marketplace_admin router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _admit_admin(app):
    """Override get_current_user so the caller has an @isol8.co email."""
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_admin_123", email="admin@isol8.co")


def test_admin_queue_requires_admin(client):
    resp = client.get("/api/v1/admin/marketplace/listings")
    assert resp.status_code in (401, 403)


def test_approve_requires_admin(client):
    resp = client.post("/api/v1/admin/marketplace/listings/l1/approve")
    assert resp.status_code in (401, 403)


def test_admin_takedown_endpoint_requires_admin(client):
    resp = client.post(
        "/api/v1/admin/marketplace/listings/l1/takedown",
        json={"reason": "policy", "basis_md": "x" * 20},
    )
    assert resp.status_code in (401, 403)


def test_list_takedowns_requires_admin(client):
    resp = client.get("/api/v1/admin/marketplace/takedowns")
    assert resp.status_code in (401, 403)


def test_admin_takedown_endpoint_creates_and_grants(client):
    """Full request shape: admin POSTs reason+basis_md → service called →
    response carries takedown_id + affected_purchases."""
    from main import app

    _admit_admin(app)

    expected = {
        "takedown_id": "td-uuid-1",
        "listing_id": "l-1",
        "affected_purchases": 4,
    }
    with patch(
        "routers.marketplace_admin.takedown_service.execute_admin_initiated_takedown",
        new=AsyncMock(return_value=expected),
    ) as mock_service:
        resp = client.post(
            "/api/v1/admin/marketplace/listings/l-1/takedown",
            json={
                "reason": "fraud",
                "basis_md": "Listing impersonates an unrelated brand. Reported by Stripe radar.",
            },
        )

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "taken_down"
    assert body["takedown_id"] == "td-uuid-1"
    assert body["affected_purchases"] == 4
    assert body["listing_id"] == "l-1"

    mock_service.assert_awaited_once()
    call_kwargs = mock_service.await_args.kwargs
    assert call_kwargs["listing_id"] == "l-1"
    assert call_kwargs["reason"] == "fraud"
    assert call_kwargs["basis_md"].startswith("Listing impersonates")
    assert call_kwargs["decided_by"] == "user_admin_123"


def test_reject_reads_notes_from_request_body(client):
    """Regression: reject must accept notes as a JSON body field, not a
    query param. The admin UI sends ``{ notes }`` in JSON; if `notes`
    binds to a query param, every UI reject 422s (Codex P1 on PR #517,
    commit 23bdc518).
    """
    from main import app

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.marketplace_service.reject",
        new=AsyncMock(return_value={"status": "draft", "rejection_notes": "incomplete docs"}),
    ) as mock_service:
        resp = client.post(
            "/api/v1/admin/marketplace/listings/l-1/reject",
            json={"notes": "incomplete docs"},
        )

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    mock_service.assert_awaited_once()
    assert mock_service.await_args.kwargs["notes"] == "incomplete docs"


def test_admin_takedown_404s_for_nonexistent_listing(client):
    """Regression: the takedown cascade's ``_listings_table().update_item``
    used to upsert a fake listing for nonexistent IDs. Now guarded with
    ``ConditionExpression="attribute_exists(listing_id)"`` and surfaced as
    404 (Codex P2 on PR #517, commit 23bdc518).
    """
    from main import app

    from core.services.takedown_service import ListingNotFoundError

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.takedown_service.execute_admin_initiated_takedown",
        new=AsyncMock(side_effect=ListingNotFoundError("listing l-ghost v1 does not exist")),
    ):
        resp = client.post(
            "/api/v1/admin/marketplace/listings/l-ghost/takedown",
            json={
                "reason": "policy",
                "basis_md": "Long enough basis to pass min_length=10 validator.",
            },
        )

    app.dependency_overrides.clear()

    assert resp.status_code == 404, resp.text
    assert "l-ghost" in resp.json()["detail"]


def test_approve_v2_derives_prev_version_from_history(client):
    """Regression: approving version=N (N>1) without explicit prev_version
    must auto-derive it from the listing's history and route through
    publish_v2 — otherwise both versions end up published (Codex P1 on
    PR #517, commit 977bf178).
    """
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.query.return_value = {
        "Items": [
            {"listing_id": "l1", "version": 2, "status": "review"},
            {"listing_id": "l1", "version": 1, "status": "published"},
        ]
    }
    # The new approve-side scan gate calls get_item before reaching the
    # publish_v2 derivation path. Returning a row without s3_prefix
    # short-circuits the gate so this test only exercises the prev_version
    # derivation logic it cares about.
    fake_table.get_item.return_value = {"Item": {"listing_id": "l1", "version": 2, "status": "review"}}
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch(
            "routers.marketplace_admin.marketplace_service.publish_v2",
            new=AsyncMock(return_value=None),
        ) as mock_publish_v2,
        patch(
            "routers.marketplace_admin.marketplace_service.approve",
            new=AsyncMock(return_value={"status": "published", "version": 2}),
        ) as mock_simple_approve,
    ):
        resp = client.post("/api/v1/admin/marketplace/listings/l1/approve?version=2")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    # publish_v2 path was taken with derived prev_version=1.
    mock_publish_v2.assert_awaited_once()
    assert mock_publish_v2.await_args.kwargs["prev_version"] == 1
    assert mock_publish_v2.await_args.kwargs["new_version"] == 2
    # The simple approve path was NOT taken.
    mock_simple_approve.assert_not_awaited()
    body = resp.json()
    assert body["retired_version"] == 1


def test_admin_takedown_validates_basis_md_length(client):
    """basis_md < 10 chars rejected so the audit log always has a real reason."""
    from main import app

    _admit_admin(app)

    resp = client.post(
        "/api/v1/admin/marketplace/listings/l-1/takedown",
        json={"reason": "policy", "basis_md": "short"},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_admin_takedowns_list_returns_recent_first(client):
    """Scan-based audit log view sorts by decided_at desc."""
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.scan.return_value = {
        "Items": [
            {"takedown_id": "old", "decided_at": "2026-01-01T00:00:00Z"},
            {"takedown_id": "newest", "decided_at": "2026-04-01T00:00:00Z"},
            {"takedown_id": "mid", "decided_at": "2026-03-15T00:00:00Z"},
            {"takedown_id": "no-decided-at"},  # defensive: missing field sinks
        ]
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource):
        resp = client.get("/api/v1/admin/marketplace/takedowns")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [r["takedown_id"] for r in items] == ["newest", "mid", "old", "no-decided-at"]


def test_admin_takedowns_list_paginates_full_scan(client):
    """Audit-log view must page through ALL scan results before sorting.

    Pins the pagination contract: a `LastEvaluatedKey` on page 1 forces a
    follow-up scan with `ExclusiveStartKey`, and items from BOTH pages must
    end up in the merged + sorted output. Catches regressions that re-add
    a `Limit=N` cap (which would silently return "newest of an arbitrary
    subset" once the table holds >N rows).
    """
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    page1 = {
        "Items": [
            {"takedown_id": "p1-old", "decided_at": "2026-01-01T00:00:00Z"},
            {"takedown_id": "p1-mid", "decided_at": "2026-02-15T00:00:00Z"},
        ],
        "LastEvaluatedKey": {"listing_id": "l-cursor", "takedown_id": "td-cursor"},
    }
    page2 = {
        "Items": [
            {"takedown_id": "p2-newest", "decided_at": "2026-04-01T00:00:00Z"},
            {"takedown_id": "p2-older", "decided_at": "2025-12-01T00:00:00Z"},
        ],
        # No LastEvaluatedKey → end of pagination.
    }
    fake_table.scan.side_effect = [page1, page2]
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource):
        resp = client.get("/api/v1/admin/marketplace/takedowns")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text

    # Both pages were fetched.
    assert fake_table.scan.call_count == 2
    # Page 1: no ExclusiveStartKey, no Limit cap.
    page1_kwargs = fake_table.scan.call_args_list[0].kwargs
    assert "ExclusiveStartKey" not in page1_kwargs
    assert "Limit" not in page1_kwargs
    # Page 2: ExclusiveStartKey forwarded from page 1's LastEvaluatedKey.
    page2_kwargs = fake_table.scan.call_args_list[1].kwargs
    assert page2_kwargs["ExclusiveStartKey"] == {
        "listing_id": "l-cursor",
        "takedown_id": "td-cursor",
    }

    # All four items merged + sorted by decided_at desc.
    items = resp.json()["items"]
    assert [r["takedown_id"] for r in items] == [
        "p2-newest",
        "p1-mid",
        "p1-old",
        "p2-older",
    ]


# ---------------------------------------------------------------------------
# GET /admin/marketplace/listings/{id}/preview — partial-preview behavior
# ---------------------------------------------------------------------------


def _make_tarball(files: list[tuple[str, bytes]]) -> bytes:
    """Build a minimal in-memory .tar.gz with the given file content."""
    import io as _io
    import tarfile as _tarfile

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files:
            info = _tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))
    return buf.getvalue()


def _stub_preview_s3_and_ddb(tarball_bytes: bytes, listing_overrides: dict | None = None):
    """Build (boto3.resource, boto3.client) stubs the preview endpoint expects.

    Returns the patches as a tuple of context managers — caller composes them
    in a `with` block.
    """
    listing = {
        "listing_id": "lst_preview",
        "version": 1,
        "slug": "demo",
        "name": "Demo",
        "seller_id": "u_seller",
        "format": "openclaw",
        "status": "review",
        "price_cents": 0,
        "tags": [],
        "s3_prefix": "listings/lst_preview/v1/",
        "manifest_json": {},
    }
    listing.update(listing_overrides or {})

    fake_table = MagicMock()
    fake_table.query.return_value = {"Items": [listing]}
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    body_stream = MagicMock()
    body_stream.read.return_value = tarball_bytes
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": len(tarball_bytes)}
    s3_client.get_object.return_value = {"Body": body_stream}

    return fake_resource, s3_client


def test_preview_returns_partial_when_unpacked_exceeds_cap(client):
    """Regression: a tarball whose unpacked size exceeds the scan cap used
    to 413. Now it returns a partial preview — file_tree populated so the
    admin sees WHAT is in the artifact (and can reject), but safety_flags
    empty and scan_incomplete=True. The frontend disables Approve."""
    from main import app

    _admit_admin(app)

    # Two files: a small openclaw.json (well under cap) + a huge bloat file
    # whose size header alone trips the unpacked-cap check on the second
    # iteration. The actual data we tar in can stay small for test perf —
    # the cap check reads `m.size` from the header, not the buffer.
    small = b'{"name":"demo"}'
    cap = 50 * 1024 * 1024
    huge = b"x" * (cap + 1)  # one byte past cap forces the check to fire

    tar_bytes = _make_tarball([("openclaw.json", small), ("bloat.bin", huge)])
    fake_resource, s3_client = _stub_preview_s3_and_ddb(tar_bytes)

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
    ):
        resp = client.get("/api/v1/admin/marketplace/listings/lst_preview/preview")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scan_incomplete"] is True
    assert body["scan_failure_reason"]
    # File tree shows BOTH files so the moderator can identify the bloat.
    paths = sorted(e["path"] for e in body["file_tree"])
    assert paths == ["bloat.bin", "openclaw.json"]
    # Safety scan is skipped on partial preview — a partial scan is
    # actively dangerous (adversary could hide content behind padding).
    assert body["safety_flags"] == []


def test_preview_returns_partial_when_compressed_exceeds_cap(client):
    """When the compressed tarball itself exceeds the scan ceiling we can't
    safely buffer it for header enumeration — return a metadata-only
    response with scan_incomplete=True (file_tree=[]). The admin still
    sees the tarball size so they can ask the seller to slim it."""
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.query.return_value = {
        "Items": [
            {
                "listing_id": "lst_huge",
                "version": 1,
                "slug": "huge",
                "status": "review",
                "s3_prefix": "listings/lst_huge/v1/",
                "format": "openclaw",
                "seller_id": "u",
                "manifest_json": {},
                "price_cents": 0,
                "tags": [],
                "name": "Huge",
            }
        ]
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": 80 * 1024 * 1024}

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
    ):
        resp = client.get("/api/v1/admin/marketplace/listings/lst_huge/preview")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scan_incomplete"] is True
    assert body["tarball_bytes"] == 80 * 1024 * 1024
    assert body["file_tree"] == []
    # get_object should NOT have been called — we never downloaded the bytes.
    s3_client.get_object.assert_not_called()


def test_preview_with_version_returns_exact_row_not_review_priority(client):
    """Regression for Codex P2 on PR #718: when the admin clicks a
    listing from the browse view, the URL carries ``?version=N`` so
    the preview endpoint must fetch that exact row — not apply its
    "prefer review > published > newest" heuristic, which would
    silently swap to a different version's artifact for a listing
    that has both a published v1 and a review v2.
    """
    from main import app

    _admit_admin(app)

    published_v1 = {
        "listing_id": "lst_multi",
        "version": 1,
        "slug": "demo",
        "name": "Demo v1",
        "seller_id": "u_seller",
        "format": "openclaw",
        "status": "published",
        "price_cents": 0,
        "tags": [],
        "s3_prefix": "listings/lst_multi/v1/",
        "manifest_json": {},
    }
    # The query path (no-version fallback) would prefer this v2 row.
    review_v2 = {
        **published_v1,
        "version": 2,
        "name": "Demo v2",
        "status": "review",
        "s3_prefix": "listings/lst_multi/v2/",
    }

    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": published_v1}
    # Defensive: if the handler took the query path by mistake, it'd see v2.
    fake_table.query.return_value = {"Items": [review_v2, published_v1]}
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    tar_bytes = _make_tarball([("openclaw.json", b'{"name":"demo"}')])
    body_stream = MagicMock()
    body_stream.read.return_value = tar_bytes
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": len(tar_bytes)}
    s3_client.get_object.return_value = {"Body": body_stream}

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
    ):
        resp = client.get("/api/v1/admin/marketplace/listings/lst_multi/preview?version=1")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # We asked for v1 and got v1 — not the review v2 the heuristic would prefer.
    assert body["version"] == 1
    assert body["status"] == "published"
    assert body["name"] == "Demo v1"
    # Get-item was used (exact key lookup), query was NOT.
    fake_table.get_item.assert_called_once_with(Key={"listing_id": "lst_multi", "version": 1})
    fake_table.query.assert_not_called()


def test_preview_without_version_falls_back_to_review_priority(client):
    """Backwards-compat: the review-queue link still carries no version,
    so the endpoint applies its "prefer review > published > newest"
    heuristic — same behavior as before this change."""
    from main import app

    _admit_admin(app)

    published_v1 = {
        "listing_id": "lst_multi",
        "version": 1,
        "slug": "demo",
        "name": "Demo v1",
        "seller_id": "u_seller",
        "format": "openclaw",
        "status": "published",
        "price_cents": 0,
        "tags": [],
        "s3_prefix": "listings/lst_multi/v1/",
        "manifest_json": {},
    }
    review_v2 = {
        **published_v1,
        "version": 2,
        "name": "Demo v2",
        "status": "review",
        "s3_prefix": "listings/lst_multi/v2/",
    }

    fake_table = MagicMock()
    fake_table.query.return_value = {"Items": [review_v2, published_v1]}
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    tar_bytes = _make_tarball([("openclaw.json", b'{"name":"demo"}')])
    body_stream = MagicMock()
    body_stream.read.return_value = tar_bytes
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": len(tar_bytes)}
    s3_client.get_object.return_value = {"Body": body_stream}

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
    ):
        resp = client.get("/api/v1/admin/marketplace/listings/lst_multi/preview")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No version param → review row wins over published row.
    assert body["version"] == 2
    assert body["status"] == "review"


def test_approve_refuses_when_artifact_exceeds_cap(client):
    """Defense in depth: a moderator who bypasses the UI gate and POSTs
    approve directly must still be blocked when the artifact is too large
    to scan completely."""
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {"listing_id": "l1", "version": 1, "s3_prefix": "listings/l1/v1/", "artifact_uploaded": True}
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": 100 * 1024 * 1024}

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
        patch("routers.marketplace_admin.marketplace_service.approve", new=AsyncMock()) as mock_approve,
    ):
        resp = client.post("/api/v1/admin/marketplace/listings/l1/approve")

    app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert "scan cap" in resp.json()["detail"]
    # marketplace_service.approve should never have been called.
    mock_approve.assert_not_awaited()


def test_approve_refuses_when_unpacked_exceeds_cap(client):
    """Regression for Codex P1 on PR #693 (commit df4f8263): a gzip with a
    high compression ratio can be small on disk but unpack into more than
    the safety-scan budget. The compressed-size check alone would let such
    a listing through. Walk tar headers and check the unpacked sum too.
    """
    from main import app

    _admit_admin(app)

    # Build a tarball that's tiny compressed (the bloat is highly compressible)
    # but unpacks to over 50 MB. `'x' * N` compresses to ~N/1000 because gzip
    # finds the repeating pattern instantly.
    cap = 50 * 1024 * 1024
    bloat = b"x" * (cap + 1)
    tar_bytes = _make_tarball([("bloat.bin", bloat)])
    assert len(tar_bytes) < cap, (
        f"test setup invariant violated: tarball should be small compressed (was {len(tar_bytes)})"
    )

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {"listing_id": "l1", "version": 1, "s3_prefix": "listings/l1/v1/", "artifact_uploaded": True}
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    body_stream = MagicMock()
    body_stream.read.return_value = tar_bytes
    s3_client = MagicMock()
    s3_client.head_object.return_value = {"ContentLength": len(tar_bytes)}
    s3_client.get_object.return_value = {"Body": body_stream}

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
        patch("routers.marketplace_admin.marketplace_service.approve", new=AsyncMock()) as mock_approve,
    ):
        resp = client.post("/api/v1/admin/marketplace/listings/l1/approve")

    app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert "unpacks to" in resp.json()["detail"]
    mock_approve.assert_not_awaited()


def test_approve_fails_closed_when_head_object_fails(client):
    """Regression for Codex P2 on PR #693 (commit 42968c8f): a head_object
    failure on a listing whose artifact_uploaded=True (i.e. it MUST exist
    because submit_for_review enforced that invariant) means we can't
    verify scan safety. Fail closed instead of falling through to publish.
    """
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {"listing_id": "l1", "version": 1, "s3_prefix": "listings/l1/v1/", "artifact_uploaded": True}
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table
    s3_client = MagicMock()
    s3_client.head_object.side_effect = RuntimeError("S3 timeout")

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
        patch("routers.marketplace_admin.marketplace_service.approve", new=AsyncMock()) as mock_approve,
    ):
        resp = client.post("/api/v1/admin/marketplace/listings/l1/approve")

    app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert "could not verify" in resp.json()["detail"]
    mock_approve.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /admin/marketplace/listings — browse / search across statuses
# ---------------------------------------------------------------------------


def test_list_listings_no_params_returns_review_queue_via_gsi(client):
    """Backwards-compat: hitting /admin/marketplace/listings with no params
    keeps the original review-queue behavior (GSI query for status='review',
    Limit=50, no search-service call). Pre-existing callers (the original
    review page, scripts) must keep working without a code change.
    """
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.query.return_value = {
        "Items": [
            {"listing_id": "l1", "version": 1, "status": "review", "name": "Foo"},
            {"listing_id": "l2", "version": 1, "status": "review", "name": "Bar"},
        ]
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch(
            "routers.marketplace_admin.marketplace_search.search",
            new=AsyncMock(),
        ) as mock_search,
    ):
        resp = client.get("/api/v1/admin/marketplace/listings")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["listing_id"] for r in body["items"]] == ["l1", "l2"]
    # Default path uses the GSI query, NOT the search service.
    mock_search.assert_not_awaited()
    fake_table.query.assert_called_once()
    query_kwargs = fake_table.query.call_args.kwargs
    assert query_kwargs["IndexName"] == "status-published-index"
    assert query_kwargs["Limit"] == 50
    assert query_kwargs["ExpressionAttributeValues"][":review"] == "review"


def test_list_listings_with_status_uses_search_service(client):
    """status=published delegates to marketplace_search.search with
    include_unpublished=True + status_in=['published'] so admins see the
    full set across statuses (not just the helper's default public gate).
    """
    from main import app

    _admit_admin(app)

    expected_items = [
        {"listing_id": "lp1", "version": 1, "status": "published", "name": "Live"},
    ]
    with patch(
        "routers.marketplace_admin.marketplace_search.search",
        new=AsyncMock(return_value=expected_items),
    ) as mock_search:
        resp = client.get("/api/v1/admin/marketplace/listings?status=published")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == expected_items
    mock_search.assert_awaited_once()
    kwargs = mock_search.await_args.kwargs
    assert kwargs["include_unpublished"] is True
    assert kwargs["status_in"] == ["published"]
    assert kwargs["seller_id"] is None
    assert kwargs["query"] is None


def test_list_listings_with_query_only_calls_search(client):
    """Just q (no status) still routes through the search service so the
    admin can fuzzy-match across every status without first picking a
    status pill."""
    from main import app

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.marketplace_search.search",
        new=AsyncMock(return_value=[]),
    ) as mock_search:
        resp = client.get("/api/v1/admin/marketplace/listings?q=ray")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    mock_search.assert_awaited_once()
    kwargs = mock_search.await_args.kwargs
    assert kwargs["query"] == "ray"
    # No status filter → status_in stays None so search returns every status.
    assert kwargs["status_in"] is None
    assert kwargs["include_unpublished"] is True


def test_list_listings_with_status_and_query_combines_filters(client):
    """status + q both narrow the same search call."""
    from main import app

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.marketplace_search.search",
        new=AsyncMock(return_value=[]),
    ) as mock_search:
        resp = client.get("/api/v1/admin/marketplace/listings?status=taken_down&q=fraud")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    kwargs = mock_search.await_args.kwargs
    assert kwargs["query"] == "fraud"
    assert kwargs["status_in"] == ["taken_down"]


def test_list_listings_clamps_limit_at_200(client):
    """A 10000-row limit would scan most of the table per call. Clamp
    server-side to keep this from becoming a free DoS surface."""
    from main import app

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.marketplace_search.search",
        new=AsyncMock(return_value=[]),
    ) as mock_search:
        resp = client.get("/api/v1/admin/marketplace/listings?status=published&limit=10000")

    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert mock_search.await_args.kwargs["limit"] == 200


def test_list_listings_rejects_unknown_status(client):
    """An unknown status must 422 rather than silently returning [] —
    otherwise a typo in the URL bar looks like "no listings in that state"
    instead of an error."""
    from main import app

    _admit_admin(app)

    with patch(
        "routers.marketplace_admin.marketplace_search.search",
        new=AsyncMock(),
    ) as mock_search:
        resp = client.get("/api/v1/admin/marketplace/listings?status=bogus")

    app.dependency_overrides.clear()

    assert resp.status_code == 422
    mock_search.assert_not_awaited()


def test_list_listings_accepts_all_five_statuses(client):
    """Every status in the marketplace state machine must be a valid filter
    value — otherwise the admin can't browse half the rows."""
    from main import app

    _admit_admin(app)

    for status in ("draft", "review", "published", "retired", "taken_down"):
        with patch(
            "routers.marketplace_admin.marketplace_search.search",
            new=AsyncMock(return_value=[]),
        ) as mock_search:
            resp = client.get(f"/api/v1/admin/marketplace/listings?status={status}")
        assert resp.status_code == 200, f"{status}: {resp.text}"
        assert mock_search.await_args.kwargs["status_in"] == [status]

    app.dependency_overrides.clear()


def test_approve_fails_closed_when_artifact_download_fails(client):
    """Regression for Codex P2 on PR #693 (commit 805655bb): once
    head_object confirms an artifact exists, any subsequent S3 failure
    (AccessDenied, NoSuchKey race, timeout) must block approval. A broad
    catch that fell through to the publish path would ship an unscanned
    listing — exactly the case the scan cap is meant to prevent.
    """
    from main import app

    _admit_admin(app)

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {"listing_id": "l1", "version": 1, "s3_prefix": "listings/l1/v1/", "artifact_uploaded": True}
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table
    s3_client = MagicMock()
    # head_object succeeds — artifact is present and under the compressed cap.
    s3_client.head_object.return_value = {"ContentLength": 1024}
    # get_object fails — simulate S3 transient error or AccessDenied.
    s3_client.get_object.side_effect = RuntimeError("S3 AccessDenied")

    with (
        patch("routers.marketplace_admin.boto3.resource", return_value=fake_resource),
        patch("routers.marketplace_admin.boto3.client", return_value=s3_client),
        patch("routers.marketplace_admin.marketplace_service.approve", new=AsyncMock()) as mock_approve,
    ):
        resp = client.post("/api/v1/admin/marketplace/listings/l1/approve")

    app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert "scan failed" in resp.json()["detail"]
    mock_approve.assert_not_awaited()
