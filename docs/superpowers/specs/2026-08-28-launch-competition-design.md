# Lightbulb Launch Competition — waitlist, comp, referral, leaderboard

Status: design approved in chat 2026-08-27/28 (owner + Aden Kim). Disposable — delete when
the competition ends.

## Goal

Relaunch under the Lightbulb brand with a waitlist, a product drop, and a one-month
competition ranked on the entrant's own Stripe revenue. The economics sheet (owner's)
breaks even at 500 entrants: 220 comped waitlist users, ~100 referral free months, ~180
paid, $5,000 of AWS credits absorbing the comps, a $6,000 pot.

## Decisions (locked — do not re-litigate)

| Decision | Value |
|---|---|
| Waitlist | Uncapped. Sign-up IS the waitlist: a Clerk account created before the drop + the idea stashed as the pending brief (hero funnel) + a `launch-waitlist` row written by the Clerk `user.created` webhook (position = sign-up order, referral code). No email form. |
| Comp | Waitlist positions 1–220 get a free month (100% off, once). Delivered by **email + Stripe promotion code** created by the owner in the Stripe console: `max_redemptions=220`, `expires_at = competition_start_at`. Unredeemed = forfeited; never passed to #221. |
| Activation | Onboarding complete **and** Friday's first run complete. In this codebase that is one write (`bootstrap_state == "delivered"`). The gate is **soft**: the launch email says "finish onboarding first"; the code is redeemable at checkout regardless. |
| Entry | Two explicit steps after activation (Friday's first run delivered) AND a live subscription (paid or the promo-code free month; `trial_local` cannot): **Join** (`POST /launch/join` → entry = competing) and optional **Connect Stripe** (`POST /launch/connect-stripe` → on the board). `public` is a privacy control, retractable via `PATCH /launch/entry`; private entrants count toward `on_board` but never appear in rows. |
| Timeline | Drop Thursday midnight ET (Friday 00:00 America/New_York); competition starts Sunday midnight ET (Monday 00:00) and runs 30 days — the 3-day free trial covers activation. Both instants live in `launch-schedule.json` (per-env `dev`/`prod`, `null` = off). Free month = competition month. |
| Pot | Two figures. Public pot = $1,000 + $10 × sign-ups (climbs at sign-up, never steps down). Funded pot (admin only) = $1,000 + $10 × people whose FIRST invoice landed, any amount (a free-month redemption counts). |
| Metric | Hybrid. Public leaderboard on the entrant's own Stripe account revenue, ranked by **dollar delta vs the 30 days before competition start**, percent shown alongside (percent as the rank breaks on $0 baselines). Judges pick the winner. Never promise "highest revenue wins". |
| Revenue read | Entrant pastes a Stripe **restricted API key** (`rk_live_…`, Balance: Read + Charges: Read). Connect OAuth `read_only` is Extension-only per Stripe docs and our Connect app is a Platform; `read_write` is too much trust; a Stripe App is a build + review. |
| Referral | Referrer earns a free month **when the referred person's first paid invoice lands** (`invoice.paid`, `amount_paid > 0`). Not on activation — farmable. Applied via API as the same coupon; banked if it cannot be applied yet. |
| Scope | Comp + referral rewards are **personal owners only**. Orgs may enter paid. |
| Sign-ups during the waitlist | OPEN throughout — no Clerk Restricted mode. Held accounts get no container and no trial clock until the drop (`launch_hold.pre_drop_gate`, code `launch_pending`, runs before `ensure_personal_trial_default` at every door). |
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

One DynamoDB row per environment — `lightbulb-{env}-launch-settings`, item `id="settings"` — is
the only place the dates, pot constants, comp slots and the Stripe coupon live. Edited on
`/admin/launch` (the **Launch settings** card; platform admins only; every write is an audit row
`launch.settings.update` / `launch.settings.stripe_coupon`). There is no schedule file and no
`LAUNCH_*` / coupon env var any more.

| Field | Meaning |
|---|---|
| `enabled` | Off ⇒ every `/launch*` route 404s, the webhook join returns first thing, the pre-drop hold never fires. Both environments ship disabled. Requires both dates to turn on. |
| `drop_at` / `competition_start_at` | ISO-8601 instants **with offset** (the card shows and edits them in Eastern time). `competition_start_at > drop_at`. |
| `comp_slots` | 220. Display + the promo code's `max_redemptions`. |
| `competition_days` | 30. |
| `pot_front_cents` / `pot_per_entrant_cents` | $1,000 / $10 — the public pot is `front + per_entrant × sign-ups`. |
| `stripe_coupon_id`, `stripe_promotion_code_id`, `stripe_promotion_code` | Written by **Create Stripe coupon + promo code** on the card: `Coupon(percent_off=100, duration=once)` + `PromotionCode(max_redemptions=comp_slots, expires_at=competition_start_at)`. The code is what goes in the launch email. `stripe_coupon_id` unset ⇒ referral rewards are banked, never applied. |
| server-side only | `mint_nonce` (idempotency: a retry re-sends byte-identical params; "force" mints a fresh pair), `updated_at`, `updated_by`. |

The backend reads the row through a 30-second in-process cache (`core/services/launch_phase.py`
— it sits on the container-status poll path), so a save takes effect within ~30 s fleet-wide
with no deploy; writes also clear the public `/launch*` response cache and the admin sheet
cache. Validation on save mirrors the backend parser: `24:00`, Feb 29 and naive timestamps are
rejected, money is non-negative, `comp_slots ≥ 1`, `competition_days ≥ 1`. A DynamoDB read
failure never raises on the poll path — the last cached row is served, else the defaults
(feature off). Derived: `competition_end_at = competition_start_at + competition_days`,
`phase ∈ waitlist | activation | live | ended`.

Admin routes (`routers/ops.py`, mounted at `/api/v1/ops`): `GET /launch/settings`,
`PUT /launch/settings` (422 → `{"detail": {"errors": {field: msg}}}`),
`POST /launch/settings/stripe-coupon {force}` (409 `already_configured`, 502 `stripe_error`).

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
| `source` | Always `signup` now — written by the Clerk `user.created` webhook or lazily by `/launch/me`. (The `waitlist` value is a dead literal; nothing sets it since the waitlist form was removed.) |
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
| `baseline_cents`, `baseline_at` | Net revenue in the 30 days before `competition_start_at`, and the instant it was read. An entry made during the activation window sums a window whose tail is still in the future, so the first refresh pass at/after `competition_start_at` recomputes and re-stamps it. |
| `baseline_prorated_cents` | `baseline_cents` scaled to the elapsed fraction of the competition. Written by the sweep; it is what `delta_cents` and `pct` are measured against, so the live board ranks by growth rather than by baseline size. |
| `window_cents`, `delta_cents`, `refreshed_at`, `refresh_error` | Written by the sweep. |

The Stripe restricted key is stored on the BYO-key rail (`provider = "stripe-launch"`), never in DynamoDB, never logged.

## Endpoints — `routers/launch.py`, mounted at `/api/v1/launch`

All 404 (`require_launch`) while the settings row is disabled or missing a date for this env.
There is no `POST /launch/waitlist` — the waitlist row is written by the Clerk `user.created`
webhook (`routers/webhooks.py`), not a form submit.

| Route | Auth | Behaviour |
|---|---|---|
| `GET /launch` | public, `Cache-Control: public, max-age=30`, 20s server cache | `{phase, drop_at, competition_start_at, competition_end_at, signups, competing, on_board, pot_cents, comp_slots, pot_per_entrant_cents}` |
| `GET /launch/me` | user | Resolves email, joins/claims the caller's waitlist row (lazy, so every user has a referral link even if they signed up before the webhook existed). Returns the waitlist row, `activated` (`bootstrap_state == "delivered"`), `activated_at`, `entry`, `joined`, `has_key`, `can_join`, `reason`, `idea` (first 140 chars of the pending hero-funnel brief). |
| `POST /launch/referral {ref}` | user | Attribute a post-drop sign-up: only if `referred_by` unset, caller has no live subscription, `ref` resolves and is not the caller's own. Idempotent. |
| `POST /launch/join {display_name, public}` | user, `RateBucket` per client IP (10/min) | Step 1. Requires `phase ∉ {waitlist, ended}`, `subscription_status ∈ {active, trialing}` (`trial_local` cannot join), `bootstrap_state == "delivered"`. Creates the entry row (idempotent — re-joining returns the existing row untouched). Never touches Stripe. 400 with a `reason` on refusal. |
| `POST /launch/connect-stripe {stripe_key}` | user, `RateBucket` per client IP (10/min) | Step 2 (optional). Requires an existing entry. Validates the key (`^rk_live_`, or `^rk_(live\|test)_` when `ENVIRONMENT != "prod"`) with one `stripe.BalanceTransaction.list(limit=1, api_key=key)`, computes the `[start-30d, start)` baseline, stores the key on the BYO-key rail, flips the entry onto the board (`has_key=true`). Replaces any previously-connected key. The key is never echoed or logged. |
| `PATCH /launch/entry {public}` | user, `RateBucket` per client IP (10/min) | Flip the board-visibility flag on an existing entry — retractable at any time, independent of join/connect-stripe. |
| `GET /launch/leaderboard` | public, `max-age=60`, 30s server cache | `{competing, on_board, rows}` — `rows` = public entries with a connected key, ordered by `delta_cents` desc, top 50: `{rank, display_name, delta_cents, pct (null when baseline == 0), currency}`. |
| `GET /ops/launch` | admin | Envelope: schedule, waitlist total/top-220-claimed/activated, `signups`, `competing`, `on_board`, `competing_breakdown` (`paid` / `comp_eligible` — position ≤ `comp_slots` AND activated by the deadline; a proxy, promo-code redemption is not observed, so the P&L treats every eligible entrant as comped — / `public`), `public_pot_cents` (by sign-ups), `funded_pot_cents` + `pot_counted` (by first-invoice count), referrals signed-up/paid/banked, projected P&L vs `AWS_CREDITS_CENTS` (constant 500000 in `core/services/ops/launch.py`), full entries table, `waitlist_rows`. |
| `POST /ops/launch/refresh` | admin | Force-refresh every entry now (`refresh_all(force=True)`). |

CSV is built client-side on `/admin/launch` from `waitlist_rows` in `GET /ops/launch`.

## Webhook change — `handle_invoice_paid`

On `invoice.paid` with `amount_paid > 0`:

1. Resolve owner from `customer` via the `stripe-customer-index` GSI. Org owner ⇒ return (personal only).
2. Referral payoff: owner → `users.email` → waitlist row. If `referred_by` set and `referral_rewarded_at` unset → conditional update stamping `referral_rewarded_at` (the CAS). On success: resolve referrer via the GSI, `ADD referrals_paid 1`, then **grant**:
   - referrer has `stripe_subscription_id` and the subscription carries no discount and the settings row's `stripe_coupon_id` is set → `stripe.Subscription.modify(sub_id, discounts=[{"coupon": id}], idempotency_key=f"referral_reward:{referrer_subscription_id}:{invoice_id}")`.
   - otherwise → `ADD free_months_banked 1` on the referrer's row.
3. Drain for the payer: if the payer's own row has `free_months_banked > 0` and their subscription carries no discount → conditional decrement + apply the coupon (idempotency key `banked_month:{payer_subscription_id}:{invoice_id}`). A refused decrement stops the drain — there is no month to spend, so nothing is put back.

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

`sum_net_revenue(key, start, end) -> (cents, currency)`: paginate `BalanceTransaction.list(created={gte,lte}, limit=100)`, sum `amount` for `type ∈ {charge, payment}` and `type ∈ {refund, payment_refund}` (negative), in the account's balance currency (the first transaction's `currency`; mixed currencies ⇒ `refresh_error = "mixed_currency"` and skip). Window = `[competition_start_at, min(now, competition_end_at)]`. `delta = window − round(baseline × elapsed / total)` — the baseline is prorated to the elapsed fraction of the competition, so a full 30-day baseline is never compared against a three-day window.

