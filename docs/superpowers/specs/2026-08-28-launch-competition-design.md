# Lightbulb Launch Competition — waitlist, comp, referral, leaderboard

Status: design approved in chat 2026-08-27/28 (owner + Aden Kim). Disposable — delete when
the competition ends.

## Goal

Relaunch under the Lightbulb AI brand with a waitlist, a product drop, and a one-month
competition ranked on the entrant's own Stripe revenue. The economics sheet (owner's)
breaks even at 500 entrants: 220 comped waitlist users, ~100 referral free months, ~180
paid, $5,000 of AWS credits absorbing the comps, a $6,000 pot.

## Decisions (locked — do not re-litigate)

| Decision | Value |
|---|---|
| Waitlist | Uncapped. It is a marketing list + referral link, not a gate. |
| Comp | Waitlist positions 1–220 get a free month (100% off, once). Delivered by **email + Stripe promotion code** created by the owner in the Stripe console: `max_redemptions=220`, `expires_at = drop + 72h`. Unredeemed = forfeited; never passed to #221. |
| Activation | Onboarding complete **and** Friday's first run complete. In this codebase that is one write (`bootstrap_state == "delivered"`). The gate is **soft**: the launch email says "finish onboarding first"; the code is redeemable at checkout regardless. |
| Timeline | Wk 0 waitlist opens → wk 3 drop → drop+72h activation/redemption deadline → competition starts at drop+72h and runs 30 days. **Free month = competition month.** |
| Pot | `$1,000` owner front + `$10 × entrants`. Grows visibly. |
| Metric | Hybrid. Public leaderboard on the entrant's own Stripe account revenue, ranked by **dollar delta vs the 30 days before competition start**, percent shown alongside (percent as the rank breaks on $0 baselines). Judges pick the winner. Never promise "highest revenue wins". |
| Revenue read | Entrant pastes a Stripe **restricted API key** (`rk_live_…`, Balance: Read + Charges: Read). Connect OAuth `read_only` is Extension-only per Stripe docs and our Connect app is a Platform; `read_write` is too much trust; a Stripe App is a build + review. |
| Referral | Referrer earns a free month **when the referred person's first paid invoice lands** (`invoice.paid`, `amount_paid > 0`). Not on activation — farmable. Applied via API as the same coupon; banked if it cannot be applied yet. |
| Scope | Comp + referral rewards are **personal owners only**. Orgs may enter paid. |
| Sign-ups during the waitlist | Clerk **Restricted** mode (dashboard toggle; existing users unaffected). Flip to Public at the drop. |
| Waitlist vendor | Build in-house. Vendors ($15–49/mo) only cover the pre-drop list; referral state must live with us because the reward is a Stripe webhook on our side, and referrals keep flowing 5+ weeks post-drop. Clerk's own waitlist mode has no metadata/referral field. |

## What already exists (verified 2026-08-27)

- **Activation signal**: `PaperclipRepo.mark_bootstrap_delivered` (`core/repositories/paperclip_repo.py`) sets `bootstrap_state="delivered"`, `bootstrap_delivered_at`, `funnel_state="complete"` in one conditional update. `readiness_state.resolve_onboarding_state(owner_id)` reads it. Both fields are already on `GET /container/status` (`funnel.state`, `bootstrap.state`).
- **Email ↔ account join**: the Clerk `user.created` webhook writes `users.email` (`routers/webhooks.py`). Waitlist rows are keyed by email; no binding write is needed.
- **Checkout**: `billing_service._build_subscription_checkout` already sets `allow_promotion_codes=True` on the $50 flat checkout (documented purpose: "100%-off launch codes"). The webhook accepts `payment_status in ("paid", "no_payment_required")`. **No coupon/discount plumbing exists** — no `discounts=`, no coupon id anywhere.
- **`invoice.paid`** is subscribed in `STRIPE_WEBHOOK_HANDLERS` but `handle_invoice_paid` is a log-only no-op. `invoice.payment_succeeded` is not subscribed.
- **Owner resolution from Stripe objects**: `resolve_owner_account` (subscription metadata → `stripe-customer-index` GSI).
- **BYO-key rail**: `core/services/key_service.py` stores per-owner secrets in Secrets Manager only (`…/user-keys/{owner}/{provider}`), no DynamoDB copy.
- **Rate limiting**: `core/services/rate_bucket.RateBucket` (in-process sliding window), used by `routers/hooks_proxy._check_rate_limit`.
- **Public routes**: no public-router convention; endpoints simply omit `Depends(get_current_user)`. `get_optional_user` exists.
- **Admin pattern**: `routers/ops.py` → `core/services/ops/growth.py` → `envelope(data, source_state, errors)` → `apps/frontend/src/app/admin/growth/page.tsx` (`getGrowth` in `admin/_lib/api.ts`). Nav in `app/admin/layout.tsx`.
- **Lifespan loops**: `main.py` already runs a running-gauges loop and a one-time reconcile sweep — precedent for an in-process periodic task.
- **Landing page**: `app/page.tsx`, `HeroFunnel.tsx`, `globals.css`, `landing.css`, `Logo.tsx` are owned by the in-flight brand branch `feat/hero-lamp-three`. The launch surface must be its own route.
- **Feature flags**: none. Backend gates on settings; frontend on `NEXT_PUBLIC_*`.
- **Nothing** named waitlist / referral / promo / invite-code exists in the repo.

