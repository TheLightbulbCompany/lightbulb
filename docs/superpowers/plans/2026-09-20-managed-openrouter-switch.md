# Managed OpenRouter Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing $50 managed-credit Bedrock inference path with Lightbulb-owned, per-workspace OpenRouter keys while preserving the customer subscription and credit-purchase experience; then build a 30-day no-card trial and $20 Base tier on the proven path.

**Architecture:** A server-only OpenRouter management key provisions one capped completion key per Lightbulb owner. Hermes sends all model traffic through a Lightbulb streaming gate; the gate keeps the upstream key secret, enforces the existing Metronome balance and an approved model policy, and settles OpenRouter's actual generation cost into Metronome. Bedrock remains live for unmigrated owners and rollback. Trial/Base changes are a separate second phase.

**Tech Stack:** Hermes 0.21.3 on Bedrock AgentCore, FastAPI, DynamoDB, Secrets Manager, CDK, Lambda response streaming, OpenRouter Chat Completions and Management API, Metronome, Stripe, Next.js.

**Spec:** `docs/superpowers/specs/2026-09-20-managed-openrouter-billing-design.md`

## Global Constraints

- No direct production server or environment mutation: use committed code and CI/CD. Dev deploys on merge; prod is a manual `deploy.yml` dispatch.
- Existing $50 Workspace/$150 Ultra Stripe subscriptions, Lightbulb credit purchase UI, Metronome balance, ChatGPT OAuth, and direct OpenAI/Anthropic BYOK continue through Phase 1.
- The customer never receives an OpenRouter account or key. The management key never reaches an inference endpoint; an owner completion key never reaches a Hermes profile or AgentCore environment.
- The current three-day `trial_local` remains unchanged through Phase 1. The Zen/Muse draft PR is not part of this branch.
- All new provider routes are disabled for customer traffic until Hermes, money, and rollback acceptance passes.
- OpenRouter commercial permission and selected model terms are production gates, not assumptions inferred from the existence of Management API keys.
- Every model route, including auxiliary and delegated turns, must pass the Lightbulb gate or fail closed.

---

## Phase 1 — existing managed-credit path

### Task 1: Prove external money and key contracts before building the gateway

**Files:** `docs/runbooks/openrouter-managed-inference.md` (create), `apps/backend/tests/manual/openrouter_contract_probe.py` (create, dev-only reusable probe).

**Interfaces:** A probe accepts test-mode credentials from Secrets Manager at runtime, emits only redacted JSON results, and never changes a production account. It checks one disposable dev key and one disposable Metronome customer/contract.

- [ ] Record the target OpenRouter account plan, workspace limit, key-management rate/key-count limits, whether concurrent key-limit requests can overshoot, and an approved written answer on Lightbulb's managed customer-facing use. Standard supports management keys but only five OpenRouter workspaces; start with one workspace per environment, not one per tenant. Confirm pooled-credit low-balance and auto-recharge behavior with a fail-closed 402 test.
- [x] Prove the Metronome sandbox can SUM a string provider-cost property and deduplicate replay. Both microcents and the selected `provider_cost_nanodollars` unit passed: 12,345,678 nanodollars debited exactly $0.012345678, with no second debit on transaction-ID replay. The disposable rate card was archived; the deployed rate card still needs the additive rate and contract readback in Task 5.
- [ ] Prove OpenRouter key creation, list/get/disable, one-time plaintext return, strict cap response, a streamed `usage` tail, delayed `GET /api/v1/generation`, and per-key activity readback. Document status codes and how canceled streams settle. Use no customer prompt or credential in probe output.
- [ ] Read back the development Metronome–Stripe account/Product mapping: `docs/runbooks/workspace-stripe-metronome-configuration.md` reports a mismatch that blocks dev top-up acceptance. Repair through the existing environment-specific operator workflow before claiming an end-to-end credit test passes.

### Task 2: Provision, rotate, and reap Lightbulb-owned OpenRouter keys

**Files:** Create `apps/backend/core/services/openrouter_keys.py`, `apps/backend/core/repositories/openrouter_key_repo.py`, and `apps/backend/tests/unit/services/test_openrouter_keys.py`; modify `apps/backend/core/config.py`, `apps/infra/lib/stacks/database-stack.ts`, `apps/infra/lib/stacks/service-stack.ts`; create `apps/infra/test/openrouter-key-resources.test.ts`.

