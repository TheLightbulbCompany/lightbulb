# Marketplace UUID-Identity Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `listing_id` UUID the sole identity/uniqueness key for marketplace listings, delete the slug-reservation sentinel mechanism, and let agent names repeat freely.

**Architecture:** Today the slug (derived from the agent name) is both the public URL key and a uniqueness constraint enforced by a permanent `listing_id="slug:{slug}", version=0` sentinel row that no delete/takedown path ever cleans up — which is why republishing a taken-down agent's name 409s forever. We remove the sentinel entirely (uniqueness is now inherent in the UUID), repoint the public detail / deploy / checkout routes to resolve by `listing_id`, and switch the frontend `[slug]` routes + links to carry `listing_id`. The `slug` field survives only as a cosmetic, non-unique display/search token.

**Tech Stack:** FastAPI + boto3 DynamoDB (backend), Next.js 16 App Router + TypeScript (frontend), pytest + vitest.

---

## Background — verified root cause (live prod data, 2026-06-05)

- Slug is `slugify(agent_name)` (`PublishWizard.tsx:504`).
- `create_draft` writes a sentinel row `listing_id="slug:{slug}", version=0` guarded by `ConditionExpression="attribute_not_exists(listing_id)"` (`marketplace_service.py:142-191`). Collision → `SlugCollisionError` → 409 "slug already taken" (`marketplace_listings.py:574`).
- **No path deletes the sentinel.** Seller "delete" (`discard_listing_version`) is v≥2-only; admin takedown (`_cascade_takedown`) only flips the real row's status and operates on a different partition key. The sentinel persists forever.
- Live prod orphaned sentinels confirmed: `slug:stitch`, `slug:pulse`, `slug:ledger` — all point at `taken_down` listings, all block re-publish of those names.

## Decision

Public identity = `listing_id` (UUID). Names repeat freely. Sentinel deleted. `slug` kept as cosmetic non-unique field. (Option C from the 2026-06-05 design discussion.)

**Out of scope (deliberate):**
- Dropping the `slug` field from the DB / schema / search — keep it cosmetic; removing it is pure churn for no benefit (YAGNI).
- Dropping the now-unused `slug-version-index` GSI — destructive infra change; defer to a separate follow-up PR (see Task 9). Leaving an unused GSI is harmless.

## File Structure

**Backend (modify):**
- `apps/backend/core/services/marketplace_service.py` — remove sentinel from `create_draft`; remove `SlugCollisionError`; add `get_published_by_id`.
- `apps/backend/routers/marketplace_listings.py` — `create_listing` drop 409; `get_listing` + `deploy_listing` resolve by `listing_id`.
- `apps/backend/routers/marketplace_purchases.py` — `checkout` resolves by `listing_id`.
- `apps/backend/schemas/marketplace.py` — `CheckoutRequest.listing_slug` → `listing_id`; docstring fixes on `ListingUpdate`.

**Backend (tests, modify):**
- `apps/backend/tests/unit/services/test_marketplace_service.py`
- `apps/backend/tests/unit/routers/test_marketplace_listings.py`
- `apps/backend/tests/unit/routers/test_marketplace_purchases.py`
- `apps/backend/tests/credentials/test_marketplace_requires_credentials.py`

**Frontend (modify):**
- `apps/frontend/src/lib/marketplace/api.ts` — `getListing`/`getListingBySlug`/`checkout` by id.
- `apps/frontend/src/hooks/useCatalog.ts` — `deploy()` posts `/listings/{listing_id}/deploy`.
- `apps/frontend/src/components/marketplace/drawer/useHire.ts` — pass `listing.listing_id`.
- Route folders: `apps/frontend/src/app/marketplace/listing/[slug]/` → `[listingId]/` (+ `manage/`).
- Link sites: `MarketplaceSection.tsx`, `drawer/ListingCard.tsx`, `storefront/Listing/ListingCard.tsx`, `ListingDetailView.tsx`, `storefront/Listing/OwnerManageLink.tsx`.

**Frontend (tests, modify):**
- `apps/frontend/src/app/marketplace/listing/[listingId]/manage/__tests__/page.test.tsx`
- `apps/frontend/src/components/marketplace/drawer/__tests__/ListingCard.test.tsx`
- `apps/frontend/src/components/marketplace/__tests__/ListingDetailView.test.tsx`

