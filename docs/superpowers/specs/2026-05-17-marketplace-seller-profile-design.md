# Marketplace Seller Profile — design

**Date:** 2026-05-17
**Status:** approved, in-flight
**Surface:** `apps/backend/{routers,core/services,schemas,repositories}`, `apps/frontend/src/components/marketplace`, `apps/infra/lib/stacks/database-stack.ts`

## Goal

Sellers pick a **marketplace handle** (Reddit-style username) before they
publish. The handle is the public identity on every listing byline today,
and the anchor for stars / upvotes / per-seller credibility tomorrow.

## Non-goals (this iteration)

- Stars / upvotes / followers — separate spec.
- Public profile pages with aggregated listings — the data model leaves
  room for it but the UI doesn't ship yet.
- Re-attribution of past listings whose seller hasn't onboarded the new
  flow — they keep the v0 Clerk-derived display name from
  `seller_display.py` as a transitional fallback.

## Data model

New DynamoDB table `marketplace-sellers` (env-prefixed: `isol8-dev-marketplace-sellers`).

| Attribute        | Type   | Notes |
|------------------|--------|-------|
| `seller_id`      | string | PK. Clerk user_id. |
| `handle`         | string | Lowercased. **Unique** via GSI. |
| `display_name`   | string | Optional pretty name shown alongside `@handle`. |
| `bio`            | string | Optional ≤280 chars; reserved for future profile page. |
| `created_at`     | string | ISO-8601 UTC. |
| `updated_at`     | string | ISO-8601 UTC. |

**GSI:** `handle-index` — partition key `handle`, no sort key. Used for
uniqueness probe on create and for future `GET /seller/{handle}` lookups.

**Throughput:** on-demand (matches every other marketplace table).

## Validation

Handle format: `^[a-z0-9_-]{3,32}$`. Lowercased server-side before write.

Reserved handles (case-insensitive, rejected with 409):
`isol8`, `admin`, `support`, `help`, `staff`, `system`, `official`,
`security`, `moderator`, `mod`, `marketplace`, `featured`, `curator`.

Uniqueness: enforced via DDB conditional `attribute_not_exists(handle)`
write against the GSI's pk on create; on update, the new handle goes
through the same probe.

## API

All under `/api/v1/marketplace/seller`. Authenticated (Clerk JWT) except
where noted.

| Method | Path             | Purpose |
|--------|------------------|---------|
| `GET`  | `/seller/me`     | My profile, or 404 if not set. |
| `PUT`  | `/seller/me`     | Create or update my profile. Body: `{handle, display_name?, bio?}`. |
| `GET`  | `/seller/handle/{handle}` | Public read by handle. 404 if missing. **No auth.** |
| `GET`  | `/seller/check-handle?h=` | Returns `{available: bool, reason?: str}`. Auth. |

### `PUT /seller/me` responses

- `200` — profile created or updated, body is the new profile.
- `400` — handle fails format validation; body `{detail: "handle_format"}`.
- `409` — reserved handle; body `{detail: "handle_reserved"}`.
- `409` — handle taken by another seller; body `{detail: "handle_taken"}`.

## Publish gate

`POST /api/v1/marketplace/listings` (create_draft) starts checking that
the calling user has a seller profile. If absent:

- Returns `409` with `{detail: "seller_profile_required"}`.
- Frontend handles this by redirecting the publish wizard to its new
  "Set your handle" step, completing the profile, then retrying the
  original publish.

Existing draft / review / published listings created before this lands
keep working unchanged — only **new** create_draft calls are gated.

## Resolver behaviour

`core/services/seller_display.resolve_display_name(seller_id)` is
updated to prefer the new sellers table over the Clerk derivation:

1. **Sellers table hit** → `@{handle}`.
2. **Curator id match** → `@isol8` (special-cased — the curator
   account gets the handle implicitly without going through `/seller/me`,
   so it always renders correctly even before backfill).
3. **Clerk derivation** (first+last name, then email local-part).
4. **Final fallback** → `"Community member"`.

The single in-process TTL cache continues to wrap this. Cache key stays
`seller_id`. A successful `PUT /seller/me` clears that seller's cache
entry so the new handle is reflected on the next read without waiting
for TTL.

## Frontend surfaces

### Listing card byline
Show `@{handle}` instead of `seller_display_name`. The existing field
still drops in as a fallback when no handle exists yet (transitional —
remove the field a release after backfill is done).

Self-attribution: when the card belongs to the current user, render
`you (@{handle})` so people see their own public identity.

### Settings page (`/settings`)
New "Marketplace profile" section under the existing layout. Inputs:
- Handle (with `@` prefix + inline availability indicator)
- Display name (optional)
- Bio (optional, ≤280)

Save button calls `PUT /seller/me`. On success, toast + reload the
section. On `409 handle_taken`/`handle_reserved`, surface inline.

### Publish wizard
First step changes from `PickAgent` to `EnsureHandle` whenever the user
has no profile. The step:
1. Calls `GET /seller/me`.
2. If 404, shows the same handle picker as settings, gating "Next".
3. If 200, advances immediately to `PickAgent`.

After the publish flow completes, the wizard does not need to handle
the gate again — the profile is now set.

## Tests

Backend:
- Validation: format regex, reserved list (case-insensitive), length
  bounds, lowercase normalisation.
- Uniqueness: conditional-write probe rejects taken handle with 409.
- Publish gate: `POST /listings` 409 without a profile; succeeds after
  `PUT /seller/me`.
- Resolver: profile hit returns `@handle`; absent profile falls through
  to Clerk derivation; curator special-case still returns `@isol8`.
- Cache invalidation: `PUT /seller/me` evicts the seller's resolver
  cache entry.

Frontend:
- Settings handle editor: availability check debounce, validation
  errors render inline, save round-trip.
- Publish wizard: handle gate appears when 404, advances when 200.
- Listing card: renders `@handle` when present, falls back to the
  legacy `seller_display_name` otherwise.

## Rollout

1. Land CDK change (new table + IAM grants on the backend role).
   Vercel + ECS deploy pick it up on next CI run.
2. Backend + frontend code lands in the same PR — depends on the table
   existing. Code paths tolerate `ResourceNotFoundException` by
   surfacing as "no profile yet" so a brief window between CDK deploy
   and backend deploy doesn't 500.
3. After ship: the curator account (admin@isol8.co) gets `@isol8`
   seeded manually via the new endpoint so the featured shelf shows
   the new byline immediately.
4. Backfill of existing sellers is **opt-in**: their next publish
   prompts them through the handle gate. We don't auto-generate
   handles for them — credibility shouldn't accrue to an identity
   they didn't pick.

## Out of scope (parking lot)

- Star / upvote system on sellers and listings.
- Profile pages at `/marketplace/seller/{handle}`.
- Verified-seller badges.
- Display-name moderation queue.
- Handle changes — for v1 you can update freely, but once stars exist
  the model may need history / squat-protection.