**Interfaces:** `ensure_owner_key(owner_id: str) -> OwnerOpenRouterKey`, `read_owner_key(owner_id: str) -> str | None`, `disable_owner_key(owner_id: str) -> None`, and `delete_owner_key(owner_id: str) -> None`. The record contains owner ID, OpenRouter key hash, workspace ID, policy revision, secret ID, state, and timestamps. The plaintext appears only in Secrets Manager.

- [ ] Write a failing test for concurrent `ensure_owner_key` calls. It must assert one active hash/secret and no duplicate live key. Add failures for missing management credential, secret-write failure, owner removal, and rotation while an old runtime is warm.
- [ ] Run `cd apps/backend && uv run pytest tests/unit/services/test_openrouter_keys.py -q`; confirm red due to the missing service/record.
- [ ] Implement a conditional DynamoDB provisioning claim. Since OpenRouter documents no create idempotency token, recover a crash after remote creation by listing deterministic-name orphan keys, disabling an unrecorded key, and creating a replacement whose plaintext can be stored. Store the management key only in `lightbulb/{env}/openrouter/management` Secrets Manager; scope IAM to that secret and owner-key secret prefix. Set the workspace default guardrail to the broad allowed model/provider/privacy ceiling and test that a direct owner-key call cannot escape it; the gate enforces narrower per-tier policy. Document management-key rotation before expiry.
- [ ] Run the backend test, `cd apps/infra && pnpm test -- openrouter-key-resources.test.ts`, CDK synth, and backend lint. Commit this bounded lifecycle change.

### Task 3: Prove and write the pinned Hermes configuration

**Files:** Modify `apps/backend/core/services/hermes_profiles.py`, `apps/backend/core/services/hermes_provisioning.py`, `apps/backend/core/services/container_provision.py`, `apps/backend/core/services/runtime_rpc.py`, `apps/backend/core/services/owner_facts.py`, `apps/backend/core/services/agentcore_runtime.py`, `apps/infra/hermes/bridge/__init__.py`, `apps/infra/hermes/bridge/sync.py`; add focused tests in `apps/backend/tests/unit/services/`, `apps/infra/hermes/tests/`.

**Interfaces:** New stored provider choice `openrouter_managed`. The profile's primary model uses a named custom OpenAI-compatible provider at the Lightbulb gate URL, authenticated only by an owner-scoped gate bearer from the existing boot-secret mechanism. The initial model list is a versioned server-side candidate allowlist, not the full public catalog.

- [ ] Write failing profile tests asserting no `sk-or-` value, management key, or upstream OpenRouter URL appears in S3 profile content, boot environment, logs, or browser API; every member of `AUXILIARY_TASKS` has an explicit gate route and empty fallback chain. Assert the bridge's fill-missing Bedrock pin cannot add a Bedrock auxiliary lane to an OpenRouter profile.
- [ ] In a disposable dev runtime built from the pinned image in `hermes-version.json`, point a custom OpenAI-compatible model at a fake local streaming gate. Capture primary turns, multi-turn tool calls, image/vision, compression, title generation, review, delegation, background cron, and Teams/Paperclip runs. Confirm the exact config keys (`model.provider`, `model.base_url`, bearer expansion, auxiliary base URLs) on this image, not only current upstream docs.
- [ ] Implement profile/boot-secret fields and backend-owned `models.list` for OpenRouter-managed owners. Keep the owner IAM role for S3/browser/integrations, but never grant the owner direct Bedrock invoke. On provider change, rewrite all roster profiles and stop the warm AgentCore session before revoking a still-presentable old gate bearer.
- [ ] Run focused profile/bridge tests, `cd apps/backend && uv run pytest tests/unit/services/test_hermes_profiles_env_config.py tests/unit/services/test_hermes_provisioning.py -q`, and the in-image Hermes smoke. Commit only after the smoke proves the provider route works.

### Task 4: Authenticate and bound OpenRouter requests at a streaming gate

**Files:** Create `apps/infra/lambda/openrouter-gate/` and `apps/infra/lib/stacks/openrouter-gate-stack.ts`; modify `apps/infra/lib/app.ts`, `apps/infra/lib/stacks/service-stack.ts`; add Node/Vitest and CDK tests.

**Interfaces:** `POST /v1/chat/completions` receives Hermes's OpenAI-compatible body plus owner bearer. The gate checks `openrouter_managed`, paid subscription, model policy, and Metronome fast-gate balance, reserves estimated spend, fetches that owner's key, and forwards SSE. It records owner/key hash/request ID/generation ID and settlement state in DynamoDB. The upstream key is never returned.