## Config

Backend `Settings` (`core/config.py`), injected per environment by CDK (`service-stack.ts`):

| Setting | Meaning |
|---|---|
| `LAUNCH_DROP_AT` | ISO-8601 UTC. **Unset ⇒ every `/launch*` route 404s and the sweep does not run.** Prod is inert until an owner sets it. |
| `LAUNCH_COMP_SLOTS` | 220. Display only ("you're in the first 220"); enforcement is the promo code's `max_redemptions`. |
| `LAUNCH_ACTIVATION_WINDOW_HOURS` | 72. Competition start = drop + this. |
| `LAUNCH_COMPETITION_DAYS` | 30. |
| `LAUNCH_PRIZE_FRONT_CENTS` | 100000. |
| `LAUNCH_POT_PER_ENTRANT_CENTS` | 1000. |
| `STRIPE_FREE_MONTH_COUPON_ID` | The 100%-off-once coupon the owner creates in the Stripe console (dev + prod). Used only by the referral reward. Unset ⇒ rewards are banked, never applied. |

Derived: `competition_start_at`, `competition_end_at`, `phase ∈ waitlist | activation | live | ended`.
Changing a date is a CDK env change + deploy. No flag machinery.

## Data

Two DynamoDB tables in `database-stack.ts`, `launch-waitlist` and `launch-entries`,
following the current table-prefix convention: `get_table("launch-waitlist")` /
`get_table("launch-entries")` are read through the standard
`DYNAMODB_TABLE_PREFIX` (`lightbulb-{env}-`), like every other repository — no
dedicated `LAUNCH_WAITLIST_TABLE` / `LAUNCH_ENTRIES_TABLE` env var.

### `launch-waitlist` — PK `email` (lowercased, trimmed)

| Attr | Notes |
|---|---|
| `position` | N. From the counter row (`email = "__counter__"`, `ADD position 1`). |
| `referral_code` | S, 8 chars, unambiguous alphabet. GSI `referral-code-index`. |
| `referred_by` | S, another row's `referral_code`. Set at join (`?ref=`) or by `POST /launch/referral`. Never self. |
| `source` | `waitlist` (joined pre-drop via the form) or `signup` (row created lazily by `/launch/me`). |
| `created_at` | ISO. |
| `referrals_signed_up` | N counter, `ADD` on the referrer's row when a referred row is created/attributed. |
| `referrals_paid` | N counter, `ADD` when a referred person's first payment rewards this row. |
| `free_months_banked` | N. Earned rewards not yet applied to a subscription. |
| `referral_rewarded_at` | ISO. Stamped on the **referred** person's row by the reward CAS — the once-only guard. |
| `user_id` | S. Stamped on the first authed `/launch/me` (`claim_user`), so admin can resolve activation without an email index on `users`. |
| `claimed_at` | ISO. Stamped alongside `user_id` by the same `claim_user` conditional update. |

Position assignment: read by email → if absent, `ADD` the counter → conditional put
(`attribute_not_exists(email)`) → on condition failure re-read and return the existing row.
`# ponytail:` a same-email race leaves a gap in positions; harmless.

### `launch-entries` — PK `owner_id`

| Attr | Notes |
|---|---|
| `display_name` | 2–40 chars. |
| `public` | bool — appear on the public board. |
| `entered_at`, `waitlist_position` (nullable), `currency` | |
| `baseline_cents` | Net revenue in the 30 days before `competition_start_at`. Computed once at entry (recomputed if re-entering before start). |
| `window_cents`, `delta_cents`, `refreshed_at`, `refresh_error` | Written by the sweep. |

The Stripe restricted key is stored on the BYO-key rail (`provider = "stripe-launch"`), never in DynamoDB, never logged.

## Endpoints — `routers/launch.py`, mounted at `/api/v1/launch`

All 404 when `LAUNCH_DROP_AT` is unset.

