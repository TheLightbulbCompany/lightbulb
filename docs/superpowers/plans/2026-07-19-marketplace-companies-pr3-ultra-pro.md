# Marketplace Companies PR3 — Ultra Pro Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use `- [ ]` checkboxes.

**Goal:** Add a second paid tier — **Ultra Pro** ($150/mo, container resized to 2 vCPU/8 GB) — that unlocks deploying large (>10-agent) marketplace company listings (currently 409 "Requires Ultra Pro").

**Architecture:** A `plan` field on the per-owner billing record (`standard`|`ultra_pro`). An upgrade checkout on a new Stripe Ultra price; the `customer.subscription.updated` webhook derives the tier **from the price on the subscription item** (never from metadata — the #1315 clobber pattern) and, on the transition into ultra, resizes the container. **The provision path is made plan-aware so the larger size is durable** across reprovision/image-roll. The company-deploy gate reads the owner's plan to admit ultra_pro listings.

**Tech Stack:** FastAPI + DynamoDB + Stripe + AWS ECS (Fargate resize) + AWS CDK, Next.js 16 billing UI.

**Spec:** `docs/superpowers/specs/2026-07-18-marketplace-companies-design.md` (PR3 section). Base branch: `feat/marketplace-companies-pr2-deploy-engine` (stacked — review/merge PR1 → PR2 → PR3 in order).

## Global Constraints

- Stacked PR on PR2. NEVER push without the user's explicit approval.
- Backend: `cd apps/backend && uv run pytest <target> -v`; full suite at end (coverage off if disk tight). Frontend: vitest single-file; `tsc` typechecks tests.
- No PR numbers/bug references in code comments.
- Container size values are ECS **strings**: standard = `PER_USER_CPU="512"`/`PER_USER_MEMORY_MIB="2048"`; Ultra Pro = `"2048"`/`"8192"` (a valid Fargate combo). Define `ULTRA_CPU="2048"`, `ULTRA_MEMORY_MIB="8192"`.
- **Tier is derived from the Stripe PRICE on the subscription item, NEVER from `subscription.metadata`** (metadata is a stale checkout snapshot — the #1315 clobber). Mirror the existing team-seat price detection.
- **Durable sizing:** the provision/reprovision path must size by the owner's `plan`, or the resize is silently reverted. This is the single most important correctness requirement.
- Default/absent `plan` = `"standard"`.
- `plan` writes go through a setter guarded by `ConditionExpression="attribute_exists(owner_id)"` (the billing repo's phantom-row defense).
- Commit at the end of every task.

## Verified signatures (cite these — from the surface map)

- `billing_repo.get_by_owner_id(owner_id, *, consistent_read=False) -> dict | None` (line 24); `set_credits_auto_charge` (line 264) is the setter idiom to mirror.
- `settings.STRIPE_FLAT_PRICE_ID` / `STRIPE_TEAM_SEAT_PRICE_ID` (`config.py:212,216`, default `""`, populated from ECS task env). CDK: `app.ts:49-58` (pipeline secret→env) + `service-stack.ts:1107-1118` (task-def per-env ternary).
- `billing_service._build_subscription_checkout(*, owner_id, price_id, quantity, metadata, success_url, cancel_url, idempotency_key)` (line 306) — the reusable checkout seam; `create_flat_fee_checkout` (line 191) is the sibling to copy. `sync_team_seat_quantity` (line 382) shows `stripe.SubscriptionItem.modify` / `stripe.Subscription.modify` proration usage.
- Webhook: `stripe_webhook_handlers.handle_subscription_state(event, billing_service)` (line 391); price-based tier seam at lines 524-529 (`first_price_id = items.data[0].price.id`); the `entering_live` metadata gate at 491-519 (the #1315 fix — study it).
- Resize: `ecs_manager.resize_user_container(self, user_id, new_cpu=None, new_memory=None, new_image=None) -> str` (line 1051) — FUNCTIONAL (used by `update_service.apply_update:386` and `reprovision_for_user:1293`). Size core: `_build_register_kwargs_from_base(..., new_cpu, new_memory)` (line 380, `cpu=new_cpu or PER_USER_CPU` at 486). **Durability gap:** `_register_task_definition` (line 494) and `provision_user_container` (1673) do NOT thread cpu/memory → always 512/2048.
- Deploy gate: `marketplace_listings.py:385-398` (company branch, 409 at 397). `resolve_owner_id(auth)` already in scope (line 405). `billing_repo` NOT imported there — add it.
- Agent count: `paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner(*, owner_id, repo, admin_client) -> list[dict]` (line 72; returns `[]` on failure, never raises); `len(...)` = count.
- Frontend: `hooks/useBilling.ts` `BillingAccount` (add `plan`); `app/settings/page.tsx` `BillingPanel` personal card (line 267, `"Isol8 — $50 / month"`) is where the upgrade CTA hangs; `schemas/billing.py:8` `BillingAccountResponse` (add `plan`).

---

### Task 1: `plan` field on the billing record

**Files:** `core/repositories/billing_repo.py`, `schemas/billing.py`, test `tests/unit/repositories/test_billing_repo_plan.py`

**Interfaces:** Produces `billing_repo.set_plan(owner_id, *, plan: str, now_iso) -> None` (conditional `attribute_exists`), and `plan` surfaced on `BillingAccountResponse` (default `"standard"` when absent). Consumed by Tasks 3-6.

- [ ] **Step 1: failing tests** — `set_plan` writes `plan` and `get_by_owner_id` reads it back (moto/mocked table, mirror existing billing_repo tests); `set_plan` on a nonexistent owner raises (ConditionExpression); a row with no `plan` key reads as `plan` absent (endpoint defaults it to "standard").
- [ ] **Step 2: fail → Step 3: implement** `set_plan` mirroring `set_credits_auto_charge` (update_item, `#p`/`:p`, `ConditionExpression="attribute_exists(owner_id)"`). Add `plan: str = "standard"` to `BillingAccountResponse` and populate it from the row (`account.get("plan") or "standard"`) wherever the response is built (find via the existing `BillingAccountResponse(...)` construction site).
- [ ] **Step 4: pass → Step 5: commit** `feat(billing): plan field + set_plan on billing record`.

---

### Task 2: Stripe Ultra price config + CDK wiring

**Files:** `core/config.py`, `apps/infra/lib/app.ts`, `apps/infra/lib/stacks/service-stack.ts`

**Interfaces:** Produces `settings.STRIPE_ULTRA_PRICE_ID` (from ECS task env) + `ULTRA_PRICE_USD=150`. Consumed by Tasks 3/4.

- [ ] **Step 1:** `config.py`: add `STRIPE_ULTRA_PRICE_ID: str = ""` beside line 216 and `ULTRA_PRICE_USD: int = 150` beside `TEAM_SEAT_PRICE_USD`.
- [ ] **Step 2:** `app.ts:49-58`: add `STRIPE_ULTRA_PRICE_ID_DEV`/`_PROD` from `${{ secrets.* }}` (mirror the flat/team lines).
- [ ] **Step 3:** `service-stack.ts:1107-1118`: add the `STRIPE_ULTRA_PRICE_ID: env === "prod" ? process.env.STRIPE_ULTRA_PRICE_ID_PROD : ..._DEV` ternary in the task-def env block (mirror `STRIPE_FLAT_PRICE_ID`).
- [ ] **Step 4:** (No unit test — config/CDK. Confirm the settings import resolves: `cd apps/backend && uv run python -c "from core.config import settings; print(settings.STRIPE_ULTRA_PRICE_ID, settings.ULTRA_PRICE_USD)"`.) Commit `feat(billing): Stripe Ultra price config + CDK env wiring`. (Operator note for the PR: the `STRIPE_ULTRA_PRICE_ID_DEV/_PROD` GitHub secrets + the Stripe price object must be created before deploy — call this out in the PR body.)

---

### Task 3: Ultra upgrade checkout + endpoint

**Files:** `core/services/billing_service.py`, `routers/billing.py`, test `tests/unit/services/test_ultra_checkout.py`

**Interfaces:** Produces `billing_service.create_ultra_upgrade(*, owner_id, clerk_user_id=None) -> stripe.Subscription | stripe.checkout.Session` and `POST /billing/ultra-checkout` returning `{checkout_url}` (or `{upgraded: true}` for an in-place item swap). Consumed by Task 6 (frontend).

**Design decision (item-swap, in place):** an owner already on the $50 flat sub upgrades by swapping the subscription item's price to Ultra (keeps ONE subscription, prorated). The flat sub's `subscription_item_id` is not persisted, so read it live: `sub = stripe.Subscription.retrieve(account["stripe_subscription_id"])`; `item_id = sub["items"]["data"][0]["id"]`; `stripe.Subscription.modify(sub.id, items=[{"id": item_id, "price": settings.STRIPE_ULTRA_PRICE_ID}], proration_behavior="create_prorations")`. If the owner has NO active sub (trial_local / never subscribed), fall back to a fresh Ultra checkout via `_build_subscription_checkout(price_id=STRIPE_ULTRA_PRICE_ID, quantity=1, ...)`. The webhook (Task 4) does the durable state write + resize on the resulting `subscription.updated`.

- [ ] **Step 1: failing tests** — mock `stripe`: an owner with an active flat sub → `create_ultra_upgrade` calls `Subscription.modify` with the Ultra price on the existing item (idempotency key stable); an owner without a sub → builds a checkout on the Ultra price. Endpoint returns the right shape + is authed (owner-scoped).
- [ ] **Step 2-4: fail → implement → pass.** Use a stable idempotency key (`ultra-upgrade-{owner_id}`) per the repo's Stripe-idempotency practice.
- [ ] **Step 5: commit** `feat(billing): Ultra Pro upgrade checkout + endpoint`.

---

### Task 4: Webhook — derive tier from price, persist plan, resize on upgrade

**Files:** `core/services/stripe_webhook_handlers.py`, test `tests/unit/services/test_webhook_ultra_tier.py`

**Interfaces:** Extends `handle_subscription_state` — after the existing team-seat detection, derive `is_ultra = bool(settings.STRIPE_ULTRA_PRICE_ID) and first_price_id == settings.STRIPE_ULTRA_PRICE_ID`; persist `set_plan(owner_id, plan="ultra_pro" if is_ultra else "standard")`; on the transition INTO ultra (old plan != ultra, new == ultra) dispatch `resize_user_container(owner_id, new_cpu=ULTRA_CPU, new_memory=ULTRA_MEMORY_MIB)`; on transition OUT of ultra (→ standard) resize down **only if the downgrade guard passes** (see below).

**Downgrade guard (webhook-side, safe):** before resizing DOWN, count `len(fetch_paperclip_agents_for_owner(owner_id))`. If `> COMPANY_STANDARD_PLAN_MAX_AGENTS`, DO NOT resize down (leave the container at Ultra size to avoid OOM), persist `plan="standard"` (billing follows Stripe), and emit a metric/log `ultra.downgrade_deferred_oversized` for admin follow-up. (A user who trims agents then re-triggers a sub event, or an admin, completes the down-resize. Direct-portal downgrade is thus OOM-safe.)

**Defensive:** empty `items.data` → treat as standard-but-do-not-resize (don't act on a partial payload). Never derive tier from `subscription.metadata`.

- [ ] **Step 1: failing tests** — (a) `subscription.updated` on the Ultra price for a previously-standard owner → `set_plan("ultra_pro")` + `resize_user_container(cpu="2048", memory="8192")`; (b) Ultra→flat with ≤10 agents → `set_plan("standard")` + resize down to 512/2048; (c) Ultra→flat with >10 agents → `set_plan("standard")` + NO down-resize + the deferred metric; (d) metadata carrying a different tier is IGNORED (price wins); (e) empty items → no plan change, no resize. Mock `resize_user_container`, `set_plan`, `fetch_paperclip_agents_for_owner`.
- [ ] **Step 2-4: fail → implement → pass.** Keep the existing team-seat + `entering_live` provider logic untouched — you are ADDING a tier branch, not altering the #1315 guard.
- [ ] **Step 5: commit** `feat(billing): derive Ultra tier from price, persist plan, resize on upgrade`.

---

### Task 5: Durable Ultra sizing on the provision path + deploy-gate flip

**Files:** `core/containers/ecs_manager.py`, `routers/marketplace_listings.py`, tests `tests/unit/containers/test_provision_plan_sizing.py`, `tests/unit/routers/test_deploy_gate_ultra.py`

**Interfaces:** The provision path sizes by the owner's `plan`; the deploy gate admits `ultra_pro` listings for `ultra_pro` owners.

**Durable sizing (the critical piece):** make `_register_task_definition` / `provision_user_container` / `reprovision_for_user` size by plan. Cleanest: a helper `_size_for_owner(owner_id) -> tuple[str, str]` returning `(ULTRA_CPU, ULTRA_MEMORY_MIB)` if `billing_repo.get_by_owner_id(owner_id).plan == "ultra_pro"` else `(PER_USER_CPU, PER_USER_MEMORY_MIB)`; thread its result into `_build_register_kwargs_from_base` at every provision/reprovision site (currently hardcoded 512/2048). This makes the resize durable — a reprovision or image-roll re-derives the Ultra size instead of reverting it.

- [ ] **Step 1: failing tests** — `provision_user_container`/`reprovision_for_user` for an `ultra_pro` owner registers a task-def with cpu=2048/memory=8192; a standard owner gets 512/2048. Mock `billing_repo.get_by_owner_id` + the ECS register call; assert the register kwargs.
- [ ] **Step 2-4: fail → implement → pass.**
- [ ] **Step 5: deploy-gate flip** — in `marketplace_listings.py` company branch: `from core.repositories import billing_repo`; `account = await billing_repo.get_by_owner_id(owner_id)`; the 409 fires only when `company.min_plan != "standard"` AND `(account or {}).get("plan") != "ultra_pro"`. So an ultra_pro owner deploys a >10-agent company; a standard owner still 409s. (The `require_platform_admin` line from PR2 stays — the admin flag lift is a separate post-E2E step.) Tests: ultra_pro owner + ultra listing → passes the plan gate; standard owner + ultra listing → 409.
- [ ] **Step 6: commit** `feat(marketplace): durable Ultra container sizing + ultra_pro deploy gate`.

---

### Task 6: Frontend — plan display + Upgrade to Ultra Pro

**Files:** `hooks/useBilling.ts`, `app/settings/page.tsx`, `lib/api.ts` (or the billing api module), test `components/__tests__/UpgradeUltra.test.tsx` (or extend the billing panel test)

**Interfaces:** `BillingAccount.plan` (TS); `useBilling().upgradeToUltra()` → POST `/billing/ultra-checkout` → redirect to `checkout_url` (or refresh on in-place upgrade). The billing panel shows the current plan and, for a standard subscribed owner, an "Upgrade to Ultra Pro — $150/mo" button.

- [ ] **Step 1: failing test** — the panel renders "Upgrade to Ultra Pro" for a `plan: "standard"` subscribed account and calls `upgradeToUltra` on click; for `plan: "ultra_pro"` it shows an "Ultra Pro" active badge and no upgrade button. Mock `useBilling`.
- [ ] **Step 2-4: fail → implement → pass** (`tsc` + `lint` clean). Add `plan` to the `BillingAccount` interface; surface the `$150` from backend config if easy, else a labeled constant.
- [ ] **Step 5: commit** `feat(billing): Ultra Pro plan display + upgrade CTA`.

---

### Task 7: Full suites + PR

- [ ] **Step 1:** Full backend suite (coverage off) + `tsc` + `lint` green.
- [ ] **Step 2:** Whole-branch review (most capable model); fix Critical/Important.
- [ ] **Step 3: Stop for PR approval.** Present the branch. The PR body MUST call out the operator prerequisites: create the Stripe Ultra price object + the `STRIPE_ULTRA_PRICE_ID_DEV/_PROD` GitHub secrets before deploy; and the post-merge validation (upgrade a dev owner → confirm plan flips, container resizes to 2 vCPU/8 GB, a >10-agent company then deploys). Do NOT push without approval.

---

## Self-Review Notes

- Spec coverage: plan field (T1), Stripe price+CDK (T2), upgrade checkout (T3), price-derived webhook + resize + downgrade guard (T4), **durable sizing** + deploy-gate flip (T5), UI (T6), suites+PR (T7).
- The critical durability gap (provision path reverts Ultra size) is T5's headline, not an afterthought.
- The #1315 clobber is explicitly avoided (price-derived tier, `entering_live` guard untouched).
- Downgrade OOM is handled safely (defer down-resize while >10 agents) rather than left as a footgun.
- Out of scope (documented): the admin-flag lift on the deploy endpoint (post-E2E, PR2's follow-up); Stripe billing-portal plan-switch config (dashboard, not code); org/team-seat interaction with Ultra (Ultra is modeled as a flat-sub upgrade; per-seat orgs are a separate future concern).
