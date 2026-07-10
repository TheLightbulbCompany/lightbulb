# Phase A — Signup → Free Chat (auto-default `trial_local` on Meta) — Sub-Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`. Grounded in the current-on-main insertion points (workflow `wy0kve76i`, 2026-07-10). **Uncapped → DEV-ONLY** until Phase B lands the $3/day cap; do not open to real users.

**Goal:** A brand-new signup (no billing row, no subscription) auto-provisions a **`trial_local`** container on **Meta Muse Spark** and lands in chat — no card, no provider picker, no CTA.

**Architecture:** `trial_local` is a new `subscription_status` on `billing_accounts`; `meta` is a config-path provider mode (never a stored `provider_choice`). Two-axis entitlement: `trial_local` ∈ usable (`_ACTIVE_STATUSES`) but ∉ always-on (`_ALWAYS_ON_STATUSES`). Dev base already bundles the `meta` provider (7.1-beta.3); prod stays 6.10.

**Global constraints:** model `meta/muse-spark-1.1` (locked, fallback==primary); key via `MODEL_API_KEY` task-def secret from `isol8/<env>/meta-model-api-key` (never in `openclaw.json`); effort `medium`, `contextTokens 200000`, `maxTokens 32000`; generated config must pass the 7.1 `.strict()` schema test. Branch + PR, no push without approval.

---

## Task 1 — `billing_repo.create_trial_local_if_absent`

**Files:** Modify `core/repositories/billing_repo.py`; Test `tests/unit/repositories/test_billing_repo.py`

- [ ] **Test:** creating for a new owner writes `subscription_status="trial_local"`, `owner_type`, `trial_end` (now+3d); a second call (or an existing `active`/`canceled` row) is a **no-op** (conditional `attribute_not_exists(owner_id)`), returns without overwrite.
- [ ] **Run** it → fails (method missing).
- [ ] **Implement:** `create_trial_local_if_absent(owner_id, owner_type)` mirroring `create_if_not_exists` (`billing_repo.py:157`) idiom — conditional `PutItem`, `subscription_status="trial_local"`, `trial_end` epoch = now+3d, no `stripe_*`. Swallow `ConditionalCheckFailedException` (already exists → no-op).
- [ ] **Run** → pass. **Commit.**

## Task 2 — `provision_gate`: make `trial_local` usable + provision-eligible + the always-on seam

**Files:** Modify `core/services/provision_gate.py`; Test `tests/unit/services/test_provision_gate.py`

- [ ] **Test:** `is_subscription_active({"subscription_status":"trial_local"})` is `True`; `is_trial_blocked(trial_local)` is `False`; `evaluate_provision_gate` returns `None` (no gate) for a `trial_local` row with **no `provider_choice`** (i.e. Layer-2 `provider_required` does **not** fire).
- [ ] **Run** → fails.
- [ ] **Implement:**
  - `:32` `_ACTIVE_STATUSES = frozenset({"active","trialing","trial_local"})`.
  - in `_evaluate_provision_gate_inner`, **before Layer 2 (`:281`)**: `if account.get("subscription_status") == "trial_local": return None` (provider is resolved to meta, not stored on the row).
  - Leave `TRIAL_BLOCKED_STATUSES` unchanged.
- [ ] **Run** → pass. **Commit.**
- [ ] *(Deferred to Phase B: the `has_always_on_entitlement` predicate — added when the reaper/entitlement enforcement actually calls it. Not defined here (YAGNI).)*

## Task 3 — `ensure_user_ready`: auto-default a new owner into `trial_local`

**Files:** Modify `core/services/user_provision.py`; Test `tests/unit/services/test_user_provision.py`

- [ ] **Test:** a no-billing new personal owner → `create_trial_local_if_absent` is called, the gate then returns `None`, and `background_provision` is dispatched. A no-billing **non-admin org member** does NOT create a trial (mirrors the container-dispatch predicate). An existing `active` owner is untouched.
- [ ] **Run** → fails.
- [ ] **Implement:** add `from core.repositories import billing_repo`; between `:68` and the gate call `:72`:
  ```python
  if not auth.is_org_context or auth.is_org_admin:
      await billing_repo.create_trial_local_if_absent(
          owner_id=owner_id,
          owner_type=("org" if auth.is_org_context else "personal"),
      )
  ```
  (Idempotent — safe on every ~3s poll. Gate stays pure; the write lives only here.)
- [ ] **Run** → pass. **Commit.**

## Task 4 — `resolve_provider_choice` → `meta` for `trial_local`

**Files:** Modify `core/services/container_provision.py`; Test `tests/unit/services/test_container_provision.py`

- [ ] **Test:** `resolve_provider_choice` returns `("meta", None)` for a `trial_local` row; unchanged (`bedrock_claude`/byo/etc.) for others.
- [ ] **Run** → fails.
- [ ] **Implement:** at `:28`, before the existing default: `if (row or {}).get("subscription_status") == "trial_local": return "meta", None`.
- [ ] **Run** → pass. **Commit.**

## Task 5 — `config.py` meta provider branch (+ effort/clamps)

**Files:** Modify `core/containers/config.py`; Test `tests/unit/containers/test_config.py`

- [ ] **Test:** `build_provider_block(provider_choice="meta", ...)` returns `({}, {"primary":"meta/muse-spark-1.1","fallbacks":["meta/muse-spark-1.1"]}, {})` (no raise); `build_openclaw_config_dict` for meta sets `agents.defaults` reasoning-effort `medium` + `contextTokens 200000` + `maxTokens 32000`.
- [ ] **Run** → fails (raises `Unknown provider_choice`).
- [ ] **Implement:**
  - `config.py:482` (before the `raise`): add the `if provider_choice == "meta":` branch returning the 3-tuple above (provider is bundled/`enabledByDefault` in the 7.1 base — no `models.providers` block, no plugin entry).
  - `build_openclaw_config_dict` (`:841`, the `agents.defaults` dict ~`:989-1075`): when `provider_choice == "meta"`, set the reasoning-effort + `contextTokens`/`maxTokens` keys. **Verify exact key placement against the 7.1 schema in Task 9.**
