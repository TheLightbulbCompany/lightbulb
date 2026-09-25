# iOS in-app purchase (StoreKit 2) — design

Status: draft for owner review, 2026-09-25.
Delete this doc when the feature ships (CLAUDE.md "Planning docs are disposable").

## Goal

A free-tier workspace admin can buy Base, Workspace or Ultra inside the iPhone app,
and the workspace becomes paid exactly as a Stripe checkout makes it paid. Built and
tested end to end **now**, shipped dark, switched on when Apple unblocks the account.

## Owner rulings (2026-09-24/25)

- Route A: StoreKit 2 through the already-bundled `expo-iap`, linked to our backend.
  No RevenueCat, no web link-out.
- Web prices stay $20 / $50 / $150. iOS prices go up so Apple's cut comes out of
  the uplift: **$23.99 / $59.99 / $179.99** (Small Business Program, 15%). Prices live
  in App Store Connect, never in code; the app renders StoreKit's `displayPrice`.
- All three plans are sold in-app. The free tier stays and is the entry point.
- Apple-billed workspaces get no in-app credit top-ups (balance only). Out of scope.

- **Both payment options in the app** (owner ruling 2026-09-25): Apple IAP everywhere, plus
  "Pay on the web" at the web price through Stripe
  Checkout in Safari (App Store 3.1.1(a) US carve-out; Stripe's "digital goods on iOS"
  guide). The web option is the cheaper one ($20/$50/$150 vs $23.99/$59.99/$179.99) and
  carries no Apple commission. IAP stays at least as prominent as the web button.
- The 3.1.3(c) Enterprise question is still worth asking Apple; if accepted, IAP can be
  switched off with `APPLE_IAP_SELLING` and the link-out stays.

## Stripe link-out

