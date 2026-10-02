# Local StoreKit testing for iOS IAP

Xcode's StoreKit Testing lets you buy the three workspace plans in a simulator
without touching App Store Connect or the real Apple sandbox. This is the dev
loop for `apps/mobile/storekit/Lightbulb.storekit` — three auto-renewable
subscriptions (`company.thelightbulb.mobile.{base,plan,ultra}.monthly`, one
subscription group, $23.99 / $59.99 / $179.99) in `apple_iap.PRODUCT_PLANS`.

## Setup

1. Prebuild the native iOS project (gitignored, regenerated on demand):

   ```bash
   cd apps/mobile && EXPO_PUBLIC_API_URL=https://api-dev.thelightbulb.company/api/v1 npx expo prebuild -p ios
   ```

2. Open the workspace: `open ios/*.xcworkspace`. In Xcode: **Product → Scheme
   → Edit Scheme → Run → Options → StoreKit Configuration**, and pick
   `storekit/Lightbulb.storekit`. If it's not in the list, drag
   `apps/mobile/storekit/Lightbulb.storekit` into the Xcode project navigator
   first, then select it in the scheme editor.

3. Run on an iOS Simulator from Xcode (not `expo start` — StoreKit
   Configuration only applies to a scheme run from Xcode itself).

4. Sign in as the dev test admin. Use the Clerk ticket recipe (CLAUDE.md
   "Signing a Chrome session in…", or the simulator variant in memory
   `mobile-nav-insets-keyboard-pass-2026-09-15`): mint a Clerk sign-in token
   for the test org's admin user via the Clerk admin API and redeem it with
   the ticket strategy inside the app instead of typing a password.

## Driving purchases

**Debug → StoreKit → Manage Transactions** opens the Transaction Manager for
the running simulator. From there you can:
- Refund or expire a subscription (simulates cancellation/lapse without
  waiting for a real renewal interval).
- Force a renewal or an upgrade/downgrade between the three products in the
  group.
- Toggle "Ask to Buy" to exercise the approval-required path: the purchase
  stays pending until approved, then arrives at `PurchaseListener`. An
  approved Ask to Buy purchase is the buyer's own (`PURCHASED`), so the
  backend's refusal of `FAMILY_SHARED` transactions (`apple_iap.py`) does not
  apply to it.

## Why the dev backend accepts these

Xcode StoreKit-config transactions are signed with Apple's **local testing**
certificate, not the production/sandbox one. `APPLE_IAP_ACCEPT_XCODE=true` in
dev only (CDK: `service-stack.ts`, `env === "prod" ? "false" : "true"`) tells
the verifier to accept that certificate; prod carries
`APPLE_IAP_ACCEPT_XCODE=false` and refuses it outright, so this whole flow is
dev/simulator-only by construction.

**Xcode mode SKIPS verification entirely.** The library does no chain check
for `Environment.XCODE` — it decodes the local blob as-is — so on dev any org
admin can forge a transaction and grant themselves any plan. That is accepted
for dev only; never enable the flag anywhere real money or real rows matter.

## Note: no webhook traffic in this mode

Xcode-local purchases never reach `POST /webhooks/apple-iap` — App Store
Server Notifications V2 require Apple's real (sandbox or production)
environment, which needs the Paid Apps Agreement to be active. In Xcode
testing, the only way the backend learns about a renewal, expiry, or upgrade
is a **relink**: the app calling `POST /billing/apple/link` again (via the
paywall, `PurchaseListener`, or Settings → Plans → Restore). Keep that in mind
when reading a "nothing changed" result — it may just mean nothing has
relinked yet, not that the transaction didn't change in Xcode.

## Dev acceptance script (run after merge + dev deploy)

Run against a fresh free-tier dev org (see memory
`money-freetier-ux-audit-2026-09-24` for the Clerk admin API recipe to create
one) whose admin you're signed in as. Before starting, confirm dev has rolled
past this change:

```bash
aws ecs describe-services --profile isol8-admin --cluster lightbulb-dev-container \
  --services lightbulb-dev-backend --query 'services[0].taskDefinition'
```

then check that task definition's env has `APPLE_IAP_SELLING=true`. Capture a
screenshot or response for each step and post them on the PR:

1. `GET /billing/account` → `apple_iap_product_ids` has 3 ids.
2. Settings → Plans shows $23.99 / $59.99 / $179.99.
3. Buy Base (Xcode StoreKit sheet) → the billing-accounts row
   (`aws dynamodb get-item --table-name lightbulb-dev-billing-accounts …`)
   shows `apple_subscription_status=active`, `plan=base`,
   `subscription_status=active`, `subscription_source=apple_iap`, and no
   `provider_choice`; a reconcile SFN execution started; `/container/status`
   is ready (no `credits_required`); an agent replies in `#general`.
4. Transaction Manager → upgrade to Ultra → the paywall/`PurchaseListener`
   links it → the row updates to `plan=ultra_pro`. Then downgrade back to Base
   in Transaction Manager → confirm the plan stays `ultra_pro` until the
   renewal (a downgrade takes effect at the next period), and record what
   StoreKit delivers and when.
5. Buy with the backend unreachable: toggle the simulator's network off right
   after the Apple purchase sheet clears, then relaunch with network back on
   → `PurchaseListener` links the still-unfinished transaction on launch (row
   updates, and the transaction shows finished in Transaction Manager).
6. Transaction Manager → expire the subscription, then relaunch the app.
   Nothing is relinked (`getAvailablePurchases` does not return an expired
   subscription, and Xcode mode sends no notifications), so the row keeps
   `apple_subscription_status=active` — but its `apple_expires_at` is now in
   the past, so the next status read derives `subscription_status=canceled`
   (`billing_repo.apple_lapsed`) and the gate returns `subscription_required`,
   showing the paywall again.
7. As a member (not admin) of the same org: no Plans row in Settings; the
   gate shows "Ask your workspace admin…"; a `curl` to `purchase-intent` with
   the member's token → 403.
8. `POST /billing/checkout` with the admin's token on this (Apple-billed) org
   → 409 `apple_managed`; the web billing page for that org shows "Billed
   through the App Store" with no Stripe buttons.
9. On a **second** fresh free-tier org: the paywall shows "Or pay $20/month
   on the web" under Base → tap it → Safari opens Stripe Checkout (dev =
   Stripe test mode; card `4242 4242 4242 4242`) → pay → the universal link
   (or the `/checkout_redirect/success` fallback page's "Open the app" link)
   returns to the app → the account refetches → the row shows
   `stripe_subscription_status=active`, `plan=base`,
   `subscription_source=stripe`; the paywall no longer offers either option.