- [ ] Write failing tests for a wrong or revoked owner token, canceled subscription, missing balance, model outside policy, client-supplied `models` fallbacks, `plugins` router override, `provider` price/privacy override, concurrent reservations, oversized prompt/output request, upstream 402/429, and disconnect after the first stream chunk.
- [ ] Run `cd apps/infra/lambda/openrouter-gate && node --test` and `cd apps/infra && pnpm test -- openrouter-gate-stack.test.ts`; confirm red.
- [ ] Implement a separate OpenRouter gate, reusing the Bedrock gate's proven owner-token and fast-gate reservation contracts where safe. For Auto Router, the gate supplies `auto-router` `allowed_models`/`cost_tier`, `provider.max_price` prompt/completion ceilings, data-collection policy, and a stable owner-scoped session ID. Treat `cost_tier` as a preference, not a cap. Reject unsupported request modes and ensure a failed stream releases or leaves a bounded reservation for reconciliation.
- [ ] Test SSE text/tool deltas, terminal usage, key non-disclosure, rate-limit propagation, and an agent-edited profile attempting to bypass policy. Run infra tests and synth; commit.

### Task 5: Replace the Bedrock-only meter for moved owners

**Files:** Create an OpenRouter settlement/reconciliation worker and DynamoDB generation/outbox table under `apps/infra/lambda/` or `apps/backend/core/billing/`; modify `apps/infra/lib/stacks/database-stack.ts`, `apps/infra/lib/stacks/service-stack.ts`, `apps/backend/core/services/spend_service.py`, focused tests and runbook.

**Interfaces:** A settled event uses `transaction_id = OpenRouter generation ID`, `customer_id = owner ID`, `event_type = openrouter_inference`, and string `provider_cost_nanodollars` (one dollar = one billion units). Keep actual model, provider, cache, token, canceled and router metadata for analytics. Metronome remains the customer balance source of truth; round the local fast-gate projection debit up to a whole microcent, then correct it on Metronome refresh.

- [ ] Write failing tests for final stream usage, stream canceled after upstream billed, gateway crash before final usage, duplicate generation lookup, two distinct billable generations from a retry, key rotation during reconciliation, and a 30-day activity lookback boundary.
- [ ] Run focused tests to confirm no OpenRouter settlement path exists.
- [ ] Persist an outbox before forwarding. Resolve final cost from `GET /api/v1/generation?id=...` when necessary; ingest to Metronome with its 34-day transaction-ID dedup. Add a metric to the existing rate card or create a versioned replacement that rates both `bedrock_invocation` and `openrouter_inference`, so rollback owners retain their credit path. Retry pending events via a worker/DLQ; compare daily per-owner OpenRouter key usage with settled events and alarm/fail closed on an unexplained gap. Broadcast traces can aid diagnosis but are not treated as a billing delivery guarantee.
- [ ] Exercise a real dev top-up through Metronome/Stripe, run one Hermes request, read back OpenRouter cost, Metronome debit, fast-gate balance, and duplicate replay behavior. Run meter tests and commit.

### Task 6: Expose the managed route through existing product surfaces

**Files:** Modify `apps/backend/core/services/billing_service.py`, `apps/backend/core/services/provision_gate.py`, `apps/backend/routers/billing.py`, `apps/backend/schemas/billing.py`, `apps/backend/core/services/runtime_rpc.py`, existing frontend/mobile provider selectors and tests, and `apps/infra/hermes/skills-src/lightbulb/references/company-and-plans.md` plus `MAINTENANCE.md`.

- [ ] Write failing API/UI tests: a $50 owner can choose `openrouter_managed` without seeing an OpenRouter account/key prompt; purchased Lightbulb credits still display and gate inference; BYOK/ChatGPT remain selectable; an unpaid owner cannot invoke the managed route; model choices come from the server-approved catalog.
- [ ] Implement the provider choice behind a default-off capability flag. Preserve current Stripe subscription Checkout and `metronome_funding.request_top_up` body/idempotency contract. Do not rename the money-path `isol8 top-up …` description.
- [ ] Run backend unit tests, frontend unit/typecheck/lint, mobile tests if touched, and the Hermes skill build. Commit the UI/entitlement change.

### Task 7: Acceptance, cutover, and rollback

**Files:** `docs/runbooks/openrouter-managed-inference.md`, relevant CI/image pins and runtime-roll scripts only as required by a tested image change.

