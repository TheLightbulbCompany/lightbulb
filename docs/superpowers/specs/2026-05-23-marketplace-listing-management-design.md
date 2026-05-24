# Marketplace Listing Management & Republish

**Date:** 2026-05-23
**Status:** Spec, awaiting implementation plan
**Author:** prasiddha@gmail.com

## Problem

Sellers can publish marketplace listings today, but once a listing reaches `published` ("Live") state there is no way to:

1. See the listing's detail/management view from anywhere in the product
2. Edit the listing's metadata
3. Re-snapshot the underlying agent to pick up newly-added skills, MCPs, or persona changes
4. Track the history of versions shipped

Concretely, the current `MyListingsView` (in the `/chat` marketplace drawer) renders Live rows with no click target and no action buttons; the public `/marketplace/listing/[slug]` page is buyer-oriented; and the backend's `PATCH /listings/{id}` rejects with 409 on any non-draft row.

The version-aware storage layer (composite key `(listing_id, version)`, atomic `publish_new_version` transaction) was built in anticipation of a republish flow but is currently unused — every call hardcodes `version=1`.

## Goals

- Sellers can open a full detail view of any of their listings from the `/chat` drawer and from a dedicated `/marketplace/listing/[slug]/manage` page
- Sellers can republish a Live listing with updated metadata and/or a fresh agent snapshot, going through the same admin review queue as initial publishes
- The currently-Live version stays Live the entire time a new version is being drafted, reviewed, or rejected — buyers see no gap
- The system scales naturally to v3, v4, vN with no special-casing for v=2

## Non-goals

- No automatic version-diffing UI ("what changed between v3 and v4")
- No "rollback to v(N-1)" affordance
- No per-version pricing history graph
- No buyer notification when a new version ships
- No deeplinking for the drawer-detail view (only the `/manage` page is URL-addressable)
- No new admin-side UI — admin reviews v(N+1) submissions through the existing review queue

## Architecture

### Storage

DDB `marketplace-listings` table is keyed by `(listing_id, version)` and that already supports arbitrary version counts. Each republish inserts one new row at `version = max(version) + 1`. Older versions stay in the table with terminal status (`retired` / `taken_down`) — they are the version history.

S3 artifacts are versioned per row at `s3://${MARKETPLACE_ARTIFACTS_BUCKET}/listings/{id}/v{n}/`. Retired versions' artifacts are never garbage-collected — buyers who installed v(N) keep working even after v(N+1) ships, because deploy is a one-time snapshot.

### State machine

```
                ┌──────────────────────────────────────────────────┐
  create_draft  │                                                  ▼
────────────────▶ draft ──submit──▶ review ──approve──▶ published ──┐
                    ▲                  │                  │         │
                    │              reject (notes)         │   retire (admin)
                    └──────────────────┘                  │         │
                                                          ▼         ▼
                                                      retired   taken_down
```

### Republish flow

```
v(N) published (Live)
  │
  ├──► seller clicks "Republish" in <ListingDetailView>
  │
  ▼
v(N+1) draft created (metadata cloned from v(N); artifact slot empty)
  │
  ├──► seller edits metadata in PublishWizard (existing component, version=N+1)
  ├──► seller re-snapshots agent (POST /artifact-from-agent?version=N+1)
  ├──► seller submits (POST /submit?version=N+1)
  │
  ▼
v(N+1) review (admin queue surfaces it; admin's existing approve flow handles it)
  │
  ├──► reject(notes) ──► v(N+1) back to draft, v(N) still Live, seller revises and resubmits
  │
  ▼
v(N+1) approved → publish_new_version(prev=N, new=N+1)
  │
  ▼
v(N) retired + v(N+1) published (atomic via TransactWriteItems)
```

### Invariants

1. **At most one row per `listing_id` can be in `published` state** — enforced by `publish_new_version`'s transaction guards
2. **At most one row per `listing_id` can be in a non-terminal state** (`draft` or `review`) — enforced by a new transaction guard in `create_new_version`. This prevents sellers from spawning v(N+1) and v(N+2) drafts simultaneously, which would race at approve time
3. **Slug is immutable across versions** — the slug-reservation sentinel row at `version=0` is unaffected by republish
4. **Retired versions' artifacts stay in S3 forever** — buyer recovery and deploy auditability

## Backend changes

### New: `marketplace_service.create_new_version`