**Data (one-off):**
- `apps/backend/scripts/sweep_orphan_slug_sentinels.py` — delete every `listing_id` begins_with `slug:` row.

---

## Task 1: Backend — delete the slug-reservation sentinel

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py:39-40` (remove `SlugCollisionError`), `:103-217` (`create_draft`)
- Modify: `apps/backend/routers/marketplace_listings.py:561-575` (`create_listing`)
- Test: `apps/backend/tests/unit/services/test_marketplace_service.py`

- [ ] **Step 1: Write the failing test** — two drafts with the same slug both succeed, and no `slug:` sentinel row is written.

Add to `apps/backend/tests/unit/services/test_marketplace_service.py` (the file already has a `mock_listings` fixture — see `conftest.py`):

```python
@pytest.mark.asyncio
async def test_create_draft_allows_duplicate_slug(mock_listings, monkeypatch):
    """Names repeat: two drafts may share a slug, and no sentinel row exists."""
    async def _fake_upload(**_kwargs):
        return ("listings/x/v1/", "deadbeef")
    monkeypatch.setattr(marketplace_service, "_upload_artifact_to_s3", _fake_upload)

    common = dict(
        seller_id="user_1", slug="dup", name="Dup", description_md="d",
        format="openclaw", price_cents=0, tags=[], artifact_bytes=b"",
        manifest={"name": "Dup"},
    )
    a = await marketplace_service.create_draft(**common)
    b = await marketplace_service.create_draft(**common)

    assert a["listing_id"] != b["listing_id"]
    table = marketplace_service._listings_table()
    sentinel = table.get_item(Key={"listing_id": "slug:dup", "version": 0}).get("Item")
    assert sentinel is None, "no slug-reservation sentinel should be written"
```

- [ ] **Step 2: Run it — expect FAIL** (today the second create raises `SlugCollisionError` and a sentinel exists).

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py::test_create_draft_allows_duplicate_slug -v`
Expected: FAIL (`SlugCollisionError` raised on the second create).

- [ ] **Step 3: Remove the sentinel from `create_draft`.** Replace the `try/except` TransactWriteItems block (`marketplace_service.py:140-191`) with a single `put_item` of the listing row only:

```python
    listings_tbl = settings.MARKETPLACE_LISTINGS_TABLE  # noqa: F841 retained for parity below
    client = _dynamodb_client()
    client.put_item(
        TableName=settings.MARKETPLACE_LISTINGS_TABLE,
        Item={
            "listing_id": {"S": listing_id},
            "version": {"N": "1"},
            "slug": {"S": slug},
            "name": {"S": name},
            "description_md": {"S": description_md},
            "format": {"S": format},
            "price_cents": {"N": str(price_cents)},
            "tags": {"L": [{"S": t} for t in tags]},
            **({"category": {"S": category}} if category else {}),
            "seller_id": {"S": seller_id},
            "status": {"S": "draft"},
            "s3_prefix": {"S": s3_prefix},
            "manifest_sha256": {"S": sha},
            "manifest_json": {"S": json.dumps(manifest)},
            "artifact_format_version": {"S": "v1"},
            "entitlement_policy": {"S": "perpetual"},
            "artifact_uploaded": {"BOOL": False},
            "created_at": {"S": now_iso},
            "updated_at": {"S": now_iso},
        },
    )
```

Delete the `SlugCollisionError` class (`marketplace_service.py:39-40`). Update the `create_draft` docstring (`:117-128`) to drop the sentinel/uniqueness paragraph — replace with: `"""Create a new listing in draft state. Slugs are NOT unique — listing_id (a UUID) is the sole identity key; the slug is a cosmetic display token."""`. Remove the now-stale `ListingUpdate` docstring sentence in `schemas/marketplace.py:76-78` that says slug "is reserved at create time (a separate sentinel row enforces uniqueness atomically)" → replace with "slug is set at create time and is immutable thereafter".

- [ ] **Step 4: Drop the 409 mapping in the router.** In `apps/backend/routers/marketplace_listings.py`, remove the `try/except marketplace_service.SlugCollisionError` around the `create_draft` call (`:561-575`) — call `create_draft` directly (keep the `seller_profile_required` 409 above it):