| Route | Auth | Behaviour |
|---|---|---|
| `GET /launch` | public, `Cache-Control: public, max-age=30` | `{phase, drop_at, competition_start_at, competition_end_at, pot_cents, entrants, waitlist_count, comp_slots}` |
| `POST /launch/waitlist {email, ref?}` | public, `RateBucket` per client IP (10/min) | Validate email shape; idempotent per email; attribute `ref` via the GSI (ignore unknown/self); `ADD referrals_signed_up` on the referrer. Returns `{position, referral_code, in_comp_window}`. |
| `GET /launch/me` | user | Resolve email from `users.email` (fallback: Clerk claims if present). Lazily create a `source=signup` row so every user has a referral link. Returns waitlist row (minus counters' internals), `activated` (`bootstrap_state == "delivered"`), `activated_at`, `entry`, `can_enter`, `reason`, referral stats, `free_months_banked`. |
| `POST /launch/referral {ref}` | user | Attribute a post-drop sign-up: only if `referred_by` unset, caller has no live subscription, `ref` resolves and is not the caller's own. Idempotent. |
| `POST /launch/enter {display_name, stripe_key, public}` | user | Requires `phase ∈ {activation, live}`, `subscription_status ∈ {active, trialing}` (`trial_local` cannot enter), `bootstrap_state == "delivered"`, key matches `^rk_live_` (`^rk_(live|test)_` when `ENVIRONMENT != "prod"`), and one `stripe.BalanceTransaction.list(limit=1, api_key=key)` succeeds. Stores key, computes baseline, upserts the row. Re-entering replaces key/name. 400 with a specific `reason` on every failure; the key is never echoed. |
| `GET /launch/leaderboard` | public, `max-age=60` | Public entries ordered by `delta_cents` desc, top 50: `{rank, display_name, delta_cents, pct (null when baseline == 0), currency}` + `entrants`. |
| `GET /ops/launch` | admin | Envelope: waitlist total, top-220 activated, entrants (`paid` / `comp_eligible` — position ≤ `comp_slots` AND activated by the deadline; a proxy, promo-code redemption is not observed, so the P&L treats every eligible entrant as comped), pot, referrals signed-up/paid, months granted/banked, projected P&L vs `LAUNCH_AWS_CREDITS_CENTS` (constant 500000 in `ops/launch.py`), full entries table. |
| `POST /ops/launch/refresh` | admin | Force-refresh every entry now. |

CSV is built client-side on `/admin/launch` from `waitlist_rows` in `GET /ops/launch`.

## Webhook change — `handle_invoice_paid`

On `invoice.paid` with `amount_paid > 0`:

1. Resolve owner from `customer` via the `stripe-customer-index` GSI. Org owner ⇒ return (personal only).
2. Referral payoff: owner → `users.email` → waitlist row. If `referred_by` set and `referral_rewarded_at` unset → conditional update stamping `referral_rewarded_at` (the CAS). On success: resolve referrer via the GSI, `ADD referrals_paid 1`, then **grant**:
   - referrer has `stripe_subscription_id` and the subscription carries no discount and `STRIPE_FREE_MONTH_COUPON_ID` is set → `stripe.Subscription.modify(sub_id, discounts=[{"coupon": id}], idempotency_key=f"referral_reward:{sha256(referred_email)[:16]}")`.
   - otherwise → `ADD free_months_banked 1` on the referrer's row.
3. Drain for the payer: if the payer's own row has `free_months_banked > 0` and their subscription carries no discount → conditional decrement + apply the coupon (idempotency key `banked_month:{owner_id}:{invoice_id}`).

Every step is best-effort and logged; a Stripe error after the CAS is retried on the next `invoice.paid` for that owner via the banked path (the CAS only guards the *credit*, the grant is idempotent by key). Product events: `launch_referral_rewarded`, `launch_free_month_applied`. Any failure after the once-only CAS — including a Stripe error mid-grant — banks the month rather than losing it.

## Revenue refresh (stale-while-revalidate)

`core/services/launch_revenue.py`. There is **no lifespan task and no hourly
loop** (owner decision 2026-08-28) — the spec's original hourly sweep was
replaced by stale-while-revalidate on read. `maybe_revalidate()` runs at the
top of `GET /launch/me` and `GET /launch/leaderboard`; it schedules at most
one background `refresh_all()` pass (an in-flight guard plus a 5-minute
throttle keep concurrent reads from stacking passes) and no-ops outside
`[competition_start_at, competition_end_at + 24h]`. `refresh_all()` skips
entries whose `refreshed_at` is younger than 55 minutes unless called with
`force=True`. `refresh_entry()` records `refresh_error` as `missing_key`, the
key-rejection reason (`stripe_auth` / `stripe_permission` / `stripe_error`),
or `mixed_currency`, and — for any other failure — `refresh_failed`; it never
raises, and the repo writes it makes are guarded too (`_record()` swallows and
logs a write failure rather than aborting the pass). Admin's
`POST /ops/launch/refresh` calls `refresh_all(force=True)` to force a full
pass on demand.

`sum_net_revenue(key, start, end) -> (cents, currency)`: paginate `BalanceTransaction.list(created={gte,lte}, limit=100)`, sum `amount` for `type ∈ {charge, payment}` and `type ∈ {refund, payment_refund}` (negative), in the account's balance currency (the first transaction's `currency`; mixed currencies ⇒ `refresh_error = "mixed_currency"` and skip). Window = `[competition_start_at, min(now, competition_end_at)]`. `delta = window − baseline`.