`# ponytail:` `_inflight`/`_last_started_at` are module-level state on the
single backend task — a scheduled Lambda if the fleet ever runs more than one
instance.

## Frontend

There is no `WaitlistForm` and no separate waitlist front door (owner decision
2026-08-29, superseding the spec's original split) — sign-up itself is the
waitlist, so the root landing page's hero stays the plain idea funnel
(`HeroFunnel.tsx` → `GetStartedLauncher.tsx`) unchanged by the launch. Two
in-app/in-product surfaces carry the launch instead:

- **`LaunchHeldCard`** (`components/chat/LaunchHeldCard.tsx`) — rendered by
  `ProvisioningStepper` when the provision gate returns
  `blocked.code === "launch_pending"` (`core/services/launch_hold.py`). Reads
  `GET /launch/me` (authed, via `useApi()`) and `GET /launch` (public fetch)
  and renders `LaunchStatusLines` plus the gate's held-until-drop message.
- **`app/launch/page.tsx`** (`components/launch/LaunchPage.tsx`) is the
  competition page: `ld-page` layout with the landing `Navbar`/`Footer`, a
  hero (pot + countdown + phase copy via `LaunchHero`), and states built
  around `phase`:
  - `waitlist` / `activation` — a "Sign up" CTA (`activation` also shows
    "finish onboarding first, then redeem your code at checkout").
  - `live` / `ended` — a `Leaderboard` section.
  - Signed in, any phase — a `MyLaunchCard`, plus a static "How it works"
    rules list.
  - `MyLaunchCard` itself branches on `me`: `waitlist` phase → `LaunchStatusLines`;
    not `activated` → "finish onboarding" copy; `activated` but not `joined` →
    `JoinForm` when `can_join`, else the `reason`-keyed blocked copy; `joined`
    → the public-board toggle (`PATCH /launch/entry`) plus `ConnectStripeForm`
    when `!has_key` (or on `refresh_error`), else the live delta.
- `?ref=` is captured by `useLaunchRef` into `localStorage`. When signed in and
  a stored ref exists → `POST /launch/referral` once. The stored code is
  cleared only on a resolved response (`attributed` true OR false) — never on
  a network/server error, which leaves it in place so the once-guard's next
  mount can retry. Referral links are rendered as `flickmeon.ai/?ref=CODE`
  (`ReferralLink.tsx`) — the root domain, since that's where sign-up (the
  waitlist) happens.
