# Marketplace Listing Management & Republish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let sellers view their listings' details from `/chat` and `/marketplace`, and republish a Live listing with updated metadata or a fresh agent snapshot. v(N) stays Live until v(N+1) is approved, atomically swapped via `publish_new_version`.

**Architecture:** Reuses the existing version-aware DDB schema (`(listing_id, version)` composite key) and the publish wizard. One new service method (`create_new_version`) clones the live row into a v(N+1) draft. The wizard then drives v(N+1) through the existing PATCH/artifact-from-agent/submit flow. Admin approval of v(N+1) calls the existing (renamed) `publish_new_version` which atomically retires v(N).

**Tech Stack:** FastAPI + DynamoDB + S3 (backend); Next.js 16 App Router + Clerk + SWR + Tailwind (frontend); pytest + Vitest (tests).

**Spec:** `docs/superpowers/specs/2026-05-23-marketplace-listing-management-design.md`

**Testing convention for this plan:** Per user preference (`feedback_run_tests_at_end.md`, `feedback_write_tests_run_at_end.md`), each task **writes** the failing test and the implementation in the same task and commits them together. **No per-task test runs.** The final task (Task 13) runs the full backend + frontend suites and does the manual verification dance.

---

## File Map

**Backend — created:**
- (none — no new files; all changes layer onto existing services/routers)

**Backend — modified:**
- `apps/backend/core/services/marketplace_service.py` — add `create_new_version`, `discard_draft`, error types; rename `publish_v2` → `publish_new_version`
- `apps/backend/core/services/takedown_service.py` — cascade non-terminal v(N+1) rows on takedown
- `apps/backend/routers/marketplace_listings.py` — add `POST /listings/{id}/republish`, `DELETE /listings/{id}?version=N`
- `apps/backend/routers/marketplace_admin.py` — update call from `publish_v2` to `publish_new_version`
- `apps/backend/tests/unit/services/test_marketplace_service.py` — extend with versioning tests
- `apps/backend/tests/unit/services/test_takedown_service.py` — cascade tests
- `apps/backend/tests/unit/routers/test_marketplace_listings.py` — republish + discard tests
- `apps/backend/tests/unit/routers/test_marketplace_admin.py` — assert publish_new_version dispatch

**Frontend — created:**
- `apps/frontend/src/components/marketplace/ListingDetailView.tsx` — shared detail component
- `apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx` — unit tests
- `apps/frontend/src/app/marketplace/listing/[slug]/manage/page.tsx` — owner-gated full-page surface
- `apps/frontend/src/app/marketplace/listing/[slug]/manage/__tests__/page.test.tsx` — page test

**Frontend — modified:**
- `apps/frontend/src/lib/marketplace/api.ts` — add `republishListing`, `discardListingVersion`
- `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx` — group by listing_id, row click, pill subtext
- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx` — `"detail"` view state + `openListingDetail`
- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx` — render detail view branch
- `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx` — accept republish entry (`version` prop threaded through)
- `apps/frontend/src/app/marketplace/listing/[slug]/page.tsx` — "Manage your listing →" CTA for owners
- `apps/frontend/src/components/marketplace/drawer/__tests__/MyListingsView.test.tsx` — extend
- `apps/frontend/src/components/marketplace/drawer/__tests__/PublishWizardEdit.test.tsx` — extend

---

## Task 1: Backend — `create_new_version` service method + error types

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py` (add to top exceptions block + after `update_draft`)
- Test: `apps/backend/tests/unit/services/test_marketplace_service.py` (new class `TestCreateNewVersion`)

**Context:** This is the only new service primitive for republish. Clones the unique `status='published'` row for a listing into a new draft at `version = max(version) + 1`. Guards prevent (a) republishing when nothing is live, (b) two concurrent drafts. Uses DDB `TransactWriteItems` so the read-then-write race is closed: the new row insert is conditional on the published row still existing and no non-terminal row existing.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/services/test_marketplace_service.py`:

```python
class TestCreateNewVersion:
    @pytest.mark.asyncio
    async def test_clones_published_row_to_next_version_draft(self, dynamodb_table):
        # Setup: v=1 published row exists
        await marketplace_service.create_draft(
            seller_id="seller_1",
            slug="my-agent",
            name="My Agent",
            description_md="hello",
            format="openclaw_agent",
            price_cents=500,
            tags=["productivity"],
            category="agent",
            artifact_bytes=b"",
            manifest={"name": "My Agent"},
        )
        # Force v=1 to published (admin approve path) for the test
        await marketplace_service.approve(
            listing_id="lst_my-agent", version=1, approved_by="admin_1"
        )

        result = await marketplace_service.create_new_version(
            listing_id="lst_my-agent", seller_id="seller_1"
        )

        assert result["version"] == 2
        assert result["status"] == "draft"
        assert result["name"] == "My Agent"
        assert result["description_md"] == "hello"
        assert result["price_cents"] == 500
        assert result["tags"] == ["productivity"]
        # Artifact fields cleared on the new draft
        assert result.get("artifact_sha256") in (None, "")
        assert result.get("manifest_sha256") in (None, "")
        # v=1 is still published
        v1 = await marketplace_service.get_by_id(listing_id="lst_my-agent", version=1)
        assert v1["status"] == "published"

    @pytest.mark.asyncio
    async def test_raises_no_published_version_when_only_draft_exists(self, dynamodb_table):
        await marketplace_service.create_draft(
            seller_id="seller_1", slug="x", name="X", description_md="d",
            format="openclaw_agent", price_cents=0, tags=[], category="agent",
            artifact_bytes=b"", manifest={"name": "X"},
        )
        with pytest.raises(marketplace_service.NoPublishedVersionError):
            await marketplace_service.create_new_version(
                listing_id="lst_x", seller_id="seller_1"
            )

    @pytest.mark.asyncio
    async def test_raises_when_existing_draft_in_progress(self, dynamodb_table):
        # v=1 published, then v=2 draft already exists
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        # Second call must fail
        with pytest.raises(marketplace_service.RepublishAlreadyInProgressError) as exc:
            await marketplace_service.create_new_version(
                listing_id="lst_x", seller_id="seller_1"
            )
        assert "v2" in str(exc.value)

    @pytest.mark.asyncio
    async def test_raises_when_existing_review_in_progress(self, dynamodb_table):
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        # Need an artifact uploaded before submit; tests use a helper
        await _upload_dummy_artifact(dynamodb_table, "lst_x", version=2)
        await marketplace_service.submit_for_review(
            listing_id="lst_x", seller_id="seller_1", version=2
        )
        with pytest.raises(marketplace_service.RepublishAlreadyInProgressError):
            await marketplace_service.create_new_version(
                listing_id="lst_x", seller_id="seller_1"
            )

    @pytest.mark.asyncio
    async def test_raises_permission_when_seller_mismatch(self, dynamodb_table):
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        with pytest.raises(marketplace_service.PermissionError):
            await marketplace_service.create_new_version(
                listing_id="lst_x", seller_id="other_seller"
            )

    @pytest.mark.asyncio
    async def test_scales_to_v3(self, dynamodb_table):
        """v=1 published → v=2 republished+approved → v=3 republish must succeed."""
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        v2 = await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        await _upload_dummy_artifact(dynamodb_table, "lst_x", version=2)
        await marketplace_service.submit_for_review(
            listing_id="lst_x", seller_id="seller_1", version=2
        )
        await marketplace_service.publish_new_version(
            listing_id="lst_x", prev_version=1, new_version=2, approved_by="admin"
        )
        # Now v=2 is published. Republish to v=3.
        v3 = await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        assert v3["version"] == 3
        assert v3["status"] == "draft"
```

Helper fixtures `_create_and_publish` and `_upload_dummy_artifact` should be added at the top of the test file if not already present — mirror the patterns used in `TestPublishV2` / `TestSubmitForReview`. Look at lines around the existing `publish_v2` tests for the exact fixture shape; the codebase uses a `dynamodb_table` fixture in this module.

- [ ] **Step 2: Add the error types and implementation**

In `apps/backend/core/services/marketplace_service.py`, add to the exceptions block near the top (alongside `InvalidStateError`, `SlugCollisionError`, `ArtifactNotUploadedError`):