```python
async def create_new_version(*, listing_id: str, seller_id: str) -> dict:
    """Clone the currently-published row into a new draft at version+1.

    Guards (TransactWriteItems):
      - A row with status='published' exists for listing_id
        (else NoPublishedVersionError)
      - No row with status in {draft, review} exists for listing_id
        (else RepublishAlreadyInProgressError naming the offending vN)
      - seller_id matches the published row's seller_id
        (else PermissionError, surfaced as 403)

    Inserts a new row at version=max(version)+1 with:
      status='draft'
      name/description/price/tags/category copied from the live row
      artifact_sha256, manifest_sha256, openclaw_slice cleared
        (seller must call /artifact-from-agent before /submit)
      rejection_notes / rejected_at cleared
      created_at = updated_at = now

    Returns the new row.
    """
```

### Renamed: `publish_v2` → `publish_new_version`

The existing function is already generic over `(prev_version, new_version)` — `v2` in the name is misleading. Rename it for clarity. No signature change.

### New: `POST /listings/{listing_id}/republish`

```python
@router.post("/listings/{listing_id}/republish", response_model=schemas.Listing)
async def republish(listing_id, auth):
    require_org_admin(auth)
    try:
        return await marketplace_service.create_new_version(
            listing_id=listing_id, seller_id=auth.user_id,
        )
    except marketplace_service.NoPublishedVersionError:
        raise HTTPException(409, "no live version to republish")
    except marketplace_service.RepublishAlreadyInProgressError as e:
        raise HTTPException(409, str(e))
```

### New: `DELETE /listings/{listing_id}?version=N`

Allows the seller to discard a v(N+1) draft (e.g., started a republish and changed their mind). Allowed only on `draft` status. Rejects review/published/retired/taken_down.

### Modified: admin approve

The existing admin approve action calls `marketplace_service.approve` (sets `draft → published`). On approval of a row at `version=N`, the new logic is:

1. Look up the unique row with `status='published'` for this `listing_id` (invariant 1 guarantees uniqueness)
2. If found, call `publish_new_version(prev=<that version>, new=N)` — atomically retires the old and publishes the new
3. If not found (no live version — happens only for the initial publish at v=1), call the existing `approve` path

Looking up by status (not by `version - 1`) avoids edge cases around discarded or skipped version numbers and reflects the actual invariant.

### Modified: admin retire cascade

When admin retires a `published` row, any non-terminal `draft`/`review` row at v(N+1) is also retired in the same TransactWriteItems. This handles the race where a seller is mid-republish when admin pulls the live version.

### Reused unchanged

- `PATCH /listings/{id}?version=N` — wizard edits metadata (existing, already version-aware)
- `POST /listings/{id}/artifact-from-agent?version=N` — wizard re-snapshots agent (existing)
- `POST /listings/{id}/submit?version=N` — wizard submits (existing)
- `GET /listings/{slug}` — buyer-facing detail, already returns highest-version published row
- `POST /listings/{slug}/deploy` — buyer-facing install, already version-aware
- `GET /me/listings` — seller list. Returns flat list of all rows for the seller; the frontend groups by `listing_id`

## Frontend changes

### New: `<ListingDetailView>`

Single shared component at `apps/frontend/src/components/marketplace/ListingDetailView.tsx`.

Props: `{ listingId: string, mode: "drawer" | "page" }`.

Loads live + pending rows via existing endpoints (`GET /listings/{slug}` and a `GET /me/listings` filtered by `listing_id`).

Renders:

- **Header:** name, slug, status pill (`Live`, with sub-badge `· v2 in review` or `· v2 rejected` when a pending row exists)
- **Stats strip:** install count, lifetime earnings, last updated
- **Tabs:**
  - **Overview** — description (markdown), price, tags, category, format
  - **Bundle** — agent_id snapshotted from, MCPs bundled, skills bundled, persona file count (read from manifest)
  - **Pending update** (only when a pending row exists) — status, rejection_notes verbatim, "Continue editing →" / "Revise & resubmit →" CTA opening the wizard
- **Actions (top right):**
  - `Republish with updates` — `POST /listings/{id}/republish` → open wizard at v(N+1). Disabled with tooltip when pending exists
  - `Discard pending update` — `DELETE /listings/{id}?version=N` (visible only when pending draft exists, not pending review)
  - `View on marketplace →` — link to `/marketplace/listing/{slug}`

`mode="drawer"` vs `mode="page"` differ only in chrome (width, breadcrumb/back affordance, typography scale). Same data, same actions.

### `MyListingsView.tsx` changes

- Group fetched rows by `listing_id` client-side; render one row per listing
- Status pill: `Live`, `Live · v2 in review`, `Live · v2 rejected`, `Draft`, `In review`, `Rejected`
- Whole row is clickable → calls `openListingDetail(listing_id)` on the drawer provider
- Drop the inline "Edit →" button entirely; one door (the detail view) instead of two