- `JoinForm`: display name + public toggle → `POST /launch/join`.
  `ConnectStripeForm`: restricted-key field with the four-step instructions
  (*Dashboard → Developers → API keys → Create restricted key → Balance:
  Read, Charges: Read*) → `POST /launch/connect-stripe`. Client-side prefix
  check mirrors the server's.
- Components are written on the landing page's `ld-*` classes and tokens, with
  launch-specific extras in `components/launch/launch.css`.
- **`app/admin/launch/page.tsx`** + nav link: KPI tiles (both pots, `signups`/
  `competing`/`on_board`), sheet lines, a full entries table, Refresh all, and
  client-side CSV export (`LaunchAdminClient.tsx` builds the CSV from
  `GET /ops/launch`'s `waitlist_rows` — no server export endpoint).

## Drop-day runbook

1. **Wk 0** — open `admin.dev.flickmeon.ai/admin/launch`: set Drop + Competition start (Eastern), Save, tick Enabled. Verify on `dev.flickmeon.ai` with Claude-in-Chrome: sign-up → held card → move Drop into the past → container + Friday's first run → Join → Connect. Then the same on `admin.flickmeon.ai/admin/launch` for prod. No PR, no deploy, no Clerk Restricted/Public step — sign-ups stay open throughout.
2. **Drop** — on the card, **Create Stripe coupon + promo code** (prod = live mode; it mints the 100%-off-once coupon and a promotion code with `max_redemptions = comp_slots`, `expires_at = competition_start_at`); copy the code into the launch email; `/admin/launch` → Export CSV → send from the marketing tool. Until a coupon exists, `launch_referral` **banks** every earned free month (`free_months_banked`) and `_drain_banked` spends them retroactively on the next paid invoice once it does.
3. **Through competition start** — nothing. The promo code expires itself; the competition starts on the clock.
4. **End** — `/admin/launch` → Refresh all; judges pick from the table; announce by email.

## Testing

- Backend: phase math (boundaries, unset config ⇒ 404), waitlist idempotency + counter race, referral attribution rules (self, unknown, already-attributed, already-subscribed), `can_enter` predicate matrix, key validation (prefix, Stripe auth error, never logged), `invoice.paid` reward CAS (double delivery grants once), banking + drain, revenue summation (types, refunds, mixed currency, pagination), leaderboard ordering + `pct` null on zero baseline, admin envelope + CSV.
- Frontend: one render test per phase; `MyLaunchCard` states; ref stash/attribute.
- E2E: Claude-in-Chrome on dev — join waitlist with `?ref`, sign up, activate, enter with a test-mode restricted key (`rk_test_` accepted only when `ENVIRONMENT != "prod"`), see the board.

## Non-goals (v1)

In-shell banners; self-serve key revocation (admin removes the entry via `/admin/launch`); winner display; anti-fraud beyond the paid-referral trigger and promo-code caps; multi-currency conversion; org comps.