```python
class NoPublishedVersionError(Exception):
    """Republish requested for a listing with no currently-published version."""


class RepublishAlreadyInProgressError(Exception):
    """A non-terminal (draft/review) version already exists for this listing."""


class PermissionError(Exception):
    """Seller does not own the listing being modified."""
```

Add the function after `update_draft` (around line 390):

```python
async def create_new_version(*, listing_id: str, seller_id: str) -> dict:
    """Clone the currently-published row into a new draft at version+1.

    Guards (TransactWriteItems, executed conditionally):
      1. A row with status='published' exists for listing_id
         (else NoPublishedVersionError)
      2. No row with status in {draft, review} exists for listing_id
         (else RepublishAlreadyInProgressError naming the offending vN)
      3. seller_id matches the published row's seller_id
         (else PermissionError)

    Inserts a new row at version=max(version)+1 with metadata cloned
    from the published row and artifact fields cleared. The seller
    must call /artifact-from-agent before /submit.
    """
    table = _listings_table()

    # Read all rows for this listing. The version=0 sentinel row is
    # filtered out — it's the slug reservation.
    resp = table.query(
        KeyConditionExpression="listing_id = :l AND version > :z",
        ExpressionAttributeValues={":l": listing_id, ":z": 0},
    )
    rows = resp.get("Items", [])
    if not rows:
        raise NoPublishedVersionError(f"listing {listing_id} has no versions")

    published = next((r for r in rows if r.get("status") == "published"), None)
    if published is None:
        raise NoPublishedVersionError(
            f"listing {listing_id} has no published version to republish"
        )

    if published.get("seller_id") != seller_id:
        raise PermissionError(
            f"seller {seller_id} does not own listing {listing_id}"
        )

    non_terminal = next(
        (r for r in rows if r.get("status") in ("draft", "review")),
        None,
    )
    if non_terminal is not None:
        v = int(non_terminal["version"])
        raise RepublishAlreadyInProgressError(
            f"republish already in progress for v{v}"
        )

    new_version = max(int(r["version"]) for r in rows) + 1
    now = datetime.now(timezone.utc).isoformat()

    new_row = {
        "listing_id": listing_id,
        "version": new_version,
        "status": "draft",
        "seller_id": seller_id,
        "slug": published["slug"],
        "name": published["name"],
        "description_md": published.get("description_md", ""),
        "format": published["format"],
        "price_cents": int(published.get("price_cents", 0)),
        "tags": published.get("tags", []),
        "category": published.get("category"),
        "created_at": now,
        "updated_at": now,
        # Intentionally NOT copied: artifact_sha256, manifest_sha256,
        # openclaw_slice, rejection_notes, rejected_at, approved_by,
        # approved_at, install_count
    }

    # Conditional put: insert this version row only if it doesn't already
    # exist (closes the read-then-write race on `new_version`).
    # The two guards above (no non-terminal row, published exists) were
    # checked above; the only race we still defend here is two parallel
    # create_new_version calls trying to claim the same new_version slot.
    try:
        table.put_item(
            Item=new_row,
            ConditionExpression="attribute_not_exists(listing_id)",
        )
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RepublishAlreadyInProgressError(
                f"race detected creating v{new_version}; refetch and retry"
            )
        raise

    return new_row
```

Imports at the top of the file may need `from datetime import datetime, timezone` if not already present, and `import botocore.exceptions`.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/tests/unit/services/test_marketplace_service.py
git commit -m "feat(marketplace): add create_new_version service for republish"
```

---

## Task 2: Backend — Rename `publish_v2` → `publish_new_version`

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py:517` (function def + docstring + internal log strings)
- Modify: `apps/backend/routers/marketplace_admin.py:248` (call site)
- Modify: `apps/backend/tests/unit/services/test_marketplace_service.py` (`TestPublishV2` → `TestPublishNewVersion`)
- Modify: `apps/backend/tests/unit/routers/test_marketplace_admin.py` (any references)

**Context:** Function is already generic over `(prev_version, new_version)`. The `v2` in the name is misleading after this work ships — v3, v4, vN all use it. Rename per the user's `feedback_no_backwards_compat_noise.md` memory (no alias; just rename everywhere).

- [ ] **Step 1: Rename in the service file**

In `apps/backend/core/services/marketplace_service.py`:
- Function signature line 517: `async def publish_v2(` → `async def publish_new_version(`
- Update the docstring to drop "v2" mentions; keep the parameter docs since they're already generic
- Internal log/error strings on line ~589: `"publish_v2 conditional check failed..."` → `"publish_new_version conditional check failed..."`
- Search and update any other `publish_v2` references in the same file:

```bash
grep -n "publish_v2" apps/backend/core/services/marketplace_service.py
```

Replace each instance.

- [ ] **Step 2: Rename in the admin router**

In `apps/backend/routers/marketplace_admin.py`:
- Line ~147 docstring: `publish_v2 atomically flips` → `publish_new_version atomically flips`
- Line ~248: `await marketplace_service.publish_v2(` → `await marketplace_service.publish_new_version(`

```bash
grep -n "publish_v2" apps/backend/routers/marketplace_admin.py
```

Replace each.

- [ ] **Step 3: Update tests**

```bash
grep -rln "publish_v2" apps/backend/tests/
```

Replace each `publish_v2` reference. Class `TestPublishV2` → `TestPublishNewVersion`. Function calls inside test bodies.

- [ ] **Step 4: Verify no stragglers**

```bash
grep -rn "publish_v2" apps/backend/ apps/infra/ docs/
```

Expected: zero results outside of `docs/superpowers/specs/` (the spec is historical and stays as-is). Update the spec doc too:

```bash
grep -n "publish_v2" docs/superpowers/specs/2026-05-23-marketplace-listing-management-design.md
```

Update spec references to `publish_new_version`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/routers/marketplace_admin.py apps/backend/tests/ docs/superpowers/specs/2026-05-23-marketplace-listing-management-design.md
git commit -m "refactor(marketplace): rename publish_v2 to publish_new_version"
```

---

## Task 3: Backend — `POST /listings/{listing_id}/republish` endpoint

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py` (add endpoint after `submit`, ~line 590)
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py`

**Context:** Thin wrapper around `create_new_version`. Returns the new draft row so the frontend can immediately open the wizard at the right version.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/routers/test_marketplace_listings.py`:

```python
class TestRepublish:
    def test_republish_creates_v2_draft(self, authed_client_for_seller, published_listing):
        listing_id = published_listing["listing_id"]
        r = authed_client_for_seller.post(f"/api/v1/marketplace/listings/{listing_id}/republish")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 2
        assert body["status"] == "draft"
        assert body["seller_id"] == published_listing["seller_id"]
        assert body["name"] == published_listing["name"]

    def test_republish_returns_409_when_no_live_version(self, authed_client_for_seller, draft_listing):
        listing_id = draft_listing["listing_id"]
        r = authed_client_for_seller.post(f"/api/v1/marketplace/listings/{listing_id}/republish")
        assert r.status_code == 409
        assert "no live version" in r.json()["detail"]

    def test_republish_returns_409_when_already_in_progress(
        self, authed_client_for_seller, published_listing
    ):
        listing_id = published_listing["listing_id"]
        # First call succeeds
        authed_client_for_seller.post(f"/api/v1/marketplace/listings/{listing_id}/republish")
        # Second call must 409
        r = authed_client_for_seller.post(f"/api/v1/marketplace/listings/{listing_id}/republish")
        assert r.status_code == 409
        assert "v2" in r.json()["detail"]

    def test_republish_returns_403_for_non_owner(
        self, authed_client_for_other_seller, published_listing
    ):
        listing_id = published_listing["listing_id"]
        r = authed_client_for_other_seller.post(
            f"/api/v1/marketplace/listings/{listing_id}/republish"
        )
        assert r.status_code == 403
```

The fixtures `authed_client_for_seller`, `authed_client_for_other_seller`, `published_listing`, `draft_listing` should follow patterns in the existing tests in the same file (e.g., the patterns used in `TestUpdateListing` and `TestSubmit`). If they don't exist, add them to the file's conftest or fixture block.

