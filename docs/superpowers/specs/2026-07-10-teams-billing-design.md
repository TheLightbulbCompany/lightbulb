# Teams Billing (per-seat) — design

**Date:** 2026-07-10
**Status:** Approved design, ready for implementation plan
**Owner:** Teams Billing stream (see memory `project_teams_entry_point_shipped`)
**Depends on:** #1201 (free-tier Phase A) merging first — it adds the `team_billing_required`
gate code this feature satisfies. Build rebases onto `main` after #1201 lands.

## Problem

After #1200, the landing "Create a team" CTA routes a new org admin through Clerk org
creation → `/chat`. Once #1201 ships, `provision_gate` returns a distinct
`team_billing_required` code for an org owner with no billing row. Today nothing satisfies that
gate — a team admin dead-ends. This feature is the **per-seat Teams subscription** that
unblocks them: an org owner subscribes at $20/seat/month (quantity = member count, no trial),
seats sync automatically as members join/leave, and the paywall renders where they're blocked.

## Decisions (locked)

- **Per-seat**, quantity = **every org member** (admin included), **no trial**, **$20/seat/mo**.
- **Credit/usage model unchanged** — access-only, one shared pool at the org owner (per
  `project_teams_entry_point_shipped`). Per-seat is purely the subscription *quantity*.
- **Grandfather** existing orgs — new orgs use per-seat; existing org subscriptions are left
  untouched (no migration script; ~no real paying orgs yet).
- **Paywall** — a real per-seat checkout card on the `/chat` blocked screen AND in the settings
  billing panel (not just a link).
- **Owner model unchanged** — org subscription keys on `owner_id = org_id`
  (`resolve_owner_id`, `is_org_owner_id`).

## The one shared contract (from #1201)

`provision_gate` returns, for `owner_id.startswith("org_")` with no billing row, a `Gate`
(`provision_gate.py`) whose `to_payload()` yields the standard blocked envelope:
```
{ "detail", "blocked": { "code": "team_billing_required", "title": "Set up Teams billing",
  "message": "...", "action": {"kind":"link","label":"Set up billing","href":"/settings/billing",
  "admin_only": true}, "owner_role": "admin"|"member" } }
```
surfaced via `GET /container/status` as **402** (like every non-signup gate). This feature
**consumes** that code and never modifies `provision_gate`'s gate logic.

## Current state (grounded in the codebase map)

- **Checkout worker** `create_flat_fee_checkout` (`billing_service.py:164`) already accepts
  `trial_days` and builds a subscription Checkout Session — but hardcodes
  `line_items=[{price: STRIPE_FLAT_PRICE_ID, quantity: 1}]` (`:224`) and passes `trial_days=14`.
- **Customer** `create_customer_for_owner` (`billing_service.py:48`) is email-keyed and already
  serves org (`owner_type="org"`) — **reuse as-is**.
- **Billing row** (`billing_repo.py`) has `stripe_subscription_id` / `subscription_status` /
  `trial_end` but **no seat fields**; `set_subscription` (`:193`) is the read-then-put pattern
  to mirror.
- **Subscription webhook** `handle_subscription_state`
  (`stripe_webhook_handlers.py:196`) resolves the account (`metadata.owner_id` →
  customer GSI) and calls `set_subscription`; it does **not** capture the subscription item id
  or quantity today.
- **Org membership webhooks** `_handle_organization_membership_created/deleted`
  (`webhooks.py:285,349`) are handled but **do not touch billing/seats** — pure Paperclip +
  tenancy logging. `user.updated` (`webhooks.py:624`) already does `stripe.Customer.modify` — the
  pattern to mirror for a `Subscription.modify` quantity update.
- **Member count** — no helper; `clerk_admin.get_organization(org_id)` (`:145`) returns Clerk's
  org object which includes `members_count`.
- **Frontend blocked card** — `ProvisioningStepper.tsx:804` renders the *generic* blocked card
  from any gate `code` (title/message/CTA, admin-gated via
  `showAction = !(action.admin_only && owner_role !== "admin")`). `ProviderPicker.handlePick`
  (`:1423`) is the exact checkout pattern (`api.post("/billing/trial-checkout") → window.location`).
- **`/settings/billing`** route does **not** exist — the settings page keys off `?panel=billing`
  (`settings/page.tsx`, `BillingPanel` at `:106`); org members without `org:admin` already see a
  "Billing restricted" card (`:140`). Latent mismatch with the gate's `action_href`.
- **Config** — `STRIPE_FLAT_PRICE_ID` (`config.py:218`, empty default, CDK-wired) is the pattern.

