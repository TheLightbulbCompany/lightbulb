# Router: metering, cheapest-adequate routing, per-project budgets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every project a budget the openrouter-gate enforces, route each managed request to the cheapest adequate model, and show budget-vs-spend in the app.

**Architecture:** Extend the existing openrouter-gate (the single managed-inference path) — it already supports `openrouter/auto` + `policy.auto` routing and a per-request reservation. Store per-project budgets **as a `budgets` map on the existing per-owner billing item the gate already fetches** (`core.mjs` reads the billing row every request), so there is no new table, no new IAM grant, and no extra read. Thread a `project_id` from the runtime to the gate; the settlement step increments per-project monthly spend on that same item; extend the existing UsagePanel to show budget-vs-spend. No new inference path, no new event bus.

**Tech Stack:** Node ESM Lambda (`apps/infra/lambda/openrouter-gate/`, node:test), Python/pytest backend (`apps/backend`), React/jest frontend (`apps/frontend`).

**Spec:** `docs/superpowers/specs/2026-10-23-ramp-parity-spend-design.md` (Track A + shared budget/meter layer).

## Global Constraints

- Enforcement lives in the gate; Metronome/Stripe do billing, not limits (spec "Accepted pattern").
- Routing + budgets apply to the **managed path only**; BYO and ChatGPT OAuth do not traverse the gate.
- Markup is read once from repo-root `openrouter-pricing.json` (`OPENROUTER_MARKUP`); never hardcode it.
- Budgets live on the existing billing item; the budget dollars are a **separate pool** from managed credits but reuse this item + enforcement machinery (Track C adds a second, `purchase` entry later — not now).
- One window: **monthly**, reset at the UTC month boundary (matches the existing per-period counters and the `openrouter_capped_day` reset pattern).
- Over-budget is a structured `429 {code:"project_budget_exceeded"}` the client renders as a limit card, same shape as `trial_daily_cap`; it blocks new requests, never kills an in-flight run.
- No new SQS/queue/event bus and no new DynamoDB table; the billing item is the store.

## Review Focus