- [ ] **Step 2: Add the endpoint**

In `apps/backend/routers/marketplace_listings.py`, add after the `submit` endpoint (around line 590):

```python
@router.post("/listings/{listing_id}/republish", response_model=schemas.Listing)
async def republish(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create a new draft version cloning the currently-published row.

    Returns the new draft row at version=max(version)+1. The wizard then
    drives it through PATCH/artifact-from-agent/submit at that version.

    Guards: no-live-version → 409; existing draft/review → 409 naming the
    offending version; non-owner → 403.
    """
    require_org_admin(auth)
    try:
        row = await marketplace_service.create_new_version(
            listing_id=listing_id,
            seller_id=auth.user_id,
        )
    except marketplace_service.NoPublishedVersionError:
        raise HTTPException(status_code=409, detail="no live version to republish")
    except marketplace_service.RepublishAlreadyInProgressError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await seller_display.attach_seller_name(row)
    return row
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "feat(marketplace): POST /listings/{id}/republish endpoint"
```

---

## Task 4: Backend — Discard pending draft

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py` (add `discard_draft` after `create_new_version`)
- Modify: `apps/backend/routers/marketplace_listings.py` (add `DELETE` endpoint)
- Test: `apps/backend/tests/unit/services/test_marketplace_service.py`
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py`

**Context:** Allows the seller to walk away from an in-progress republish (also covers the "post-rejection, I changed my mind" case). Only allowed when status is `draft`. Submitted (`review`) drafts must wait for admin reject or approve.

- [ ] **Step 1: Write the failing service tests**

Append to `apps/backend/tests/unit/services/test_marketplace_service.py`:

```python
class TestDiscardDraft:
    @pytest.mark.asyncio
    async def test_discards_draft_row(self, dynamodb_table):
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        await marketplace_service.discard_draft(
            listing_id="lst_x", seller_id="seller_1", version=2
        )
        # v=2 row gone, v=1 still published
        assert await marketplace_service.get_by_id(listing_id="lst_x", version=2) is None
        v1 = await marketplace_service.get_by_id(listing_id="lst_x", version=1)
        assert v1["status"] == "published"

    @pytest.mark.asyncio
    async def test_rejects_when_not_draft(self, dynamodb_table):
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        await _upload_dummy_artifact(dynamodb_table, "lst_x", version=2)
        await marketplace_service.submit_for_review(
            listing_id="lst_x", seller_id="seller_1", version=2
        )
        with pytest.raises(marketplace_service.InvalidStateError):
            await marketplace_service.discard_draft(
                listing_id="lst_x", seller_id="seller_1", version=2
            )

    @pytest.mark.asyncio
    async def test_rejects_when_not_owner(self, dynamodb_table):
        await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
        await marketplace_service.create_new_version(
            listing_id="lst_x", seller_id="seller_1"
        )
        with pytest.raises(marketplace_service.PermissionError):
            await marketplace_service.discard_draft(
                listing_id="lst_x", seller_id="other", version=2
            )

    @pytest.mark.asyncio
    async def test_cannot_discard_v1(self, dynamodb_table):
        """v=1 is the only version of a still-unpublished listing — discarding
        it would orphan the slug reservation. Sellers delete via a different
        path (full retract, not in scope here)."""
        await marketplace_service.create_draft(
            seller_id="seller_1", slug="x", name="X", description_md="",
            format="openclaw_agent", price_cents=0, tags=[], category="agent",
            artifact_bytes=b"", manifest={"name": "X"},
        )
        with pytest.raises(marketplace_service.InvalidStateError):
            await marketplace_service.discard_draft(
                listing_id="lst_x", seller_id="seller_1", version=1
            )
```

- [ ] **Step 2: Add the service method**

In `apps/backend/core/services/marketplace_service.py`, after `create_new_version`:

```python
async def discard_draft(*, listing_id: str, seller_id: str, version: int) -> None:
    """Delete a non-terminal draft row.

    Guards:
      - version must be > 1 (v=1 carries the slug reservation; full
        listing retraction is a separate flow not in scope here)
      - row must exist and be in 'draft' status (not 'review', which
        requires admin reject)
      - seller_id must match the row's seller_id

    Idempotency: a second call after delete returns silently (the row
    is gone). The DELETE endpoint that wraps this maps that to a 404
    so callers can detect the no-op.
    """
    if version <= 1:
        raise InvalidStateError(
            "cannot discard v1; full listing retraction is a separate flow"
        )

    table = _listings_table()
    resp = table.get_item(Key={"listing_id": listing_id, "version": version})
    row = resp.get("Item")
    if row is None:
        raise InvalidStateError(f"v{version} not found for listing {listing_id}")
    if row.get("seller_id") != seller_id:
        raise PermissionError(
            f"seller {seller_id} does not own listing {listing_id}"
        )
    if row.get("status") != "draft":
        raise InvalidStateError(
            f"v{version} is in status '{row.get('status')}', not draft"
        )

    table.delete_item(
        Key={"listing_id": listing_id, "version": version},
        ConditionExpression="#s = :draft",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":draft": "draft"},
    )
```

- [ ] **Step 3: Write the failing router tests**

Append to `apps/backend/tests/unit/routers/test_marketplace_listings.py`:

```python
class TestDiscardListingVersion:
    def test_discard_removes_draft_row(self, authed_client_for_seller, published_listing):
        listing_id = published_listing["listing_id"]
        authed_client_for_seller.post(f"/api/v1/marketplace/listings/{listing_id}/republish")
        r = authed_client_for_seller.delete(
            f"/api/v1/marketplace/listings/{listing_id}?version=2"
        )
        assert r.status_code == 204

    def test_discard_returns_409_when_in_review(
        self, authed_client_for_seller, published_listing
    ):
        listing_id = published_listing["listing_id"]
        # Set up a v=2 in review (helper would mock the submit path)
        # ... (see fixture _setup_v2_in_review)
        r = authed_client_for_seller.delete(
            f"/api/v1/marketplace/listings/{listing_id}?version=2"
        )
        assert r.status_code == 409

    def test_discard_returns_403_for_non_owner(
        self, authed_client_for_other_seller, published_listing
    ):
        listing_id = published_listing["listing_id"]
        # ... seller_1 creates v=2 draft
        r = authed_client_for_other_seller.delete(
            f"/api/v1/marketplace/listings/{listing_id}?version=2"
        )
        assert r.status_code == 403
```

- [ ] **Step 4: Add the endpoint**

In `apps/backend/routers/marketplace_listings.py`, add after `republish`:

```python
@router.delete("/listings/{listing_id}", status_code=204)
async def discard_listing_version(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    version: int = Query(..., ge=2),
):
    """Discard a non-terminal draft version (v>=2).

    Used when the seller starts a republish then changes their mind, or
    when a v(N+1) sits as a rejected draft they no longer want to revise.
    The live v(N) row is untouched.
    """
    require_org_admin(auth)
    try:
        await marketplace_service.discard_draft(
            listing_id=listing_id, seller_id=auth.user_id, version=version,
        )
    except marketplace_service.InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except marketplace_service.PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return None
```

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/routers/marketplace_listings.py apps/backend/tests/
git commit -m "feat(marketplace): discard pending draft endpoint"
```

---

## Task 5: Backend — Cascade takedown to non-terminal v(N+1)

**Files:**
- Modify: `apps/backend/core/services/takedown_service.py` (extend `execute_admin_initiated_takedown`)
- Test: `apps/backend/tests/unit/services/test_takedown_service.py`

**Context:** When admin takes down v(N), any in-flight v(N+1) draft or review row would orphan — its `publish_new_version` would later fail because v(N) is no longer `published`. Cascade them to `taken_down` in the same TransactWriteItems so the seller's UI surfaces it cleanly. The takedown row + license cascade behavior is unchanged; we only add the v(N+1) status flip.

- [ ] **Step 1: Read the current takedown impl**

```bash
grep -n "execute_admin_initiated_takedown\|TransactWriteItems\|status.*taken_down" apps/backend/core/services/takedown_service.py
```

Read the function to understand the existing transaction. Identify where the v(N) status flip to `taken_down` happens; the new v(N+1) flip is added next to it in the same `TransactWriteItems` call.

- [ ] **Step 2: Write the failing test**

Append to `apps/backend/tests/unit/services/test_takedown_service.py`:

```python
@pytest.mark.asyncio
async def test_takedown_cascades_to_non_terminal_v2(dynamodb_table):
    # Setup: v=1 published, v=2 draft (mid-republish)
    await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
    await marketplace_service.create_new_version(
        listing_id="lst_x", seller_id="seller_1"
    )

    await takedown_service.execute_admin_initiated_takedown(
        listing_id="lst_x", reason="policy", initiated_by="admin_1",
    )

    v1 = await marketplace_service.get_by_id(listing_id="lst_x", version=1)
    v2 = await marketplace_service.get_by_id(listing_id="lst_x", version=2)
    assert v1["status"] == "taken_down"
    assert v2["status"] == "taken_down"