## Design

### DRY spine (three single-sources-of-truth)

To avoid the obvious duplication this feature invites:

1. **One checkout-session builder.** Extract the common Stripe Checkout construction from
   `create_flat_fee_checkout` into a private `_build_subscription_checkout(*, owner_id, price_id,
   quantity, trial_days, metadata)`; both the flat (personal) and team paths call it. No copy of
   the session kwargs, idempotency-key shape, `automatic_tax`/`customer_update` block.
2. **One seat-quantity reconciler.** A single `sync_team_seat_quantity(owner_id) -> int` computes
   the live member count and sets the Stripe subscription item to that **absolute** quantity
   (idempotent). It is the *only* place quantity is written, called from **all three** triggers:
   membership webhooks, the reconcile safety-net, and post-checkout. No delta math, no duplicated
   "modify quantity" logic.
3. **One seat price.** A single source for "$20/seat" — backend `STRIPE_TEAM_SEAT_PRICE_ID` is the
   billing source of truth; the frontend reads the display price from the billing/account API
   response (backend echoes it), so the number is defined once server-side, not hardcoded in two
   UIs.

### Backend

**B1. Config** — add `STRIPE_TEAM_SEAT_PRICE_ID: str = ""` in `config.py` beside
`STRIPE_FLAT_PRICE_ID` (CDK-wired, empty default so the app boots without it).

**B2. Billing row + repo** — add `seat_count: int` and `subscription_item_id: str` to the
`billing-accounts` shape. Add `billing_repo.set_team_subscription(owner_id, *, subscription_id,
subscription_item_id, status, seat_count)` following `set_subscription`'s read-then-put +
`attribute_exists(owner_id)` guard (never upsert a phantom row).

**B3. Checkout worker** — `create_team_seat_checkout(*, owner_id, member_count)` in
`billing_service.py`, which calls the shared `_build_subscription_checkout` with
`price_id=STRIPE_TEAM_SEAT_PRICE_ID`, `quantity=member_count`, `trial_days=None`,
`metadata={owner_id, purpose:"team_seat"}`. Guards on the price id + an existing Stripe customer
(same as flat).

**B4. Endpoint** — `POST /billing/team-checkout` in `billing.py`:
`require_org_admin(auth)` → `owner_id = resolve_owner_id(auth)` (must be `org_`) →
`create_customer_for_owner(owner_id, "org", email=auth.email)` if no account →
duplicate-sub guard (`is_trial_blocked`) → `member_count = clerk_admin.get_organization_member_count(owner_id)`
→ `create_team_seat_checkout(...)` → `{"checkout_url": session.url}`. Returns 409 for
already-subscribed, 403 for non-admin (via `require_org_admin`).

**B5. Member-count helper** — `clerk_admin.get_organization_member_count(org_id) -> int`
(reads `members_count` off `get_organization`, falls back to `GET /organizations/{id}/memberships`
`total_count`). Stub-safe when `CLERK_SECRET_KEY` unset, like the other helpers.

**B6. Subscription webhook** — extend `handle_subscription_state` so that when the subscription's
price is the team seat price (or `metadata.purpose == "team_seat"`), it records
`subscription_item_id` (`event.items.data[0].id`) and `seat_count` (`quantity`) via
`set_team_subscription`. Personal flat path unchanged.

**B7. Seat sync** — in `_handle_organization_membership_created` and `_deleted`
(`webhooks.py`), after the existing Paperclip/tenancy work, call the shared
`sync_team_seat_quantity(org_id)` — but **only if** the org has an active team subscription
(`is_subscription_active(account) and account.subscription_item_id`). It sets the Stripe item to
the absolute live member count (idempotent; a missed/duplicate event self-heals). Stripe/HTTP
errors are logged and swallowed so the webhook still 200s — the reconcile net (B8) covers drift.

**B8. Reconcile safety-net** — call `sync_team_seat_quantity(owner_id)` opportunistically on the
team-checkout path and on `GET /container/status` for an active-team owner (cheap: compare live
count to `seat_count`, modify only on mismatch). This is the same function as B7 — no second
implementation — and closes the "webhook missed" gap without a cron.

### Frontend

**F1. `/settings/billing` route** — add the route so the gate's `action_href="/settings/billing"`
resolves to the billing panel (thin route that renders the existing `BillingPanel`, or redirects
to `settings?panel=billing`). Fixes the latent mismatch for both the gate CTA and the portal
`return_url`.