- [ ] **Run** → pass. **Commit.**

## Task 6 — `MODEL_API_KEY` injection for meta containers

**Files:** Modify `core/containers/ecs_manager.py`; Test `tests/unit/containers/test_ecs_manager.py` (or a focused unit on the secret-list builder)

- [ ] **Test:** `provision_user_container(provider_choice="meta", ...)` builds `secrets_for_task` containing `{"name":"MODEL_API_KEY","valueFrom": <arn>}` (arn from `describe_secret("isol8/<env>/meta-model-api-key")`); a bedrock/byo provision does **not** get `MODEL_API_KEY`; `"meta"` is accepted by the provider allowlist (no `Unknown provider_choice` raise).
- [ ] **Run** → fails.
- [ ] **Implement:** in `provision_user_container` (`:1487`), alongside the byo branch (`:1533`): `elif provider_choice == "meta": secrets_for_task = [{"name":"MODEL_API_KEY","valueFrom": _meta_secret_arn()}]` where `_meta_secret_arn()` reuses the `key_service` describe-secret idiom for `isol8/{env}/meta-model-api-key`. Relax the allowlist guard at `:1541-1545` to include `"meta"`. (Execution role already grants `GetSecretValue:*` + `kms:Decrypt` on the general CMK — no CDK/IAM change needed to read it.)
- [ ] **Run** → pass. **Commit.**

## Task 7 — Frontend: `ProvisioningStepper` free path (3 guards + no CTA)

**Files:** Modify `apps/frontend/src/components/chat/ProvisioningStepper.tsx`; Test `apps/frontend` vitest (single-file) + `tsc --noEmit`

- [ ] **Test (vitest, single file):** with `account.subscription_status === "trial_local"`, the stepper does NOT render `ProviderPicker` and enters the container/gateway/ready phases (not `payment`).
- [ ] **Run** → fails.
- [ ] **Implement:** add near `:206` `const isTrialLocal = account?.subscription_status === "trial_local";` then:
  - `:267` `shouldPollContainer = (isSubscribed || isTrialLocal) && !reachedReady`
  - `:354` `if (trigger !== "recovery" && !isSubscribed && !isTrialLocal) return "payment";`
  - `:978-981` `if (!ownerOnboarded && !isTrialLocal) return <ProviderPicker .../>;`
  (No CTA to add — the provider phase only exists with a `?provider=` param; new users flow straight through. `useProvisioningState.ts` needs no functional change — a 200 already maps to `normal`.)
- [ ] **Run** vitest single-file + `tsc --noEmit` → pass. **Commit.**

## Task 8 — Telemetry: `trial_started` + `trial_first_agent_response`

**Files:** Modify `apps/frontend/src/lib/analytics.ts`, `ProvisioningStepper.tsx`, `useAgentChat.ts`; Test `tsc --noEmit`

- [ ] Add `trial_started` + `trial_first_agent_response` to the `AnalyticsEvent` union (`analytics.ts:120-127`) — else `capture()` won't typecheck.
- [ ] Emit `trial_started` once-per-owner on the rising edge of `isTrialLocal` (mirror the `onboarding_ready` once-marker at `ProvisioningStepper.tsx:438-465`).
- [ ] Emit `trial_first_agent_response` on the first `chat_completed` for a trial user (thread `isTrialLocal` into `useAgentChat.ts:1309-1313`, once-guarded).
- [ ] `tsc --noEmit` → pass. **Commit.**

## Task 9 — Config-schema validation on the 7.1 base

**Files:** run `apps/backend/tests/chaos/invariants/test_config_schema.py`

- [ ] Run the existing 7.1 config-schema test over the **meta** config matrix (a `trial_local` owner's generated `openclaw.json`) — confirm the meta provider block + the `agents.defaults` effort/clamps validate against `openclaw-config-schema.mjs` (`.strict()`). Fix key placement (Task 5) if it rejects. **This is the crash-loop guard.**

## Task 10 — Dev E2E smoke (the deliverable)

- [ ] Sign up a **fresh** dev Clerk account (not the E2E reserved one) at `dev.isol8.co/sign-up`.
- [ ] Confirm the poll auto-creates `trial_local` (DDB), provisions a `meta` container on the 7.1 dev base, and the UI lands in chat **with no card/provider screen**.
- [ ] Send a message → Friday replies on `muse-spark-1.1` (verify via container logs / the model in the response).
- [ ] *(Fallback if the real path needs debugging: `POST /debug/provision?provider_choice=meta` after adding the dev-only provider param + `secrets_for_task` — optional, self-contained smoke.)*

**Deliverable:** a fresh dev signup → streaming Friday chat on Meta, auto, no walls. Uncapped/dev-only until Phase B.

## Sequencing & self-review
- Backend dependency order: **T5 (config, else raise) → T2 (gate, else blocked) → T6 (secret, else auth-fail) → T1/T3/T4 (auto-default + selection).** T7/T8 (frontend) after the backend returns 200 for `trial_local`. T9/T10 last.
- **Placeholder scan:** none — every task has code + insertion line.
- **Consistency:** `trial_local` / `meta/muse-spark-1.1` / `MODEL_API_KEY` / `_ACTIVE_STATUSES` used identically.
- **Deferred to Phase B:** the $3/day cap, entitlement *enforcement* (the predicate is defined here but nothing calls it yet), spin-down. **Explicit risk:** uncapped — dev-only.