`# ponytail:` `_inflight`/`_last_started_at` are module-level state on the
single backend task — a scheduled Lambda if the fleet ever runs more than one
instance.

## Frontend

The waitlist front door and the competition page are two different routes, split
between the brand-owned root and a dedicated `/launch` page (owner decision
2026-08-28 — the spec's original single `/launch`-hosts-everything design did
not survive contact with the in-flight brand branch owning `app/page.tsx`):

- **The root landing page** (`app/page.tsx`, brand-owned) hosts the waitlist via
  a self-contained `<WaitlistForm />` (`components/launch/WaitlistForm.tsx`):
  it takes no props, fetches its own `GET /launch` state, and renders nothing
  unless the launch is configured and `phase === "waitlist"` — so it can sit
  in the hero unchanged through the drop. The brand team's integration is one
  import line: `import { WaitlistForm } from "@/components/launch/WaitlistForm"`.
- **`app/launch/page.tsx`** is the competition page (`components/launch/LaunchPage.tsx`):
  `ld-page` layout with the landing `Navbar`/`Footer`, a hero (pot + countdown +
  phase copy via `LaunchHero`) that shows the waitlist form pre-drop and a
  sign-up CTA during activation, a `Leaderboard` when `phase ∈ {live, ended}`,
  and — signed in — a `MyLaunchCard` with the `EnterForm`. Reads `GET /launch`
  on a 30s poll; signed-in users also read `GET /launch/me`.
- `?ref=` is captured by `useLaunchRef` into `localStorage`. When signed in and
  a stored ref exists → `POST /launch/referral` once, then clear. Referral
  links are rendered as `flickmeon.ai/?ref=CODE` (`ReferralLink.tsx`) — the
  root domain, not `/launch`, since that's where the waitlist form lives.
- `EnterForm`: display name, public toggle, restricted-key field with the
  four-step instructions (*Dashboard → Developers → API keys → Create
  restricted key → Balance: Read, Charges: Read*). Client-side prefix check
  mirrors the server's.
- Components are written on the landing page's `ld-*` classes and tokens, with
  launch-specific extras in `components/launch/launch.css`.
- **`app/admin/launch/page.tsx`** + nav link: KPI tiles, sheet lines, a full
  entries table, Refresh all, and client-side CSV export (`LaunchAdminClient.tsx`
  builds the CSV from `GET /ops/launch`'s `waitlist_rows` — no server export
  endpoint).

## Drop-day runbook

1. **Wk 0** — merge; set `LAUNCH_DROP_AT` (+ the other `LAUNCH_*`) for dev; verify `dev.flickmeon.ai/launch` end to end with Claude-in-Chrome; promote; set prod values; Clerk prod → **Restricted**; hero CTA → `/launch`.
2. **Drop** — Stripe live console: coupon 100% off / once → promotion code with `max_redemptions=220`, `expires_at=drop+72h`; set `STRIPE_FREE_MONTH_COUPON_ID` (dev+prod); `/admin/launch` → Export CSV → send the launch email from the marketing tool; Clerk → **Public**.
3. **+72h** — nothing. The promo code expires itself; the competition starts on the clock.
4. **End** — `/admin/launch` → Refresh all; judges pick from the table; announce by email.

## Testing

- Backend: phase math (boundaries, unset config ⇒ 404), waitlist idempotency + counter race, referral attribution rules (self, unknown, already-attributed, already-subscribed), `can_enter` predicate matrix, key validation (prefix, Stripe auth error, never logged), `invoice.paid` reward CAS (double delivery grants once), banking + drain, revenue summation (types, refunds, mixed currency, pagination), leaderboard ordering + `pct` null on zero baseline, admin envelope + CSV.
- Frontend: one render test per phase; `MyLaunchCard` states; ref stash/attribute.
- E2E: Claude-in-Chrome on dev — join waitlist with `?ref`, sign up, activate, enter with a test-mode restricted key (`rk_test_` accepted only when `ENVIRONMENT != "prod"`), see the board.

## Non-goals (v1)

In-shell banners; self-serve key revocation (admin deletes the entry); winner display; anti-fraud beyond the paid-referral trigger and promo-code caps; multi-currency conversion; org comps.