@pytest.mark.asyncio
async def test_takedown_cascades_to_v2_in_review(dynamodb_table):
    await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
    await marketplace_service.create_new_version(
        listing_id="lst_x", seller_id="seller_1"
    )
    await _upload_dummy_artifact(dynamodb_table, "lst_x", version=2)
    await marketplace_service.submit_for_review(
        listing_id="lst_x", seller_id="seller_1", version=2
    )

    await takedown_service.execute_admin_initiated_takedown(
        listing_id="lst_x", reason="policy", initiated_by="admin_1",
    )

    v2 = await marketplace_service.get_by_id(listing_id="lst_x", version=2)
    assert v2["status"] == "taken_down"

@pytest.mark.asyncio
async def test_takedown_no_cascade_when_no_v2_exists(dynamodb_table):
    """Listings with no in-flight republish behave exactly as before."""
    await _create_and_publish(dynamodb_table, "lst_x", "seller_1", "x")
    await takedown_service.execute_admin_initiated_takedown(
        listing_id="lst_x", reason="policy", initiated_by="admin_1",
    )
    v1 = await marketplace_service.get_by_id(listing_id="lst_x", version=1)
    assert v1["status"] == "taken_down"
```

- [ ] **Step 3: Extend takedown_service**

In `apps/backend/core/services/takedown_service.py`, inside `execute_admin_initiated_takedown`, before the existing `TransactWriteItems` (or as part of it), look up any non-terminal v(N+1):

```python
# After resolving the target listing_id/version pair for takedown,
# look up any in-flight non-terminal row to cascade.
all_rows_resp = table.query(
    KeyConditionExpression="listing_id = :l AND version > :z",
    ExpressionAttributeValues={":l": listing_id, ":z": 0},
)
all_rows = all_rows_resp.get("Items", [])
non_terminal_versions = [
    int(r["version"]) for r in all_rows
    if r.get("status") in ("draft", "review")
]

# In the TransactWriteItems list, in addition to the existing flip of
# the published row to taken_down, append one Update per non-terminal
# version flipping it to taken_down too. Each Update is conditional on
# the row still being in draft/review state to avoid clobbering a race.
transact_items = [
    # ... existing items (takedown row insert, license revocations,
    # published → taken_down flip) ...
]
for v in non_terminal_versions:
    transact_items.append({
        "Update": {
            "TableName": settings.MARKETPLACE_LISTINGS_TABLE,
            "Key": {"listing_id": {"S": listing_id}, "version": {"N": str(v)}},
            "UpdateExpression": "SET #s = :td, updated_at = :now",
            "ConditionExpression": "#s IN (:draft, :review)",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":td": {"S": "taken_down"},
                ":draft": {"S": "draft"},
                ":review": {"S": "review"},
                ":now": {"S": now_iso},
            },
        }
    })
```

The exact integration point depends on the existing file's structure. Read the file end-to-end first; preserve the existing license-revocation cascade and only add the new flip items.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/takedown_service.py apps/backend/tests/unit/services/test_takedown_service.py
git commit -m "feat(marketplace): cascade takedown to non-terminal v2 rows"
```

---

## Task 6: Frontend — API client helpers

**Files:**
- Modify: `apps/frontend/src/lib/marketplace/api.ts`

**Context:** Add typed wrappers for the two new endpoints. Mirror the existing helpers' style (e.g. `myListings`, `createListing`).

- [ ] **Step 1: Read the existing api.ts**

```bash
grep -n "^export\|fetch\|async function" apps/frontend/src/lib/marketplace/api.ts | head -30
```

Match the existing signature pattern (almost certainly `{ jwt }` or `useApi`-style).

- [ ] **Step 2: Add the helpers**

In `apps/frontend/src/lib/marketplace/api.ts`:

```typescript
export async function republishListing(
  { listingId, jwt }: { listingId: string; jwt: string }
): Promise<Listing> {
  const r = await fetch(
    `${API_BASE}/marketplace/listings/${listingId}/republish`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${jwt}` },
    },
  );
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail ?? `republish failed (${r.status})`);
  }
  return r.json();
}

export async function discardListingVersion(
  { listingId, version, jwt }: { listingId: string; version: number; jwt: string }
): Promise<void> {
  const r = await fetch(
    `${API_BASE}/marketplace/listings/${listingId}?version=${version}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${jwt}` },
    },
  );
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail ?? `discard failed (${r.status})`);
  }
}
```

Use the existing `API_BASE` constant (or whatever the file uses). The exact import/signature should match the file's existing patterns — if helpers in the same file use `useApi` instead of bare fetch, follow that.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/lib/marketplace/api.ts
git commit -m "feat(marketplace): add republish + discard API client helpers"
```

---

## Task 7: Frontend — `<ListingDetailView>` shell + Overview + Bundle tabs

**Files:**
- Create: `apps/frontend/src/components/marketplace/ListingDetailView.tsx`
- Create: `apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx`

**Context:** Shared component used by the drawer (mode="drawer") and the `/manage` page (mode="page"). Renders Overview + Bundle tabs in this task; Pending Update tab + action buttons land in Task 8 to keep the diff reviewable.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ListingDetailView } from "../ListingDetailView";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "seller_1", getToken: async () => "test-jwt" }),
}));
vi.mock("@/lib/marketplace/api", () => ({
  getListingBySlug: vi.fn().mockResolvedValue({
    listing_id: "lst_x",
    version: 3,
    status: "published",
    slug: "x",
    name: "My Agent",
    description_md: "# Hello\nThis is the agent.",
    price_cents: 500,
    tags: ["productivity", "writing"],
    category: "agent",
    format: "openclaw_agent",
    seller_id: "seller_1",
    install_count: 17,
  }),
  myListings: vi.fn().mockResolvedValue({ items: [] }),
}));

describe("<ListingDetailView>", () => {
  it("renders live row metadata in Overview tab", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    expect(screen.getByText(/Live/)).toBeInTheDocument();
    expect(screen.getByText(/17 installs/)).toBeInTheDocument();
    expect(screen.getByText("productivity")).toBeInTheDocument();
    expect(screen.getByText("writing")).toBeInTheDocument();
  });

  it("Bundle tab shows agent_id and manifest data", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    // Click Bundle tab
    screen.getByRole("tab", { name: /bundle/i }).click();
    await waitFor(() => expect(screen.getByText(/agent_/)).toBeInTheDocument());
  });

  it("mode='page' renders wider layout chrome than mode='drawer'", async () => {
    const { rerender, container } = render(
      <ListingDetailView listingId="lst_x" mode="drawer" />
    );
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    expect(container.querySelector("[data-mode='drawer']")).not.toBeNull();
    rerender(<ListingDetailView listingId="lst_x" mode="page" />);
    expect(container.querySelector("[data-mode='page']")).not.toBeNull();
  });
});
```

- [ ] **Step 2: Implement the component**

```tsx
// apps/frontend/src/components/marketplace/ListingDetailView.tsx
"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { getListingBySlug, myListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

type Tab = "overview" | "bundle" | "pending";

interface Props {
  listingId: string;
  mode: "drawer" | "page";
}