```python
    listing = await marketplace_service.create_draft(
        seller_id=auth.user_id,
        slug=payload.slug,
        name=payload.name,
        description_md=payload.description_md,
        format=payload.format,
        price_cents=payload.price_cents,
        tags=payload.tags,
        category=payload.category,
        artifact_bytes=b"",
        manifest={"name": payload.name, "description": payload.description_md},
    )
    await seller_display.attach_seller_name(listing)
    return listing
```

- [ ] **Step 5: Delete the obsolete collision test** if present. Search and remove any `test_*slug*collision*` / `SlugCollisionError` test in `test_marketplace_service.py` and `test_marketplace_listings.py`:

Run: `cd apps/backend && grep -rn "SlugCollisionError\|slug_collision\|slug already taken" tests/`
Delete each matching test function.

- [ ] **Step 6: Run the service + router create tests — expect PASS.**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py tests/unit/routers/test_marketplace_listings.py -k "create or duplicate_slug" -v`
Expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/routers/marketplace_listings.py apps/backend/schemas/marketplace.py apps/backend/tests/unit/services/test_marketplace_service.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "feat(marketplace): drop slug-reservation sentinel; names may repeat"
```

---

## Task 2: Backend — resolve public detail/deploy/checkout by listing_id

**Files:**
- Modify: `apps/backend/core/services/marketplace_service.py:807-844` (add `get_published_by_id`)
- Modify: `apps/backend/routers/marketplace_listings.py:244-292` (`get_listing`), `:295-316` (`deploy_listing`)
- Modify: `apps/backend/routers/marketplace_purchases.py:35-78` (`checkout`)
- Modify: `apps/backend/schemas/marketplace.py:118-122` (`CheckoutRequest`)
- Test: `apps/backend/tests/unit/services/test_marketplace_service.py`

- [ ] **Step 1: Write the failing test** for `get_published_by_id` (mirrors the existing `get_by_slug` two-tier tests at `test_marketplace_service.py:155-183`):

```python
@pytest.mark.asyncio
async def test_get_published_by_id_prefers_published(mock_listings):
    table = marketplace_service._listings_table()
    lid = "11111111-1111-1111-1111-111111111111"
    table.put_item(Item={"listing_id": lid, "version": 1, "slug": "demo", "status": "published"})
    table.put_item(Item={"listing_id": lid, "version": 2, "slug": "demo", "status": "review"})
    result = await marketplace_service.get_published_by_id(listing_id=lid)
    assert result["version"] == 1 and result["status"] == "published"


@pytest.mark.asyncio
async def test_get_published_by_id_falls_back_to_highest(mock_listings):
    table = marketplace_service._listings_table()
    lid = "22222222-2222-2222-2222-222222222222"
    table.put_item(Item={"listing_id": lid, "version": 1, "slug": "demo", "status": "taken_down"})
    table.put_item(Item={"listing_id": lid, "version": 2, "slug": "demo", "status": "draft"})
    result = await marketplace_service.get_published_by_id(listing_id=lid)
    assert result["version"] == 2
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: get_published_by_id`).

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py -k get_published_by_id -v`
Expected: FAIL.

- [ ] **Step 3: Implement `get_published_by_id`** in `marketplace_service.py` (next to `get_by_id`, base-table Query — no GSI):

```python
async def get_published_by_id(*, listing_id: str) -> dict | None:
    """Resolve the buyer-facing row for a listing_id.

    Returns the currently-published version, falling back to the highest
    version of any status (so seller/admin paths still see drafts). Mirrors
    get_by_slug's two-tier logic but keyed on the UUID — the canonical
    identity now that slugs are non-unique.
    """
    table = _listings_table()
    resp = table.query(
        KeyConditionExpression="listing_id = :l",
        ExpressionAttributeValues={":l": listing_id},
        ScanIndexForward=False,  # newest version first
    )
    items = resp.get("Items", [])
    if not items:
        return None
    for item in items:
        if item.get("status") == "published":
            return item
    return items[0]
