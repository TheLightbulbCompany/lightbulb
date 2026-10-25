# Budget metering via Paperclip — agent-accurate, gate-captured cost — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Paperclip's existing per-agent/company budgets enforce on inference with REAL dollars, by capturing OpenRouter's actual `usage.cost` at the gate and feeding it to Paperclip attributed per agent — and remove the parallel budget we built by mistake.

**Architecture:** Paperclip already has budget_policies (company/agent/project) with enforcement and a Costs UI; they're inert because inference cost reaches them as $0. OpenRouter returns the real `usage.cost` synchronously in each call's final chunk, but only the GATE sees it, and Hermes only estimates (confirmed from the image, 2026-10-25). So the gate captures the real cost × markup and attributes it per-AGENT via a per-agent gate bearer (OpenRouter key stays per-owner); the settlement path posts those as agent-scoped cost_events into Paperclip, lighting up its agent/company budgets. Per-project inference stays approximate (no per-run tag) — accepted (owner 2026-10-25). The hard floor (Metronome reservation + OpenRouter per-key limit) and Metronome-as-async-ledger are unchanged.

**Tech Stack:** Node ESM gate Lambda + Python settlement Lambda (`apps/infra/lambda/openrouter-gate/`, `.../openrouter-settlement/`), FastAPI backend (`apps/backend/core/*`, `routers/teams/`), React frontend. Paperclip fork is CALLED (its existing company cost-events endpoint), not modified.

**Spec/design:** `docs/research/2026-10-24-paperclip-budget-reconciliation.md` (reconciliation + vendor pattern + the 2026-10-25 Hermes confirmation + the agent-accurate decision).

## Global Constraints

- **Paperclip is the single budget source of truth.** No second budget store. Authoring/display = Paperclip's Costs page, surfaced via the Teams BFF.
- **Real cost, captured at the gate:** OpenRouter `usage.cost` (final chunk; `GET /generation` fallback on a cut stream) × `OPENROUTER_MARKUP` (repo-root `openrouter-pricing.json`, the same multiplier settlement already uses). Never an estimate.
- **Per-agent attribution:** a per-agent GATE BEARER; the per-owner OpenRouter key and its daily limit are UNCHANGED (do not mint per-agent OpenRouter keys — that would fragment the key limit/credits).
- **No double-count:** Paperclip's heartbeat already writes one $0 "unpriced" cost_event per managed run; our agent-scoped priced events must be ADDITIVE to those $0s, not a second priced copy. Leave the heartbeat event at $0; our event carries the dollars.
- **Managed path only.** BYO/ChatGPT runs produce no managed cost; emit no cost_event for them.
- **Credit floor intact:** nothing here touches the Metronome reservation or the per-key limit (the hard real-time stop).
- Branch: fresh off `origin/main`, carry the research/spec docs + the resolved-model commit `7431719a9`; abandon the held duplicate.

## Review Focus

- **Double-count:** an agent's budget must not count both the heartbeat $0 and our priced event as separate charges, and must not double on a settlement retry (dedupe by generation_id) — pins in Task 3.
- **Cut/aborted stream:** missing final-chunk cost falls back to `GET /generation`, never a silent $0 — pins in Task 2.
- **Per-agent bearer isolation:** one agent's bearer must map only to that agent+owner; a leaked/forged bearer must not bill another owner — pins in Task 1.
- **Managed-only:** a BYO/ChatGPT run emits no cost_event — pins in Task 3.
- **Sub-cent carry:** a cheap managed call is sub-cent; the backend accrues microcents and posts whole cents only when the carry crosses ≥1¢, keeping the remainder per `(owner, agent, month)`, so cheap-model budgets accrue correctly and nothing rounds to a silent 0 — pins in Task 3.
- **Best-effort, never blocking money:** the Paperclip post runs after the Metronome ingest + settle, so a Paperclip/endpoint failure under-counts the (soft) budget but never fails settlement or double-counts on retry (the settle-once gate) — pins in Task 3.
- **Credit floor + per-key limit unchanged** by the new bearer/capture — pins in Task 1/2.

---

### Task 1: Per-agent gate bearer