- **Backend:** `POST /billing/checkout` takes `return_to: "web" | "app"` (default `web`).
  `app` sets `success_url`/`cancel_url` to `{FRONTEND_URL}/checkout_redirect/{success,cancel}`
  (already claimed by the app's universal link, already has a web fallback page) and
  `origin_context="mobile_app"`. Everything else — admin-only, one live subscription,
  open-session reuse, the webhook that grants the plan — is the existing Stripe path. An
  open session is only reused when its `return_to` matches.
- **Mobile:** the paywall shows, under each plan's IAP button, "Or pay $X/month on the web"
  — `$X` from `/billing/account` (`base_price_usd` / `workspace_price_usd` /
  `ultra_price_usd`, server-owned, never in the binary). No storefront check: the app is
  sold on the US App Store only (ASC availability), which is what makes the link allowed.
  **Before ever widening availability beyond the US, gate this option on
  `getStorefront() === "USA"`.** It opens the Checkout URL with
  `Linking.openURL` (Safari), never a web view. The return link, or the app coming back
  to the foreground, refetches `/billing/account`; the return link is a hint, never a grant.
- **Web billing page:** when `subscription_source === "apple_iap"` it shows the plan and
  "Billed through the App Store — change or cancel on your iPhone", with no Stripe portal
  or plan-change buttons (web checkout would 409).

## Why it is blocked in production, and why that does not block the build

App Store Connect's legal entity is still the pre-migration individual, so the W-9 and the
Paid Apps Agreement are stuck (Apple Finance case 22443039). Apple's sandbox needs that
agreement. **Xcode StoreKit Testing does not**: products are defined in a local
`.storekit` file and purchases run in the simulator, signed by a local Xcode certificate.
`app-store-server-library` already has `Environment.XCODE`, which skips the Apple chain
check for exactly those transactions. So dev can test the whole money loop today.

## Products

One subscription group, `Lightbulb Workspace`, ranked Ultra > Workspace > Base, so Apple
handles upgrade (immediate), downgrade (next renewal) and proration.

| product id | `billing_accounts.plan` | iOS price |
|---|---|---|
| `company.thelightbulb.mobile.base.monthly` | `base` | $23.99 |
| `company.thelightbulb.mobile.plan.monthly` (reserved id, kept) | `standard` | $59.99 |
| `company.thelightbulb.mobile.ultra.monthly` | `ultra_pro` | $179.99 |

The plan is always derived from the **verified** `productId`, never from the client.
Family Sharing off in ASC; the server also refuses `inAppOwnershipType == FAMILY_SHARED`.

## Backend

Revive from git history rather than rewrite: `link_purchase`, `verify_transaction`,
`owner_account_token`, `AppleSubscription.owner_hint` (`24edcd0b6^:apps/backend/core/services/apple_iap.py`)
and their tests. The billing-row CAS / `signed_date` watermark / refund tombstone logic is
subtle (10 review rounds in #1720) and is revived verbatim, not refactored.

1. **Config** (`core/config.py`, CDK `service-stack.ts`, regenerated `deploy.yml`):
   - `APPLE_IAP_PRODUCT_ID` → `APPLE_IAP_PRODUCT_PLANS`, a product-id → plan map.
   - `APPLE_IAP_SELLING: bool = False` — the one switch. Off in prod until launch.
   - `APPLE_IAP_ACCEPT_XCODE: bool = False` — dev only. CDK sets it true for dev and
     **cannot** set it for prod (a synth-time assertion + a CDK test). With it on, the
     verifier accepts `environment == Xcode` transactions unverified; with it off, an
     Xcode transaction is refused like any bad signature.
2. **`POST /billing/apple/purchase-intent`** (new): `require_org_admin`; 409 `stripe_managed`
   if a Stripe subscription is live; 404 when `APPLE_IAP_SELLING` is off. Returns
   `{app_account_token, product_ids}`. The fence runs **before** StoreKit charges, and the
   server — not the client — derives the token (UUID from the org id).
3. **`POST /billing/apple/link`** (410 tombstone → live): admin-only, verifies the JWS,
   requires the echoed `appAccountToken` to match, writes the Apple subscription **and the
   plan**, returns the account status. 409 if a Stripe subscription is live (belt and braces).
4. **Entering paid, shared with Stripe**: extract the `entering_live` side effects from
   `stripe_webhook_handlers.handle_subscription_state` into one helper (referral hook,
   `promote_listening`, OpenRouter re-limit, `write_owner_facts`, `dispatch_reconcile`) and
   call it from the Apple path. Fix the latent bug in `_reconcile_active_subscription`:
   it writes `provider_choice="openrouter_managed"`, which sends an Apple-only owner (no
   Stripe customer → no Metronome contract) to `credits_required` right after paying.
   Leave the provider unset, like a Stripe checkout, so the owner lands on the capped
   managed default for its plan (`trial_cap.is_managed_capped`).
5. **Notifications** (`/webhooks/apple-iap`, unchanged route): map `DID_CHANGE_RENEWAL_PREF`
   / upgrade `DID_RENEW` so the plan follows the product; existing revoke / expiry /
   teardown handling is unchanged.
6. **Web checkout refuses an Apple-billed workspace**: `create_checkout` returns 409
   `apple_managed` when `apple_subscription_status` is live; otherwise it would mint a
   second subscription.
7. **`GET /billing/account`** gains `apple_iap_product_ids: list[str]` — empty unless
   `APPLE_IAP_SELLING` is on. This is what makes the app render the paywall.

## Mobile (`apps/mobile`, JS only — ships by OTA, native fingerprint unchanged)

- **`PaywallScreen`** (revived from `c9fa53261^`, cut down): lists the StoreKit products
  for `apple_iap_product_ids` with `displayPrice`; Buy → purchase-intent → `requestPurchase`
  with the server's `appAccountToken` → `link` → `finishTransaction` **only after a 2xx**.
  Restore → `getAvailablePurchases()` → link each. Plain, neutral copy.
- **Where it appears**, only for an org **admin** and only when the server lists products
  and StoreKit returns them:
  - (Stripe-billed workspaces see neither option; they already have a plan.)
  - Settings → **Plans** (free tier or already Apple-billed: shows current plan, allows
    upgrade). Required so a reviewer on a live trial can find the purchase.
  - `ProvisionGate`'s subscription gate (trial expired / plan required) renders the paywall
    in place of today's neutral "not available" text.
  - Members see "Ask your workspace admin". Stripe-billed workspaces never see it.
- **Launch listener** in `(app)/_layout.tsx`: links any unfinished transaction at startup
  (covers "paid, app died before link").
- **Dark by default**: with `apple_iap_product_ids` empty every screen renders today's UI,
  so merging to main (which ships an OTA) changes nothing for users.
- `purchaseCta.ts` header comment updated: the app now relies on IAP, not 3.1.3(f).

## Testing

**Automated (CI):**
- Backend: revived `test_apple_iap.py` suite (token mismatch, cross-account, refund
  replay, superseded link); new — intent 403 member / 409 Stripe-live / 404 selling-off;
  link writes `plan` per product and leaves `provider_choice` unset; entering-paid helper
  called once from each of Stripe and Apple; Xcode JWS **refused** with
  `APPLE_IAP_ACCEPT_XCODE` off and accepted with it on; web checkout 409 on Apple-live;
  FAMILY_SHARED refused; upgrade notification rewrites the plan.
- Infra: CDK test that prod can never carry `APPLE_IAP_ACCEPT_XCODE=true`.
- Mobile jest: paywall hidden with empty ids / no StoreKit products / member / Stripe-billed;
  purchase → link → finish ordering (no finish on a failed link); restore links each;
  settings Plans row visibility.

**Dev acceptance (simulator, Xcode StoreKit Testing, dev backend, receipts on the PR):**
1. A fresh free-tier dev org, admin signed in. `/billing/account` lists 3 product ids.
2. Settings → Plans shows $23.99 / $59.99 / $179.99 from the `.storekit` file.
3. Buy Base → link 200 → row: `apple_subscription_status=active`, `plan=base`,
   `subscription_status=active`, no `provider_choice`; `/container/status` ready, no
   `credits_required`; agents reply.
4. Upgrade to Ultra (Xcode Transaction Manager) → relink → `plan=ultra_pro`.
5. Kill the app between purchase and link → relaunch → the listener links it.
6. Expire/cancel in the Transaction Manager → relink or Restore → owner not entitled,
   gate returns `subscription_required`.
7. Negatives: member account sees "ask your admin" and intent 403s; the web
   `/billing/checkout` on the Apple-billed org returns 409.

**Needs Apple (after the Paid Apps Agreement is active; not in this build):** real sandbox
purchase on TestFlight, App Store Server Notifications (renewal / expiry / refund arriving
by webhook), and the ASC product setup + review screenshot.

## Launch checklist (owner, later)

Paid Apps Agreement active → Small Business Program → ASC group + three products + prices
→ Server Notifications V2 URL (prod, sandbox also prod) → sandbox acceptance →
`APPLE_IAP_SELLING=true` in prod → new `mobile-v*` build with the products attached → submit.
The public TestFlight link lets anyone make free sandbox purchases against prod; before
launch either close it or treat `apple_expires_at < now` as not entitled.

## Out of scope

Credit top-ups for Apple-billed workspaces; Android; changing web prices; any App Store
Connect work (owner-gated, blocked on Apple).
