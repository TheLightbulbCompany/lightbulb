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

**Files:** `apps/infra/lambda/openrouter-settlement/index.py` (post an agent-scoped priced cost_event to Paperclip's existing `POST /companies/:id/cost-events`), `apps/backend/core/services/` (a small Paperclip cost-events client method on `PaperclipAdminClient`).

**Interfaces:** Consumes Task 2's stamped cost + owner + agent + generation_id; produces a Paperclip cost_event {company, agent, model, cost_cents} deduped by generation_id.

- [ ] **Step 1:** failing test — a managed generation posts ONE agent-scoped priced cost_event (deduped by generation_id; a retry posts none); a BYO/ChatGPT generation posts none; the heartbeat's $0 run event is left untouched (additive, no double-count).
- [ ] **Step 2–4:** implement the client + the settlement post, green, commit. Verify in a dev company that an agent budget now trips (80% warn, hard pause) — receipts in the ledger.

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