- **Capped/trial owners:** a project budget is best-effort for them (their hard valve is OpenRouter's per-key daily limit, outside the gate) — pins in Task 3.
- **Non-room traffic** (nightly CEO, routines, heartbeats) carries no project id → charges a **`"__default__"`** project bucket, not bypass — pins in Task 3.
- **Settlement lag:** per-project spend trails real cost by up to ~5 min, so a small overshoot past the ceiling is expected and must not crash or double-charge — pins in Task 3.
- **Silent downgrade:** the model actually used must be visible — pins in Task 1.
- **Model not in policy:** a request for a model outside `policy.models` is already a 400; keep it — pins in Task 1.

---

### Task 1: Multi-model routing + chosen-model visibility

**Files:**
- Modify: `apps/infra/lambda/openrouter-gate/core.mjs` (capture the resolved model from the generation onto the gate result)
- Modify: GitHub Actions vars `OPENROUTER_MODEL_POLICY_JSON_{DEV,PROD}` — populate `models` + `auto.allowed_models` + `auto.cost_tier` (config, not code)
- Test: `apps/infra/lambda/openrouter-gate/core.test.mjs`

**Interfaces:**
- Consumes: existing `prepareBody(input, policy, ownerId, env)` and the `auto-router` plugin path (already present).
- Produces: the gate result carries the resolved model id (the ledger/settlement and the UI read it).

- [ ] **Step 1: Write failing gate test** — `openrouter/auto` with a valid `policy.auto` yields `body.plugins=[{id:'auto-router',...}]` and the SSE `model` field is returned on the gate result; a model NOT in `policy.models` still fails `400 "Model not allowed"`.
- [ ] **Step 2: Run it, verify FAIL.** `node --test apps/infra/lambda/openrouter-gate/core.test.mjs`.
- [ ] **Step 3: Implement** the resolved-model capture in `core.mjs` (read `model` from the generation/SSE, attach to the result the ledger already builds). Routing already works; this only surfaces the chosen model.
- [ ] **Step 4: Run it, verify PASS.**
- [ ] **Step 5: Check** whether the room run/message already stores the responding model (grep the run-ended / message write path). If it does, the UI reads that — stop here. Only if it does NOT, add the model to the run/message record in that same path with one test.
- [ ] **Step 6: Commit** (`feat(router): surface resolved model, no silent downgrade`).

### Task 2: Per-project budget config on the billing item

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py` (budget map accessors)
- Modify: the existing billing router (`apps/backend/routers/billing.py`) — add owner/admin-only `GET/PUT /billing/budgets`
- Test: `apps/backend/tests/unit/repositories/test_billing_repo.py`, `apps/backend/tests/unit/routers/test_billing.py`

**Interfaces:**
- Produces: `billing_repo.set_project_budget(owner_id, project_id, ceiling_microcents)`, `.get_project_budget(owner_id, project_id) -> {ceiling_microcents, spent_microcents, month} | None`, `.list_project_budgets(owner_id)`. Stored as a `budgets` map attribute on the owner's billing item; `project_id="__default__"` is the workspace bucket; window is implicit monthly.

- [ ] **Step 1: Write failing repo test** — set then get round-trips a ceiling on the billing item; list returns all; a second project doesn't clobber the first.
- [ ] **Step 2: Run it, verify FAIL.**
- [ ] **Step 3: Implement** the `budgets` map accessors in `billing_repo.py` (update the existing item, don't add a table).
- [ ] **Step 4: Run `uv run pytest apps/backend/tests/unit/repositories/test_billing_repo.py -q` → PASS.**
- [ ] **Step 5: Write failing router test** — non-owner/non-admin → 403; owner set→get works; a negative/non-numeric ceiling → 422.
- [ ] **Step 6: Implement** `GET/PUT /billing/budgets` in the existing billing router (reuse its owner/admin auth).
- [ ] **Step 7: Run `uv run pytest apps/backend/tests/unit/routers/test_billing.py -q` → PASS.**
- [ ] **Step 8: Commit** (`feat(budgets): per-project budget on the billing item`).

### Task 3: Thread project_id, accrue spend, enforce in the gate

**Files:**
- Modify: `apps/backend/core/services/hermes_provisioning.py` / the runtime request envelope — carry `project_id` (room's project, else `"__default__"`) to the gate.
- Modify: `apps/infra/lambda/openrouter-gate/core.mjs` — accept `project_id`; read `billing.budgets[project_id]` off the row it already fetches; block over-budget in the reservation step. The settlement step increments `spent_microcents` for the current month on that same budget entry.
- Test: `apps/infra/lambda/openrouter-gate/core.test.mjs`

**Interfaces:**
- Consumes: the `budgets` map shape (Task 2); the resolved cost the settlement step already computes.
- Produces: `429 {code:"project_budget_exceeded", project_id}` when `spent >= ceiling` for the month.

- [ ] **Step 1: Write failing gate test** — a budget whose `spent >= ceiling` returns `429 project_budget_exceeded` for a managed-paid request on that `project_id`; under ceiling passes; no `project_id` charges `"__default__"`; a **capped/trial** owner over a project budget is NOT hard-blocked here (best-effort). Settlement increments `spent_microcents` on the right month.
- [ ] **Step 2: Run it, verify FAIL.**
- [ ] **Step 3: Implement** `project_id` acceptance + the budget read/block in the existing reservation step (paid path only), the `"__default__"` fallback, the month-keyed `spent` increment in settlement, and a stale-month reset. Tolerate a ≤5-min overshoot (compare last-known spend; do not recompute synchronously).
- [ ] **Step 4: Run it, verify PASS.**
- [ ] **Step 5: Add `project_id`** to the runtime request in `hermes_provisioning.py`. Note in the PR that warm runtimes need `scripts/roll_hermes_runtimes.py` to pick it up; do not run the roll here.
- [ ] **Step 6: Commit** (`feat(router): enforce per-project budget in the gate`).

### Task 4: Budget-vs-spend in the app

**Files:**
- Modify: `apps/frontend/src/components/control/panels/UsagePanel.tsx` (budget vs spend per project; resolved-model label where the transcript renders)
- Modify: `apps/frontend/src/lib/` data hook for `GET /billing/budgets`
- Test: `apps/frontend/src/components/control/panels/__tests__/UsagePanel.spend.test.tsx`

**Interfaces:**
- Consumes: `GET /billing/budgets` (Task 2) and the existing usage/spend read UsagePanel already uses; the resolved model (Task 1) for the label.

- [ ] **Step 1: Write failing UI test** — UsagePanel renders a project's ceiling, spend, and remaining; over-budget shows the limit state; a message shows the resolved-model label.
- [ ] **Step 2: Run `pnpm --filter @lightbulb/frontend test UsagePanel.spend` → FAIL.**
- [ ] **Step 3: Implement** the budget rows + model label, reusing UsagePanel's existing layout and brand tokens (no new color literals).
- [ ] **Step 4: Run it, verify PASS.**
- [ ] **Step 5: Commit** (`feat(router): show per-project budget vs spend`).