export function ListingDetailView({ listingId, mode }: Props) {
  const { userId, getToken } = useAuth();
  const [live, setLive] = useState<Listing | null>(null);
  const [pending, setPending] = useState<Listing | null>(null);
  const [tab, setTab] = useState<Tab>("overview");

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt || !userId) return;
      // Fetch this seller's listings; find the rows for our listing_id.
      const { items } = await myListings({ sellerId: userId, jwt });
      if (!alive) return;
      const rows = items.filter(l => l.listing_id === listingId);
      setLive(rows.find(r => r.status === "published") ?? rows[0] ?? null);
      setPending(
        rows.find(r => r.status === "draft" || r.status === "review") ?? null
      );
    })();
    return () => { alive = false; };
  }, [listingId, userId, getToken]);

  if (!live) return <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>;

  const containerCls = mode === "page"
    ? "max-w-4xl mx-auto px-6 py-8"
    : "px-2";

  return (
    <div data-mode={mode} className={containerCls}>
      <Header live={live} pending={pending} />
      <Stats live={live} />
      <Tabs tab={tab} setTab={setTab} showPending={!!pending} />
      {tab === "overview" && <OverviewTab listing={live} />}
      {tab === "bundle" && <BundleTab listing={live} />}
      {tab === "pending" && pending && <PendingTab pending={pending} />}
    </div>
  );
}

function Header({ live, pending }: { live: Listing; pending: Listing | null }) {
  const pendingBadge = pending && (
    pending.status === "review" ? `· v${pending.version} in review`
    : pending.rejection_notes ? `· v${pending.version} rejected`
    : `· v${pending.version} draft`
  );
  return (
    <header className="mb-6 flex items-start justify-between gap-3">
      <div>
        <h2 className="font-editorial-serif text-3xl text-[#1a1a1a]">{live.name}</h2>
        <p className="text-[12.5px] text-[#5b5b5b] mt-1">{live.slug}</p>
      </div>
      <span className="text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full bg-[#e3efe6] text-[#06402b]">
        Live {pendingBadge}
      </span>
    </header>
  );
}

function Stats({ live }: { live: Listing }) {
  const installs = (live as unknown as { install_count?: number }).install_count ?? 0;
  return (
    <div className="flex gap-6 mb-6 text-[13px] text-[#5b5b5b]">
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">{installs}</strong>
        <span>{installs} installs</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          ${(live.price_cents / 100).toFixed(2)}
        </strong>
        <span>price</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          v{live.version}
        </strong>
        <span>current version</span>
      </div>
    </div>
  );
}

function Tabs({
  tab, setTab, showPending,
}: { tab: Tab; setTab: (t: Tab) => void; showPending: boolean }) {
  return (
    <div role="tablist" className="border-b border-[#e0dbd0] mb-5 flex gap-5">
      <TabBtn name="overview" tab={tab} setTab={setTab}>Overview</TabBtn>
      <TabBtn name="bundle" tab={tab} setTab={setTab}>Bundle</TabBtn>
      {showPending && (
        <TabBtn name="pending" tab={tab} setTab={setTab}>Pending update</TabBtn>
      )}
    </div>
  );
}

function TabBtn({
  name, tab, setTab, children,
}: { name: Tab; tab: Tab; setTab: (t: Tab) => void; children: React.ReactNode }) {
  const active = tab === name;
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={() => setTab(name)}
      className={`pb-2 text-[13px] font-semibold ${
        active ? "text-[#06402b] border-b-2 border-[#06402b]" : "text-[#5b5b5b]"
      }`}
    >
      {children}
    </button>
  );
}

function OverviewTab({ listing }: { listing: Listing }) {
  return (
    <div className="space-y-4">
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">Description</h3>
        <div className="prose prose-sm max-w-none">{listing.description_md}</div>
      </section>
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">Tags</h3>
        <div className="flex gap-2 flex-wrap">
          {(listing.tags ?? []).map(t => (
            <span key={t} className="text-[12px] px-2 py-1 rounded-full bg-[#f3eee5] text-[#5b5b5b]">
              {t}
            </span>
          ))}
        </div>
      </section>
      <section className="grid grid-cols-2 gap-4 text-[13px]">
        <Field label="Category" value={listing.category ?? "—"} />
        <Field label="Format" value={listing.format ?? "—"} />
      </section>
    </div>
  );
}

function BundleTab({ listing }: { listing: Listing }) {
  // The manifest fields aren't on the typed Listing today; cast to read
  // them. Future: extend Listing type once we add them to the backend
  // response shape.
  const meta = listing as unknown as {
    agent_id?: string;
    bundle_mcps?: string[];
    bundle_skills?: string[];
  };
  return (
    <div className="space-y-4">
      <Field label="Snapshotted from agent" value={meta.agent_id ?? "—"} />
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">MCPs bundled</h3>
        <ul className="text-[13px] space-y-1">
          {(meta.bundle_mcps ?? []).length === 0
            ? <li className="text-[#8a8578] italic">None</li>
            : meta.bundle_mcps!.map(m => <li key={m}>{m}</li>)}
        </ul>
      </section>
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">Skills bundled</h3>
        <ul className="text-[13px] space-y-1">
          {(meta.bundle_skills ?? []).length === 0
            ? <li className="text-[#8a8578] italic">None</li>
            : meta.bundle_skills!.map(s => <li key={s}>{s}</li>)}
        </ul>
      </section>
    </div>
  );
}

function PendingTab({ pending }: { pending: Listing }) {
  return (
    <div>
      <p className="text-[13px] text-[#5b5b5b]">Pending tab content placeholder — filled in Task 8.</p>
      <p className="text-[13px] text-[#5b5b5b] mt-1">v{pending.version} · {pending.status}</p>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <span className="block text-[11.5px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-1">{label}</span>
      <span className="text-[13px]">{value}</span>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/marketplace/ListingDetailView.tsx apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx
git commit -m "feat(marketplace): ListingDetailView shell with overview + bundle tabs"
```

---

## Task 8: Frontend — Pending Update tab + Action buttons

**Files:**
- Modify: `apps/frontend/src/components/marketplace/ListingDetailView.tsx`
- Modify: `apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx`

**Context:** Fills in the Pending Update tab body (rejection notes, "Continue editing" CTA) and adds the action button row in the header (Republish, Discard, View on marketplace). Republish triggers `republishListing` then opens the wizard at the new version. Discard calls `discardListingVersion` and refetches.

- [ ] **Step 1: Write the failing tests**

Append to `ListingDetailView.test.tsx`:

```tsx
describe("<ListingDetailView> actions", () => {
  it("Republish button disabled with tooltip when pending row exists", async () => {
    // Mock myListings to return both v=1 published + v=2 draft
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [
        { ...baseListing, version: 1, status: "published" },
        { ...baseListing, version: 2, status: "draft" },
      ],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText(/Live/)).toBeInTheDocument());
    const btn = screen.getByRole("button", { name: /republish/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title");
  });

  it("Republish click POSTs republish and opens wizard", async () => {
    vi.mocked(republishListing).mockResolvedValueOnce({ ...baseListing, version: 2, status: "draft" });
    const startEditListing = vi.fn();
    // Provider context mocked via test util that injects startEditListing
    renderWithDrawerProvider(<ListingDetailView listingId="lst_x" mode="drawer" />, { startEditListing });
    await waitFor(() => expect(screen.getByText(/Live/)).toBeInTheDocument());
    const btn = screen.getByRole("button", { name: /republish/i });
    btn.click();
    await waitFor(() => expect(republishListing).toHaveBeenCalledWith(
      expect.objectContaining({ listingId: "lst_x" })
    ));
    await waitFor(() => expect(startEditListing).toHaveBeenCalled());
  });

  it("Pending Update tab shows rejection notes verbatim", async () => {
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [
        { ...baseListing, version: 1, status: "published" },
        {
          ...baseListing, version: 2, status: "draft",
          rejection_notes: "Description is too vague — be specific about what the agent does.",
          rejected_at: "2026-05-22T10:00:00Z",
        },
      ],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /pending update/i })).toBeInTheDocument());
    screen.getByRole("tab", { name: /pending update/i }).click();
    expect(screen.getByText(/Description is too vague/)).toBeInTheDocument();
  });

  it("Discard button calls discardListingVersion and refetches", async () => {
    vi.mocked(myListings)
      .mockResolvedValueOnce({  // initial load
        items: [
          { ...baseListing, version: 1, status: "published" },
          { ...baseListing, version: 2, status: "draft" },
        ],
      })
      .mockResolvedValueOnce({  // after discard
        items: [{ ...baseListing, version: 1, status: "published" }],
      });
    vi.mocked(discardListingVersion).mockResolvedValueOnce(undefined);
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /discard/i })).toBeInTheDocument());
    screen.getByRole("button", { name: /discard/i }).click();
    await waitFor(() => expect(discardListingVersion).toHaveBeenCalledWith(
      expect.objectContaining({ listingId: "lst_x", version: 2 })
    ));
  });
});
```

- [ ] **Step 2: Implement the actions row and PendingTab**

In `ListingDetailView.tsx`:

1. Replace the `Header` component to include action buttons on the right:

```tsx
import { useMarketplaceDrawer } from "./drawer/MarketplaceDrawerProvider";
import { republishListing, discardListingVersion } from "@/lib/marketplace/api";