**F2. Shared team-checkout hook** — `useTeamCheckout()` (or extend `useBilling`) exposing
`startTeamCheckout()` that `POST`s `/billing/team-checkout` and redirects to `checkout_url` —
one implementation, used by both surfaces below. Mirrors `ProviderPicker.handlePick`.

**F3. `TeamBillingCard`** — one component rendering "N seats × $20 = $Y/mo → Subscribe your team",
used in **two** places:
- `ProvisioningStepper.tsx`: an early-return before the generic blocked card when
  `blocked.code === "team_billing_required"`, admin-gated (`showAction` rule); non-admins see the
  existing "ask your org admin" treatment.
- The settings `BillingPanel` (admin view): seat summary + Subscribe/Manage.
Seat count + price come from the account/gate payload — the card doesn't recompute or hardcode.

**F4. Billing account payload** — extend `/billing/account` (`BillingAccountResponse` /
`useBilling`'s `BillingAccount`) to expose `seat_count` and the per-seat display price (echoed
from backend config) for org owners, so F3 has one data source.

## Data flow

```
Admin (org, no billing) → GET /container/status → 402 team_billing_required
  → /chat blocked → TeamBillingCard (N × $20) → startTeamCheckout()
    → POST /billing/team-checkout → create_team_seat_checkout (qty=members, no trial)
      → Stripe Checkout → pay
        → webhook customer.subscription.created → set_team_subscription(active, item_id, seats)
          → gate passes → container provisions

Member joins/leaves org → Clerk webhook → sync_team_seat_quantity(org_id)
  → Subscription item quantity = live member count (prorated)
```

## Error handling

- **Checkout**: missing `STRIPE_TEAM_SEAT_PRICE_ID` → config guard error; non-admin → 403; already
  subscribed → 409; no Stripe customer → created first.
- **Seat sync**: only runs for active-team orgs; absolute-count set is idempotent; Stripe errors
  logged + swallowed in the webhook (200 preserved), drift healed by the reconcile net.
- **Grandfathered orgs**: an org with an existing non-seat subscription has no
  `subscription_item_id` → `sync_team_seat_quantity` no-ops for it (guard), so we never touch a
  grandfathered sub.

## Testing

- **Backend**: `create_team_seat_checkout` uses seat price + `quantity=member_count` +
  `trial_days=None`; `/billing/team-checkout` admin-gated + 409 on active sub; `sync_team_seat_quantity`
  sets the absolute live count and no-ops without an active team sub / item id; member-count helper;
  `set_team_subscription` repo setter; webhook records item id + seats for team price. Assert the
  shared `_build_subscription_checkout` is used by both flat and team (no duplicated kwargs).
  Backend: `cd apps/backend && uv run pytest tests/ -k "billing or seat or gate" -v`.
- **Frontend**: `TeamBillingCard` renders `N × $20 = total`, admin-gating hides the CTA for
  members; `blocked.code === "team_billing_required"` early-returns the card (not the generic one);
  `startTeamCheckout` POSTs the right endpoint; `/settings/billing` resolves to the billing panel.
  Single-file vitest only; `tsc --noEmit` after adding tests.

## Shared-file boundary (with the free-tier agent)

Both streams touch `billing_service.py`, `provision_gate.py`, `webhooks.py`, `config.py`. Keep it
**additive + function-scoped** to avoid conflicts:
- `provision_gate.py` — **theirs**; I only consume `team_billing_required`, add nothing.
- `billing_service.py` — I add `create_team_seat_checkout` + the extracted
  `_build_subscription_checkout`; the extraction touches `create_flat_fee_checkout`'s body (pure
  refactor, same behavior) — coordinate that one function with them.
- `webhooks.py` — I add seat-sync calls inside the two membership handlers; disjoint from their
  provisioning logic.
- `config.py` — I add one setting.

## Plan phasing (each independently testable)

1. **Backend billing core** — B1, B2, B3 (+ shared `_build_subscription_checkout`), B4, B5, B6.
2. **Seat sync** — B7 + B8 (the one `sync_team_seat_quantity`, wired into webhooks + reconcile).
3. **Frontend paywall** — F1, F2, F3, F4.

## Risks / notes

- `/settings/billing` mismatch predates this work; F1 fixes it for real (also helps the personal
  portal return_url).
- `members_count` staleness at the moment of checkout is fine — the post-checkout + webhook
  reconcile converges quantity to the live count.
- Per-seat **billing** for orgs assumes the org owner pays; personal subscribers invited to an org
  are already blocked at invite time (`orgs.py` `_has_active_personal_tenancy`), so seats are
  net-new members. No double-charge path.