- [ ] Verify the current production Metronome balances and top-up intents before any retirement; the owner reports no real customer credit use, but a readback is required before deleting a money path.
- [ ] Dev smoke: subscription → credit purchase → provisioned key → all six starter agents → model switch → Auto Router bounded route → canceled stream → cost reconciliation → depleted balance → top-up recovery → rollback to Bedrock. Check no upstream key exists in S3 profiles, AgentCore env, browser responses, or logs.
- [ ] Promote the tested Hermes candidate image to dev by pin PR and roll a dedicated dev runtime; a pin flip alone is not a rollout. Observe CI and the live runtime version. Do not use a production server hot patch.
- [ ] After written OpenRouter commercial approval and the dev money proof, enable one production owner, watch the dispatch deployment, pooled-credit balance, per-key caps, and reconciliation for several daily windows, then expand cohorts. Preserve Bedrock AIPs/gate/meter for rollback. Retire them only in a later PR after every managed owner has moved.
- [ ] Before any mobile owner is migrated, update the mobile AI-data recipient disclosure and privacy policy for OpenRouter and its routed model providers; version the stored consent so an owner who agreed only to the old Bedrock/Anthropic/OpenAI list is asked again before sending content through the new route.

## Phase 2 — 30-day no-card trial and $20 Base after Phase 1 succeeds

### Task 8: Set the included model policy and cost ceilings from actual Hermes tasks

**Files:** `docs/runbooks/openrouter-managed-inference.md`, versioned model-policy configuration and evaluation fixtures/tests.

- [ ] Run identical multi-tool, delegation, vision, long-context, recovery, and final-report tasks on pinned DeepSeek V4.1 Flash, GLM 5.3 Flash, MiniMax M3, and constrained Auto Router. Record pass rate, invalid tool calls, p95 latency, input/output/cache usage, and upstream cost **per successfully completed task**. Treat OpenRouter Hermes token-share rankings as adoption, not a quality benchmark. Keep Jev out of the chat-model comparison because it returns typed decisions.
- [ ] Choose the trial/Base allowlist and finite included allowance from that measured distribution plus the approved per-signup and per-subscriber cost budget. Set explicit owner-key limits and model/provider privacy policy; test a price increase, model removal, and provider 429 against the chosen ceiling.

### Task 9: Extend the existing local trial, then enable optional top-ups

**Files:** `apps/backend/core/repositories/billing_repo.py`, `apps/backend/core/services/trial_cap.py`, `apps/backend/core/services/provision_gate.py`, `apps/backend/core/billing/metronome_provisioning.py`, `apps/backend/core/billing/metronome_funding.py`, trial purchase route/tests, `apps/frontend/src/lib/planLabel.ts`, onboarding/billing UI/tests.

- [ ] Write failing tests for a 30-day clock armed only on canonical readiness, concurrent status polls, no payment method at signup, full listening mode, expiry without automatic charge, and preserved workspace data. Test that an explicit trial credit purchase does not extend `trial_end` and unused purchased credits survive Base conversion.
- [ ] Run focused backend/frontend tests to confirm red on the current 3-day implementation.
- [ ] Extend `trial_local` rather than starting a Stripe subscription at signup. Provision a no-card Metronome customer/contract and finite included allowance only after the Phase 1 cost metric and payment path are proven. First trial top-up collects a payment method explicitly, then uses a payment-gated credit commit; no spendable credit is granted before collection. Keep the 30-day entitlement clock independent of positive credit balance.
- [ ] Verify dev trial signup, allowance exhaustion, optional payment, day-30 expiry, and Base conversion; run tests and commit.

### Task 10: Add Base at $20 without changing $50/Ultra

**Files:** `apps/backend/core/config.py`, `apps/backend/core/services/billing_service.py`, `apps/backend/core/services/stripe_webhook_handlers.py`, `apps/backend/core/repositories/billing_repo.py`, backend schemas/routers/tests, web/mobile pricing and provider UI/tests, `apps/infra/lib/stacks/service-stack.ts`, agent-facing skill references.

- [ ] Write failing tests for a Base checkout with one $20 recurring Price, Base's restricted model policy and credits, $50's broader model/BYOK/ChatGPT choices, plan changes with one subscription item, invoice failure, duplicate/out-of-order webhooks, and no trial auto-charge.
- [ ] Run focused tests to confirm Base is absent.
- [ ] Add separate dev/prod Stripe Base Price IDs and server-owned display price. Grant each paid-period included allowance only after an authenticated paid invoice, keyed durably by subscription+invoice/period so retries after Stripe/Metronome's 24-hour idempotency retention cannot double-grant. Purchased credits remain in the owner's balance across trial→Base and Base→Workspace. Do not mutate the existing $50 or Ultra Price.
- [ ] Run backend/frontend/mobile/infra checks, dev end-to-end billing acceptance, and then promote through CI/CD. Verify live price/catalog strings and delete this plan/spec after the feature ships, per repository planning-doc policy.