### `MarketplaceDrawerProvider.tsx` changes

- Add view state: `"detail"` with `listingId` param
- Add action: `openListingDetail(listingId)`
- `startEditListing(listing)` keeps working — `ListingDetailView` calls it for the "Continue editing" CTA on pending rows

### `MarketplaceDrawer.tsx` changes

- New view branch: `case "detail": return <ListingDetailView mode="drawer" listingId={...} />`
- Detail view renders its own back-to-list button in the drawer header

### New: `/marketplace/listing/[slug]/manage` page

New file: `apps/frontend/src/app/marketplace/listing/[slug]/manage/page.tsx`.

- Clerk-gated. On mount: resolve slug → `listing_id`, fetch listing, verify `seller_id === auth.userId`
- Non-owner: redirect to public `/marketplace/listing/[slug]` (no flash of the manage UI)
- Owner: render `<ListingDetailView mode="page" listingId={...} />`
- "Republish with updates" from the page opens the existing drawer overlaying the page (`openDrawer({ view: "publish", editingListing })`). No wizard extraction; the drawer already overlays /chat and works identically over /marketplace

### Small cross-surface CTAs

- On public `/marketplace/listing/[slug]`, when viewer is the seller, render a small "Manage your listing →" link to the manage page. Keeps that page buyer-focused but discoverable to owners
- My Listings rows get a right-aligned `⋯` overflow button: "Open in full page →" links to `/marketplace/listing/[slug]/manage`

## Error handling

| Condition | Endpoint | Response | UI handling |
|---|---|---|---|
| No live version to republish | `POST /republish` | 409 | Button hidden when not published; race → toast + refetch |
| Republish already in progress | `POST /republish` | 409 (names offending vN) | Button disabled with tooltip when pending row exists; race → toast + refetch |
| Non-owner attempts republish | `POST /republish` | 403 | Not reachable from UI; toast + redirect to My Listings |
| Wizard PATCH on wrong-state row | `PATCH ?version=N` | 409 | Wizard refetches and reroutes |
| Source agent deleted mid-republish | `POST /artifact-from-agent` | 404 | Wizard shows error; seller can attach a different agent or discard the draft |
| Admin retires v(N) mid-republish | (cascade) | — | Backend cascade retires v(N+1) draft atomically; seller's next wizard call 404s and surfaces "live version was retired" |
| Two-tab discard race | wizard PATCH | 404 | Wizard catches, shows "this draft was discarded in another tab", closes |
| Discard pending review row | `DELETE ?version=N` | 409 | Button only visible for `draft` state (after rejection); not for `review` |

## Testing

### Backend (pytest)

- `tests/services/test_marketplace_service_versioning.py` — new file. Covers `create_new_version` happy + all guard paths, `publish_new_version` rename, v2→v3→v4 scaling
- `tests/routers/test_marketplace_listings_republish.py` — new file. Covers `POST /republish`, `DELETE ?version=N`, full wizard flow at `?version=2`
- `tests/routers/test_marketplace_admin_publish.py` — extend existing. Approve on v=2 calls `publish_new_version`, atomic flip works, admin retire cascade

### Frontend (Vitest + Testing Library)

- `MyListingsView.test.tsx` — extend. Row grouping by listing_id, status badge combinations, row-click opens detail
- `ListingDetailView.test.tsx` — new file. Tab rendering, pending-row visibility, action button enable/disable logic, mode="drawer" vs "page" snapshot diff
- `PublishWizardEdit.test.tsx` — extend. Wizard threads `?version=N+1` correctly when entered via republish
- `marketplace/listing/[slug]/manage/page.test.tsx` — new file. Owner gate, redirect on non-owner

### E2E

Skipped at MVP — existing `marketplace-publish.spec.ts` already covers the wizard end-to-end. Republish is the same wizard with a different entry point; unit tests cover the entry point.

### Manual verification before merge

1. Local dev: publish v=1, republish, edit + re-snapshot, submit, approve, verify v=1 retired + v=2 published in DDB, verify buyer deploys v=2
2. Repeat to v=3, v=4 to confirm vN scaling
3. Discard a v=2 draft mid-edit, verify clean state
4. Admin retires v=1 while v=2 in review, verify cascade

### Test execution

All tests run via CI/CD; defer local test runs to end-of-implementation.

## Out of scope

- Version history UI tab (live + pending is enough; full history can be added later from existing data)
- Version diffing
- Rollback affordance
- Pricing history graph
- Buyer notifications on new version
- Backend-side response-shape changes to `GET /me/listings` (frontend collapses client-side)
- Deeplinking to the drawer detail view
- DDB performance work for unrealistically-deep version chains