function Header({
  live, pending, onPendingChange,
}: {
  live: Listing;
  pending: Listing | null;
  onPendingChange: () => void;
}) {
  const { getToken } = useAuth();
  const { startEditListing } = useMarketplaceDrawer();
  const [busy, setBusy] = useState(false);
  const pendingBadge = /* … same as before … */;

  async function handleRepublish() {
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) return;
      const draft = await republishListing({ listingId: live.listing_id, jwt });
      // Hand off to wizard with the new draft row
      startEditListing(draft);
    } finally {
      setBusy(false);
    }
  }

  async function handleDiscard() {
    if (!pending) return;
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) return;
      await discardListingVersion({
        listingId: live.listing_id, version: pending.version, jwt,
      });
      onPendingChange();  // triggers parent refetch
    } finally {
      setBusy(false);
    }
  }

  const canRepublish = !pending;
  const canDiscard = pending && pending.status === "draft";

  return (
    <header className="mb-6 flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h2 className="font-editorial-serif text-3xl text-[#1a1a1a]">{live.name}</h2>
        <p className="text-[12.5px] text-[#5b5b5b] mt-1">{live.slug}</p>
        <span className="inline-block mt-2 text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full bg-[#e3efe6] text-[#06402b]">
          Live {pendingBadge}
        </span>
      </div>
      <div className="flex flex-col items-end gap-2">
        <button
          type="button"
          onClick={handleRepublish}
          disabled={!canRepublish || busy}
          title={!canRepublish ? `Republish blocked — v${pending!.version} is ${pending!.status}` : undefined}
          className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold disabled:opacity-50"
        >
          Republish with updates
        </button>
        {canDiscard && (
          <button
            type="button"
            onClick={handleDiscard}
            disabled={busy}
            className="text-[12.5px] font-semibold text-[#8a3a2a] hover:underline"
          >
            Discard pending update
          </button>
        )}
        <a
          href={`/marketplace/listing/${live.slug}`}
          className="text-[12.5px] font-semibold text-[#06402b] hover:underline"
        >
          View on marketplace →
        </a>
      </div>
    </header>
  );
}
```

2. Lift the `pending` refetch into the parent component:

```tsx
export function ListingDetailView({ listingId, mode }: Props) {
  // … existing state …
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    // existing fetch effect; add refreshKey to deps
  }, [listingId, userId, getToken, refreshKey]);

  // …
  <Header live={live} pending={pending} onPendingChange={() => setRefreshKey(k => k + 1)} />
  // …
}
```

3. Replace `PendingTab`:

```tsx
function PendingTab({ pending }: { pending: Listing }) {
  const { startEditListing } = useMarketplaceDrawer();
  const wasRejected = pending.status === "draft" && !!pending.rejection_notes;
  return (
    <div className="space-y-4">
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          v{pending.version} {wasRejected ? "rejected" : pending.status}
        </h3>
        <p className="text-[13px] text-[#5b5b5b]">
          Created {new Date(pending.created_at).toLocaleDateString()}
        </p>
      </section>

      {wasRejected && (
        <div
          role="status"
          data-testid="rejection-notes"
          className="rounded-xl border border-[#f0b3a8] bg-[#fbe9e7] px-4 py-3 text-[13px] text-[#8a3a2a]"
        >
          <strong className="font-semibold block mb-1">Rejection reason</strong>
          {pending.rejection_notes}
        </div>
      )}

      <button
        type="button"
        onClick={() => startEditListing(pending)}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold"
      >
        {wasRejected ? "Revise & resubmit →" : "Continue editing →"}
      </button>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/marketplace/ListingDetailView.tsx apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx
git commit -m "feat(marketplace): pending update tab + republish/discard actions"
```

---

## Task 9: Frontend — Drawer provider + drawer view state

**Files:**
- Modify: `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx`
- Modify: `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx`

**Context:** Add a new `"detail"` view to the drawer provider's `view` state, with a `listingId` payload, plus the action `openListingDetail`. The `MarketplaceDrawer` renders `<ListingDetailView mode="drawer">` when the view is `"detail"`.

- [ ] **Step 1: Read existing provider**

```bash
grep -n "view\|setView\|startEditListing" apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx
```

Identify the current `view` type union and the context shape.

- [ ] **Step 2: Extend the provider**

In `MarketplaceDrawerProvider.tsx`:

```tsx
// Existing view union — extend
type View = "browse" | "myListings" | "publish" | "detail";

interface DrawerState {
  view: View;
  editingListing?: Listing | null;
  detailListingId?: string | null;
}

// Action additions
const openListingDetail = useCallback((listingId: string) => {
  setState({ view: "detail", detailListingId: listingId });
}, []);

// Add to context value
return (
  <MarketplaceDrawerContext.Provider
    value={{
      ...state,
      setView: (v) => setState({ view: v }),
      startEditListing,
      openListingDetail,
    }}
  >
    {children}
  </MarketplaceDrawerContext.Provider>
);
```

- [ ] **Step 3: Update the drawer switch**

In `MarketplaceDrawer.tsx`, add a branch:

```tsx
import { ListingDetailView } from "../ListingDetailView";

// In the view switch:
case "detail":
  if (!detailListingId) return null;
  return (
    <div>
      <button
        type="button"
        onClick={() => setView("myListings")}
        className="text-[12.5px] font-semibold text-[#06402b] hover:underline mb-3"
      >
        ← Back to my listings
      </button>
      <ListingDetailView listingId={detailListingId} mode="drawer" />
    </div>
  );
```

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx
git commit -m "feat(marketplace): drawer detail view state + back button"
```

---

## Task 10: Frontend — `MyListingsView` grouping + row click + status pill

**Files:**
- Modify: `apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx`
- Modify: `apps/frontend/src/components/marketplace/drawer/__tests__/MyListingsView.test.tsx`

**Context:** Group rows by `listing_id` client-side; render one row per listing. Whole row is clickable → `openListingDetail`. Pill shows `Live` or `Live · v2 in review` or `Live · v2 rejected`. Drop the inline Edit button — the detail view is the single door.

- [ ] **Step 1: Write the failing tests**

Append to `MyListingsView.test.tsx`:

```tsx
it("groups rows by listing_id and renders one row per listing", async () => {
  vi.mocked(myListings).mockResolvedValueOnce({
    items: [
      { listing_id: "lst_a", version: 1, status: "published", name: "Agent A", ... },
      { listing_id: "lst_a", version: 2, status: "review", name: "Agent A", ... },
      { listing_id: "lst_b", version: 1, status: "published", name: "Agent B", ... },
    ],
  });
  render(<MyListingsView />, { wrapper: DrawerProviderMock });
  await waitFor(() => expect(screen.getAllByTestId("my-listings-row")).toHaveLength(2));
});

it("Live row with v2 in review shows combined pill", async () => {
  vi.mocked(myListings).mockResolvedValueOnce({
    items: [
      { listing_id: "lst_a", version: 1, status: "published", name: "A", ... },
      { listing_id: "lst_a", version: 2, status: "review", name: "A", ... },
    ],
  });
  render(<MyListingsView />, { wrapper: DrawerProviderMock });
  await waitFor(() => expect(screen.getByText(/Live/)).toBeInTheDocument());
  expect(screen.getByText(/v2 in review/i)).toBeInTheDocument();
});

it("clicking a row opens the listing detail view", async () => {
  const openListingDetail = vi.fn();
  vi.mocked(myListings).mockResolvedValueOnce({
    items: [{ listing_id: "lst_a", version: 1, status: "published", name: "A", ... }],
  });
  render(<MyListingsView />, {
    wrapper: ({ children }) => (
      <DrawerProviderMock openListingDetail={openListingDetail}>{children}</DrawerProviderMock>
    ),
  });
  await waitFor(() => expect(screen.getByTestId("my-listings-row")).toBeInTheDocument());
  screen.getByTestId("my-listings-row").click();
  expect(openListingDetail).toHaveBeenCalledWith("lst_a");
});
```

- [ ] **Step 2: Update MyListingsView.tsx**

Replace the post-fetch list construction with the grouping:

```tsx
import { useMemo } from "react";

// inside MyListingsView, after listings is set:
const grouped = useMemo(() => {
  if (!listings) return [];
  const byId = new Map<string, { live: Listing | null; pending: Listing | null }>();
  for (const l of listings) {
    const entry = byId.get(l.listing_id) ?? { live: null, pending: null };
    if (l.status === "published") entry.live = l;
    else if (l.status === "draft" || l.status === "review") entry.pending = l;
    // If we have only a non-published row (no live yet), use it as the "live"
    // for display purposes — that listing hasn't been published yet.
    else if (!entry.live) entry.live = l;
    byId.set(l.listing_id, entry);
  }
  return Array.from(byId.values())
    .filter(e => e.live !== null)
    .sort((a, b) => (b.live!.created_at ?? "").localeCompare(a.live!.created_at ?? ""));
}, [listings]);
```

Then rewrite the `ListingRow` rendering:

```tsx
// Replace the existing .map():
{grouped.map(({ live, pending }) => (
  <ListingRow
    key={live!.listing_id}
    live={live!}
    pending={pending}
    onClick={() => openListingDetail(live!.listing_id)}
  />
))}
```

And the `ListingRow` itself:

```tsx
function ListingRow({
  live, pending, onClick,
}: { live: Listing; pending: Listing | null; onClick: () => void }) {
  const wasRejected = pending?.status === "draft" && !!pending?.rejection_notes;
  const installCount = (live as unknown as { install_count?: number }).install_count ?? 0;

  const baseStatusLabel =
    live.status === "published" ? "Live"
    : live.status === "review" ? "In review"
    : live.status === "draft" && live.rejection_notes ? "Rejected"
    : live.status === "draft" ? "Draft"
    : live.status;

  const pendingLabel = pending && pending.status === "review" ? `· v${pending.version} in review`
    : wasRejected ? `· v${pending!.version} rejected`
    : null;

  return (
    <button
      type="button"
      data-testid="my-listings-row"
      onClick={onClick}
      className={`w-full text-left bg-white border rounded-2xl p-5 flex items-start gap-4 hover:bg-[#fbf9f4] transition-colors ${
        wasRejected ? "border-[#f0b3a8] bg-[#fff8f5]" : "border-[#e0dbd0]"
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="font-editorial-serif text-lg">{live.name}</div>
        <div className="flex gap-3 text-[12.5px] text-[#5b5b5b] mt-0.5">
          <span>Updated {new Date(live.created_at).toLocaleDateString()}</span>
          <span>{installCount} installs</span>
          <span className="font-semibold text-[#06402b]">
            {live.price_cents === 0 ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>
      <div className="flex flex-col items-end gap-2 flex-shrink-0">
        <span
          className={`text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full whitespace-nowrap ${
            live.status === "published" ? "bg-[#e3efe6] text-[#06402b]"
            : live.status === "review" ? "bg-[#fef4e3] text-[#a07520]"
            : wasRejected ? "bg-[#fbe9e7] text-[#8a3a2a]"
            : "bg-[#f3eee5] text-[#5b5b5b]"
          }`}
        >
          {baseStatusLabel}{pendingLabel ? ` ${pendingLabel}` : ""}
        </span>
      </div>
    </button>
  );
}
```

Drop the now-unused `startEditListing` prop and the inline Edit button completely.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/MyListingsView.tsx apps/frontend/src/components/marketplace/drawer/__tests__/MyListingsView.test.tsx
git commit -m "feat(marketplace): MyListings groups by listing_id + row opens detail"
```

---

## Task 11: Frontend — PublishWizard republish entry

**Files:**
- Modify: `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`
- Modify: `apps/frontend/src/components/marketplace/drawer/__tests__/PublishWizardEdit.test.tsx`

**Context:** The wizard already accepts `editingListing` for edit-mode (in-review demoted to draft case). For republish, `startEditListing(draft)` passes the v=N+1 draft row directly. The wizard's existing PATCH / artifact-from-agent / submit calls need to use `editingListing.version` (currently they may default to `1`).

- [ ] **Step 1: Read the wizard's API call sites**

```bash
grep -n "version\|editingListing" apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx
```

Identify each call to PATCH/artifact-from-agent/submit and verify the `version` parameter is threaded from `editingListing.version`. The comments in the file (per the explore earlier) mention `v_new=2` so the structure is partially there.

- [ ] **Step 2: Write the failing test**

Append to `PublishWizardEdit.test.tsx`:

```tsx
it("republish entry: wizard uses editingListing.version=2 for PATCH", async () => {
  const patchSpy = vi.fn().mockResolvedValue({});
  vi.mocked(useApi).mockReturnValue({ patch: patchSpy, post: vi.fn(), get: vi.fn() });
  const draftV2: Listing = {
    listing_id: "lst_x", version: 2, status: "draft",
    name: "X", description_md: "d", price_cents: 0, tags: [], category: "agent",
    format: "openclaw_agent", slug: "x", seller_id: "seller_1", created_at: "2026-05-23T00:00:00Z",
  };
  render(<PublishWizard editingListing={draftV2} />, { wrapper: DrawerProviderMock });
  // Walk through wizard, hit Save on the last step
  // …
  await waitFor(() => expect(patchSpy).toHaveBeenCalledWith(
    expect.stringContaining("?version=2"),
    expect.any(Object),
  ));
});

it("republish entry: artifact-from-agent uses version=2", async () => {
  // similar, asserting the POST URL contains version=2
});

it("republish entry: submit uses version=2", async () => {
  // similar
});
```

- [ ] **Step 3: Thread version through all wizard mutations**

In `PublishWizard.tsx`, locate each API call (PATCH update, POST artifact-from-agent, POST submit). Replace any hardcoded `version=1` with the version from `editingListing` (default to `1` only when no `editingListing` is present — initial-publish path):

```tsx
const version = editingListing?.version ?? 1;

// Update calls:
await api.patch(`/marketplace/listings/${listingId}?version=${version}`, payload);
await api.post(`/marketplace/listings/${listingId}/artifact-from-agent?version=${version}`, body);
await api.post(`/marketplace/listings/${listingId}/submit?version=${version}`);
```

Verify the wizard reads `editingListing` from drawer context (already true today per the explore findings).

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx apps/frontend/src/components/marketplace/drawer/__tests__/PublishWizardEdit.test.tsx
git commit -m "feat(marketplace): wizard threads editingListing.version for republish"
```

---

## Task 12: Frontend — `/manage` page + Owner CTA on public listing

**Files:**
- Create: `apps/frontend/src/app/marketplace/listing/[slug]/manage/page.tsx`
- Create: `apps/frontend/src/app/marketplace/listing/[slug]/manage/__tests__/page.test.tsx`
- Modify: `apps/frontend/src/app/marketplace/listing/[slug]/page.tsx` (add owner CTA)

**Context:** Owner-gated full-page surface that mounts `<ListingDetailView mode="page">`. Non-owners redirect to the public listing page. The public listing page itself gets a small "Manage your listing →" CTA when viewer is the seller.

- [ ] **Step 1: Write the failing page test**

```tsx
// apps/frontend/src/app/marketplace/listing/[slug]/manage/__tests__/page.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ManagePage from "../page";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "seller_1", getToken: async () => "test-jwt" }),
}));
vi.mock("@/lib/marketplace/api", () => ({
  getListingBySlug: vi.fn(),
  myListings: vi.fn().mockResolvedValue({ items: [] }),
}));