```

- [ ] **Step 4: Repoint the public detail route.** In `marketplace_listings.py`, change the path param + resolver (`:244-256`):

```python
@router.get("/listings/{listing_id}")
async def get_listing(listing_id: str, response: Response):
    ...
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
```

Update the two `slug=slug` references in that function's logging/comments (`:286-288`) to `listing_id`.

- [ ] **Step 5: Repoint the deploy route** (`:295-314`):

```python
@router.post("/listings/{listing_id}/deploy")
async def deploy_listing(
    listing_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    ...
    require_org_admin(auth)
    listing = await marketplace_service.get_published_by_id(listing_id=listing_id)
    if not listing or listing.get("status") != "published":
        raise HTTPException(status_code=404, detail="listing not found")
```

(The rest of `deploy_listing` already uses `listing["listing_id"]` for entitlement/S3 — no change below this line.)

- [ ] **Step 6: Repoint checkout.** In `schemas/marketplace.py:118-122`, rename `listing_slug` → `listing_id`:

```python
class CheckoutRequest(BaseModel):
    listing_id: str
    success_url: str
    cancel_url: str
    email: str | None = None  # required for anonymous (guest) checkout
```

In `marketplace_purchases.py:44`:

```python
    listing = await marketplace_service.get_published_by_id(listing_id=payload.listing_id)
```

(Stripe `metadata` at `:66-72` already records both `listing_id` and `listing_slug` from the row — leave those; the row still carries `slug`.)

- [ ] **Step 7: Update the resolver mocks in router/credential tests.** The patch target moves from `get_by_slug` to `get_published_by_id`. Apply this exact transform to the four test files, then fix request payloads/params (slug → listing_id) and any `*_unknown_slug_*` test names:

```bash
cd apps/backend
for f in tests/unit/routers/test_marketplace_listings.py \
         tests/unit/routers/test_marketplace_purchases.py \
         tests/credentials/test_marketplace_requires_credentials.py; do
  sed -i '' 's/marketplace_service\.get_by_slug/marketplace_service.get_published_by_id/g; s/mock_get_by_slug/mock_get_published/g' "$f"
done
```

Then by hand in those files: change deploy/detail test requests from `/listings/{slug}` to `/listings/{a-uuid}`, and change checkout request bodies from `{"listing_slug": ...}` to `{"listing_id": ...}`. Keep the `get_by_slug` two-tier tests in `test_marketplace_service.py` (that function still exists and is still used by nothing public — leave it, or delete if grep shows zero callers; see Task 8).

- [ ] **Step 8: Run the backend marketplace suite — expect PASS.**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_marketplace_service.py tests/unit/routers/test_marketplace_listings.py tests/unit/routers/test_marketplace_purchases.py tests/credentials/test_marketplace_requires_credentials.py -v`
Expected: PASS.

- [ ] **Step 9: Commit.**

```bash
git add apps/backend/core/services/marketplace_service.py apps/backend/routers/marketplace_listings.py apps/backend/routers/marketplace_purchases.py apps/backend/schemas/marketplace.py apps/backend/tests/
git commit -m "feat(marketplace): resolve detail/deploy/checkout by listing_id"
```

---

## Task 3: Frontend — API layer by listing_id

**Files:**
- Modify: `apps/frontend/src/lib/marketplace/api.ts:40-97`
- Modify: `apps/frontend/src/hooks/useCatalog.ts` (the `deploy()` method — read it first; it posts to `/marketplace/listings/{slug}/deploy`)

- [ ] **Step 1: Repoint `getListing` + `getListingBySlug` + `checkout`.** Edit `api.ts`:

```typescript
export async function getListing(listingId: string): Promise<ListingDetailResponse | null> {
  const resp = await fetch(`${API}/marketplace/listings/${encodeURIComponent(listingId)}`, {
    next: { revalidate: 60 },
  });
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`getListing failed: ${resp.status}`);
  return (await resp.json()) as ListingDetailResponse;
}

/** Authenticated fetch of a single listing by listing_id, unwrapped to the
 * row. Used by the seller /manage page to drive ListingDetailView + the
 * owner-gate redirect. */
export async function getListingById(opts: { listingId: string; jwt: string }): Promise<Listing> {
  const resp = await fetch(
    `${API}/marketplace/listings/${encodeURIComponent(opts.listingId)}`,
    { headers: { Authorization: `Bearer ${opts.jwt}` } },
  );
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: string };
    throw Object.assign(
      new Error(body.detail || `getListingById failed: ${resp.status}`),
      { status: resp.status, detail: body.detail },
    );
  }
  const envelope = (await resp.json()) as ListingDetailResponse;
  return envelope.listing;
}

export async function checkout(opts: {
  listingId: string;
  successUrl: string;
  cancelUrl: string;
  jwt: string;
  email?: string;
}) {
  const resp = await fetch(`${API}/marketplace/checkout`, {
    method: "POST",
    headers: { "content-type": "application/json", Authorization: `Bearer ${opts.jwt}` },
    body: JSON.stringify({
      listing_id: opts.listingId,
      success_url: opts.successUrl,
      cancel_url: opts.cancelUrl,
      email: opts.email,
    }),
  });
  if (!resp.ok) throw new Error(`checkout failed: ${resp.status}`);
  return (await resp.json()) as { checkout_url: string; session_id: string };
}
```

- [ ] **Step 2: Repoint `useCatalog().deploy`.** Read `apps/frontend/src/hooks/useCatalog.ts`, find the `deploy` method that fetches `/marketplace/listings/${slug}/deploy`, and change its parameter + URL to use `listingId`:

```typescript
  // was: deploy(slug: string) → `/marketplace/listings/${slug}/deploy`
  const deploy = useCallback(async (listingId: string) => {
    ...
    const resp = await /* existing authed fetch */ `/marketplace/listings/${encodeURIComponent(listingId)}/deploy`;
    ...
  }, [/* deps */]);
```

- [ ] **Step 3: Typecheck — expect FAIL at call sites** (useHire still passes `listing.slug`). That's expected; Task 4 fixes callers.

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: errors only in `useHire.ts` / route pages (fixed next).

- [ ] **Step 4: Commit.**

```bash
git add apps/frontend/src/lib/marketplace/api.ts apps/frontend/src/hooks/useCatalog.ts
git commit -m "feat(marketplace): frontend API resolves listings by listing_id"
```

---

## Task 4: Frontend — `useHire` + link sites pass listing_id

**Files:**
- Modify: `apps/frontend/src/components/marketplace/drawer/useHire.ts:51,63,69-75`
- Modify: `apps/frontend/src/components/landing/MarketplaceSection.tsx:44`, `drawer/ListingCard.tsx:51`, `storefront/Listing/ListingCard.tsx:11`, `ListingDetailView.tsx:304`, `storefront/Listing/OwnerManageLink.tsx:25` + its `slug` prop in `listing/[listingId]/page.tsx:54`

- [ ] **Step 1: `useHire.ts`** — replace the three `listing.slug` uses:

```typescript
        if (listing.price_cents === 0 || isSeller) {
          const result = await deploy(listing.listing_id);
          ...
          showInstallToast({ agentName: listing.name, agentId: result.agent_id ?? listing.listing_id });
          ...
        } else {
          ...
          const result = await checkout({
            listingId: listing.listing_id,
            successUrl: `${window.location.origin}/marketplace/deploy-success?listing_id=${encodeURIComponent(listing.listing_id)}&session_id={CHECKOUT_SESSION_ID}`,
            cancelUrl: `${window.location.origin}/chat`,
            jwt,
            email: user?.primaryEmailAddress?.emailAddress,
          });
```

Then update `apps/frontend/src/app/marketplace/deploy-success/page.tsx` to read `listing_id` from the query string instead of `listing_slug` (grep it for `listing_slug`).

- [ ] **Step 2: Link sites** — change every `/marketplace/listing/${listing.slug}` to `${listing.listing_id}`. The five files + the `OwnerManageLink` prop (rename the prop `slug` → `listingId`, value `listing.listing_id`, and its href to `/marketplace/listing/${listingId}/manage`):

```bash
cd apps/frontend
grep -rln 'marketplace/listing/${' src/components | xargs sed -i '' \
  -e 's#marketplace/listing/${listing.slug}#marketplace/listing/${listing.listing_id}#g' \
  -e 's#marketplace/listing/${live.slug}#marketplace/listing/${live.listing_id}#g'
```

Then in `storefront/Listing/OwnerManageLink.tsx` rename the `slug` prop to `listingId` and its href to `/marketplace/listing/${listingId}/manage`; in `listing/[listingId]/page.tsx:54` pass `listingId={listing.listing_id}`.

- [ ] **Step 3: Typecheck — expect PASS.**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit.**

```bash
git add apps/frontend/src/components apps/frontend/src/app/marketplace/deploy-success
git commit -m "feat(marketplace): link + hire by listing_id"
```

---

## Task 5: Frontend — rename `[slug]` routes to `[listingId]`

**Files:**
- Rename: `apps/frontend/src/app/marketplace/listing/[slug]/` → `[listingId]/` (incl. `manage/` + `manage/__tests__/`)

- [ ] **Step 1: git mv the folders** (preserves history):

```bash
cd apps/frontend/src/app/marketplace/listing
git mv '[slug]' '[listingId]'
```

- [ ] **Step 2: Update `[listingId]/page.tsx`** param type + call:

```typescript
}: {
  params: Promise<{ listingId: string }>;
}) {
  const { listingId } = await params;
  ...
  detail = await getListing(listingId);
```

- [ ] **Step 3: Update `[listingId]/manage/page.tsx`** — param `{ listingId }`, the three `window.location.replace(`/marketplace/listing/${listingId}`)`, and the fetch `row = await getListingById({ listingId, jwt })`:

```typescript
  const { listingId } = use(params);
  ...
      window.location.replace(`/marketplace/listing/${listingId}`);
  ...
      row = await getListingById({ listingId, jwt });
  ...
  }, [listingId, isLoaded, userId, getToken]);
```

- [ ] **Step 4: Update the manage page test** `manage/__tests__/page.test.tsx` — the mocked `getListingBySlug` → `getListingById`, the describe title, and the three `expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/x")` (the route param value is now a listing_id; keep `"x"` as the test fixture's listing_id).

```bash
cd apps/frontend/src/app/marketplace/listing/'[listingId]'/manage/__tests__
sed -i '' 's/getListingBySlug/getListingById/g; s#/marketplace/listing/\[slug\]/manage#/marketplace/listing/[listingId]/manage#g' page.test.tsx
```

- [ ] **Step 5: Run the frontend marketplace tests — expect PASS.**

Run: `cd apps/frontend && pnpm test -- marketplace`
Expected: PASS (ListingCard, ListingDetailView, manage page).

- [ ] **Step 6: Commit.**

```bash
git add apps/frontend/src/app/marketplace/listing
git commit -m "feat(marketplace): rename [slug] route to [listingId]"
```

---

## Task 6: PublishWizard — drop slug-collision UX (if any)

**Files:**
- Modify: `apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx`

- [ ] **Step 1: Inspect the create-listing error handling.** The wizard still computes `slugify(form.name)` and sends `slug` — keep that (cosmetic). Only remove any branch that special-cases a "slug already taken" / 409 collision response, since the backend no longer returns it:

Run: `cd apps/frontend && grep -n "slug.*taken\|already taken\|409\|SlugCollision" src/components/marketplace/drawer/PublishWizard.tsx`
Remove only collision-specific UX; leave `seller_profile_required` handling intact.

- [ ] **Step 2: Typecheck + wizard tests — expect PASS.**

Run: `cd apps/frontend && pnpm exec tsc --noEmit && pnpm test -- PublishWizard`
Expected: PASS.

- [ ] **Step 3: Commit.**

```bash
git add apps/frontend/src/components/marketplace/drawer/PublishWizard.tsx
git commit -m "feat(marketplace): wizard no longer handles slug collisions"
```

---

## Task 7: Data cleanup — sweep orphan sentinels (the immediate unblock)

**Files:**
- Create: `apps/backend/scripts/sweep_orphan_slug_sentinels.py`

- [ ] **Step 1: Write the sweep script** (dry-run by default; `--apply` to delete). Every `listing_id` begins_with `slug:` is a sentinel — after Task 1 ships, none are created, so this is a final sweep:

```python
"""One-off: delete slug-reservation sentinel rows (listing_id='slug:...').

The sentinel mechanism is removed (see 2026-06-05 plan). Existing rows are
inert but should be swept. Dry-run by default; pass --apply to delete.

Usage: uv run python -m scripts.sweep_orphan_slug_sentinels --env prod [--apply]
"""
import argparse
import boto3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--env", required=True, choices=["dev", "prod"])
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    table_name = f"isol8-{args.env}-marketplace-listings"
    table = boto3.resource("dynamodb").Table(table_name)
    scan = table.scan(ProjectionExpression="listing_id, version")
    sentinels = [
        {"listing_id": i["listing_id"], "version": i["version"]}
        for i in scan.get("Items", [])
        if str(i["listing_id"]).startswith("slug:")
    ]
    print(f"{table_name}: {len(sentinels)} sentinel row(s)")
    for key in sentinels:
        print("  ", key["listing_id"])
        if args.apply:
            table.delete_item(Key=key)
    print("APPLIED" if args.apply else "DRY-RUN (pass --apply to delete)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run against prod** (read-only):

Run: `cd apps/backend && AWS_PROFILE=isol8-admin uv run python -m scripts.sweep_orphan_slug_sentinels --env prod`
Expected: lists `slug:stitch`, `slug:pulse`, `slug:ledger` (+ `slug:tally`, `slug:vera` once their listings are also handled — note tally/vera are LIVE, their sentinels are now harmless and removing them is fine since uniqueness no longer depends on them).

- [ ] **Step 3: Apply** (requires explicit human approval — prod data mutation):

Run: `cd apps/backend && AWS_PROFILE=isol8-admin uv run python -m scripts.sweep_orphan_slug_sentinels --env prod --apply`
Expected: `APPLIED`; re-running the dry-run reports `0 sentinel row(s)`.

- [ ] **Step 4: Commit the script.**

```bash
git add apps/backend/scripts/sweep_orphan_slug_sentinels.py
git commit -m "chore(marketplace): one-off slug-sentinel sweep script"
```

---

## Task 8: Final verification + dead-code check

- [ ] **Step 1: Confirm `get_by_slug` has no remaining production callers.**

Run: `cd apps/backend && grep -rn "get_by_slug" --include='*.py' core/ routers/`
Expected: zero hits in `routers/`. If zero, delete `get_by_slug` (`marketplace_service.py:807-837`) and its two tests in `test_marketplace_service.py:155-183`; if a caller remains, leave it. Commit if removed.

- [ ] **Step 2: Full backend suite.**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 3: Full frontend lint + build + tests.**

Run: `cd apps/frontend && pnpm run lint && pnpm test && pnpm run build`
Expected: PASS. (Frontend CI is Lint & Build only — run vitest locally per the no-Claude-PR-review note.)

- [ ] **Step 4: Manual end-to-end on dev** — publish an agent named "Stitch", take it down, publish "Stitch" again. Expect no 409. Verify `/marketplace/listing/{uuid}` renders and Hire deploys.

---

## Task 9 (DEFERRED — separate follow-up PR): drop the `slug-version-index` GSI

Not in this PR. Once Task 8 confirms `get_by_slug` is gone, the `slug-version-index` GSI in `apps/infra/lib/stacks/database-stack.ts:393-397` is unused. Removing a GSI is a destructive infra change — do it alone, after confirming no `IndexName="slug-version-index"` query remains (`grep -rn slug-version-index apps/backend`), and watch the CDK deploy. Tracked separately to keep this behavior PR clean.

---

## Rollout notes

- **Deploy order:** backend (ECS) deploys independently of frontend (Vercel). Because the route path changes (`{slug}` → `{listing_id}`), there is a brief staggered-deploy window where the old side 404s the listing detail/deploy. On a founders-only marketplace this is acceptable; **merge as one PR and deploy backend first**. If zero-skew is ever needed, add a transitional `get_published_by_id`→`get_by_slug` fallback in the two public resolvers (one line) and remove it next release.
- **Old shareable URLs** (`/listing/tally`) break — they become `/listing/{uuid}`. Acceptable for the current tiny catalog; no redirects added (YAGNI).
- **PR hygiene:** single branch off `main` via worktree, single PR (per always-PR rule). Watch CI after push.

## Self-Review

- **Spec coverage:** sentinel removal (T1) ✓; names repeat (T1 test) ✓; listing_id resolution detail/deploy/checkout (T2) ✓; frontend API/links/routes (T3-T5) ✓; wizard UX (T6) ✓; orphan cleanup / immediate unblock (T7) ✓; GSI cleanup deferred + tracked (T9) ✓.
- **Type consistency:** `get_published_by_id(listing_id=...)` used identically in T2 service + all three routers + T2 tests; frontend `getListingById({ listingId, jwt })` used in api.ts (T3) and manage page (T5); `checkout({ listingId })` in api.ts (T3) + useHire (T4).
- **No placeholders:** every code step shows full code; mechanical test/link edits give exact sed transforms + the by-hand follow-ups.