**Files:** `apps/backend/core/billing/bedrock_gate_tokens.py` (mint per-agent bearer), `apps/backend/core/services/hermes_provisioning.py` (boot secret / per-profile `.env`), `apps/backend/core/services/hermes_profiles.py` (write the agent's bearer into its `.env`), the `bedrock-gate-tokens` DynamoDB row (store `agent_id` beside `owner_id`), `apps/infra/lambda/openrouter-gate/core.mjs` (`authenticateOwner` → also resolve `agent_id`). Tests alongside each.

**Interfaces:** Produces: a gate bearer per roster agent, hash-stored as `{token_hash → owner_id, agent_id}`; `authenticateOwner` returns `{ownerId, agentId}`.

- [ ] **Step 1:** failing gate test — a per-agent bearer resolves to `{ownerId, agentId}`; an unknown/forged bearer still 401s; the owner's OpenRouter key selection is unchanged.
- [ ] **Step 2–4:** implement mint + store + profile `.env` write + gate resolution; run the gate + backend tests green; commit.

### Task 2: Gate captures real cost × markup

**Files:** `apps/infra/lambda/openrouter-gate/forward.mjs` (read `usage.cost` from the final SSE chunk — it already parses that chunk for `id`/`model`), `core.mjs` (× markup; stamp `{cost_microcents, owner, agent, generation_id}` on the requests row), settlement for the cut-stream `GET /generation` fallback.

**Interfaces:** Consumes Task 1's `agentId`; produces a requests row carrying real per-call cost + owner + agent.

- [ ] **Step 1:** failing gate test — a final chunk with `usage.cost` yields `cost_microcents = usage.cost × markup` stamped with owner+agent; a cut stream (no final chunk) marks the row for the `/generation` fallback, never $0.
- [ ] **Step 2–4:** implement, green, commit.

### Task 3: Feed per-agent real cost into Paperclip

> **Design corrected 2026-10-25 (owner: "fix the design").** The settlement Lambda is standalone stdlib (urllib + boto3, no backend import) and Paperclip admin auth is a Better-Auth **session cookie** + `Origin` — so settlement **cannot** call `PaperclipAdminClient` directly, and it cannot replicate that auth sanely. And Paperclip stores integer **cents** while a managed per-call cost is **sub-cent** (~$0.0005 → 0.06¢ → rounds to 0), so a per-call cents post makes the budget inert for cheap models. The corrected path routes settlement → a backend internal endpoint (the established `x-lightbulb-ws-internal` / `WS_INTERNAL_SECRET` Lambda→backend pattern) → Paperclip, and carries sub-cent microcents on the backend side.

**Files:**
- `apps/infra/lambda/openrouter-settlement/index.py` — inside `settle_one`, for a managed row with `cost_microcents` + `agent_id`, **after** the Metronome ingest and **after** the row is marked settled (best-effort; Paperclip must never block the Metronome money truth), POST the backend internal endpoint with `{owner, agentId, generationId, costMicrocents, model, occurredAt}`. A failure is a logged metric, not a settle failure.
- `apps/backend/routers/` — new internal route `POST /api/v1/internal/paperclip/cost/{owner}` gated by `x-lightbulb-ws-internal` (`WS_INTERNAL_SECRET`), like `routers/container.py`'s EventBridge callback. It resolves owner→Paperclip company, accrues, and posts.
- `apps/backend/core/services/` — the accrual+carry (a per-`(owner, agent, month)` microcents remainder stored as a **pruned map on the PaperclipCompany DDB row** — internal carry, NOT a second budget store; Paperclip stays the budget source of truth) and `PaperclipAdminClient.post_cost_event(company_id, session_cookie, {...})` → `POST /api/companies/{company}/cost-events`.
- `apps/infra/lib/stacks/openrouter-gate-stack.ts` — pass `BACKEND_INTERNAL_URL` + `WS_INTERNAL_SECRET` to the settlement Lambda.

**Interfaces:** Consumes Task 2's `cost_microcents` + `owner_id` + `agent_id` (= the Paperclip agent UUID — `hermes_provisioning.py:1322`, no mapping) + `generation_id`. Produces Paperclip cost_events `{agentId, provider:"openrouter", model, costCents, occurredAt}`. `costCents = floor(accrued_microcents / 1e4)` posted when the carry crosses ≥1¢; remainder carried.

- [ ] **Step 1 (PaperclipAdminClient):** failing test → add `post_cost_event`; green.
- [ ] **Step 2 (backend endpoint + accrual):** failing test — a managed cost accrues on the company row; crossing 1¢ posts ONE agent-scoped cost_event with the carried whole cents and keeps the sub-cent remainder; a second month keys separately; the endpoint is `x-lightbulb-ws-internal`-gated (401 without). Green.
- [ ] **Step 3 (settlement wiring):** failing test — a managed settled row with `cost_microcents` + `agent_id` calls the endpoint once (after settle); a BYO/ChatGPT or `agent_id`-absent or `cost_microcents`-absent row calls nothing; the heartbeat's $0 event is untouched (additive); a Paperclip/endpoint failure does not fail the settle. Green.
- [ ] **Step 4:** `cd apps/infra && npx cdk synth`, commit the regenerated `deploy.yml` with the settlement env vars. Verify in a dev company that an agent budget trips (80% warn, hard pause) — receipts in the ledger.

### Task 4: Surface Paperclip budgets + Costs in our app

**Files:** `apps/backend/routers/teams/` (proxy `budgets/overview`, `budgets/policies`, `costs/*`), frontend control panels.

- [ ] **Step 1–4:** failing BFF test → proxy Paperclip budgets/costs (owner/admin gated) → green → commit.
- [ ] **Step 5–8:** failing UI test → a Budgets/Costs view authors an agent/company budget (PUT through to Paperclip) and shows spend vs ceiling (brand tokens only) → green → commit. Prefer surfacing Paperclip's Costs page over rebuilding it.

### Task 5: Remove the held branch's duplicate

**Files:** delete the billing-item budget store + `PUT /billing/budgets` (`routers/billing.py`, `billing_repo.py`), the UsagePanel budget card (`UsagePanel.tsx`, `useBudgets.ts`), the gate project-budget 429 + the old `budgets.<project>.spent` accrual (`core.mjs`, settlement), and the `project_id` runtime env wiring (`hermes_provisioning.py`). Keep the resolved-model commit (it feeds the cost_event's model field).

- [ ] **Step 1:** failing test — `PUT /billing/budgets` gone; the gate no longer 429s on a project budget; the Metronome reservation + per-key limit paths still gate (untouched).
- [ ] **Step 2–4:** remove, green (gate + billing + settlement suites), `cd apps/infra && npx cdk synth` + commit any regenerated `deploy.yml`.

## Deferred (owner-accepted)
Per-project inference accuracy needs Hermes to emit a per-run tag the gate can read (static headers can't vary per run). Agent + company budgets are accurate now; project budgets are accurate on tool/data spend and approximate on the inference split until that upstream change.