const baseListing = {
  listing_id: "lst_x", version: 1, status: "published" as const,
  name: "Agent X", description_md: "d", slug: "x", seller_id: "seller_1",
  price_cents: 500, tags: [], category: "agent", format: "openclaw_agent",
  created_at: "2026-05-23T00:00:00Z",
};

describe("/marketplace/listing/[slug]/manage", () => {
  it("renders ListingDetailView for the owner", async () => {
    vi.mocked(getListingBySlug).mockResolvedValueOnce(baseListing);
    render(<ManagePage params={{ slug: "x" }} />);
    await waitFor(() => expect(screen.getByText("Agent X")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /republish/i })).toBeInTheDocument();
  });

  it("redirects non-owner to public listing page", async () => {
    const otherListing = { ...baseListing, seller_id: "someone_else" };
    vi.mocked(getListingBySlug).mockResolvedValueOnce(otherListing);
    const replaceSpy = vi.fn();
    vi.spyOn(window.location, "replace" as never).mockImplementation(replaceSpy as never);
    render(<ManagePage params={{ slug: "x" }} />);
    await waitFor(() => expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/x"));
  });
});
```

- [ ] **Step 2: Implement the manage page**

```tsx
// apps/frontend/src/app/marketplace/listing/[slug]/manage/page.tsx
"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { getListingBySlug } from "@/lib/marketplace/api";
import { ListingDetailView } from "@/components/marketplace/ListingDetailView";
import type { Listing } from "@/lib/marketplace/types";

export default function ManagePage({ params }: { params: { slug: string } }) {
  const { userId, getToken } = useAuth();
  const [listing, setListing] = useState<Listing | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt || !userId) return;
      const l = await getListingBySlug({ slug: params.slug, jwt });
      if (!alive) return;
      if (l.seller_id !== userId) {
        setForbidden(true);
        window.location.replace(`/marketplace/listing/${params.slug}`);
        return;
      }
      setListing(l);
    })();
    return () => { alive = false; };
  }, [params.slug, userId, getToken]);

  if (forbidden) return null;
  if (!listing) return <p className="p-8">Loading…</p>;

  return (
    <main className="min-h-screen bg-[#fbf9f4]">
      <nav className="border-b border-[#e0dbd0] px-6 py-3">
        <a href="/marketplace" className="text-[12.5px] font-semibold text-[#06402b] hover:underline">
          ← Marketplace
        </a>
      </nav>
      <ListingDetailView listingId={listing.listing_id} mode="page" />
    </main>
  );
}
```

- [ ] **Step 3: Add owner CTA on public listing page**

In `apps/frontend/src/app/marketplace/listing/[slug]/page.tsx`, identify where the listing data is rendered and inject the CTA conditionally:

```tsx
import { useAuth } from "@clerk/nextjs";

// In the component body, after listing is loaded:
const { userId } = useAuth();
const isOwner = userId === listing?.seller_id;

// In the JSX, near the top of the listing detail:
{isOwner && (
  <a
    href={`/marketplace/listing/${listing.slug}/manage`}
    className="inline-block mb-4 text-[12.5px] font-semibold text-[#06402b] hover:underline"
  >
    Manage your listing →
  </a>
)}
```

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/app/marketplace/listing/[slug]/manage/ apps/frontend/src/app/marketplace/listing/[slug]/page.tsx
git commit -m "feat(marketplace): /manage page + owner CTA on public listing"
```

---

## Task 13: Final verification — full test suites + manual flow

**Files:** (none modified unless tests reveal regressions)

**Context:** Run the full backend and frontend test suites once and address any failures. Then manually walk through the publish → republish → republish-again cycle, plus the takedown cascade, to ensure end-to-end the feature works.

- [ ] **Step 1: Run backend test suite**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py tests/unit/services/test_takedown_service.py tests/unit/routers/test_marketplace_listings.py tests/unit/routers/test_marketplace_admin.py -v
```

Expected: all pass. If any fail, address in place (likely fixture drift); commit fixes separately.

- [ ] **Step 2: Run full backend suite**

```bash
cd apps/backend && uv run pytest tests/ -v
```

Expected: all pass (including the previously-untouched files — ensures rename didn't break any caller).

- [ ] **Step 3: Run frontend test suite**

```bash
cd apps/frontend && pnpm test
```

Expected: all pass.

- [ ] **Step 4: Run lint**

```bash
turbo run lint
```

Expected: clean.

- [ ] **Step 5: Manual verification — local dev**

Start LocalStack + backend + frontend:

```bash
./scripts/local-dev.sh
```

Then in a browser:
1. Sign up as a test seller; provision container
2. Create an agent in `/chat`
3. Open marketplace drawer → "+ New listing"; publish through the wizard
4. Self-deploy and approve via `/admin/marketplace` (admin bypass)
5. Click the now-Live row in My Listings → verify `<ListingDetailView>` opens in drawer
6. Click "Republish with updates" → wizard opens at v=2 → make a change (e.g. add a skill to the agent first, then re-snapshot) → submit
7. Approve v=2 via admin panel → verify DDB: v=1 retired, v=2 published
8. Repeat republish to v=3, confirming vN scaling
9. Start v=4 republish, then trigger takedown of v=3 from admin panel → verify v=4 cascades to taken_down
10. Navigate to `/marketplace/listing/{slug}/manage` directly → owner sees full-page detail view; non-owner is redirected to public page

- [ ] **Step 6: Verify in DDB**

```bash
aws dynamodb scan --table-name isol8-local-marketplace-listings --profile localstack --endpoint-url http://localhost:4566 --output json | jq '.Items[] | {listing_id: .listing_id.S, version: .version.N, status: .status.S}'
```

Expected: each listing has one row per version, with the correct terminal/non-terminal distribution.

- [ ] **Step 7: Commit any test fixes**

```bash
git status
# If anything was modified to fix tests, commit it:
git add <paths>
git commit -m "test(marketplace): fix fixture drift surfaced by republish suite"
```

- [ ] **Step 8: Open PR**

```bash
git push -u origin worktree-feat+marketplace-republish
gh pr create --title "feat(marketplace): listing management + republish" --body "$(cat <<'EOF'
## Summary

- Adds `<ListingDetailView>` shared by the `/chat` drawer and a new `/marketplace/listing/[slug]/manage` page so sellers can finally inspect and manage their listings
- Wires up the long-planned versioned republish flow — v(N) stays Live while v(N+1) goes through draft → review → approval, atomically swapped on publish
- Renames the misleadingly-named `publish_v2` to `publish_new_version` (function was already generic over `(prev, new)`)
- Cascades takedowns to any non-terminal v(N+1) row so in-flight republishes can't orphan

Spec: `docs/superpowers/specs/2026-05-23-marketplace-listing-management-design.md`
Plan: `docs/superpowers/plans/2026-05-23-marketplace-republish.md`

## Test plan

- [ ] Backend unit suite passes locally
- [ ] Frontend unit suite passes locally
- [ ] Lint clean
- [ ] Manual: publish → republish v=2 → republish v=3 in local dev
- [ ] Manual: takedown cascade on in-flight v=N+1 in local dev
- [ ] CI passes on PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Notes for the executing agent

- The user's worktree workflow memory says: edits + commits happen on the worktree branch, never on main. This plan is being executed in the worktree `feat+marketplace-republish`.
- The user prefers PRs over direct main pushes; the final task opens the PR. Do not merge from within the agent.
- Tests are deferred to Task 13 by user preference (`feedback_run_tests_at_end.md`). Per-task TDD steps still write tests, but the suite run is end-of-plan only.
- If any task surfaces a spec ambiguity or a previously-unknown constraint, pause and surface it — don't paper over with assumptions.
- DDB tests in this repo use a `dynamodb_table` fixture (see `apps/backend/tests/unit/services/test_marketplace_service.py` for existing patterns). Reuse it; don't invent new test infrastructure.
