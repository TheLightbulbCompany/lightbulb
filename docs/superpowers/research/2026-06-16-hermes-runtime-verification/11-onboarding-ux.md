# Onboarding / Provisioning UX — where the "Runtime: OpenClaw | Hermes" choice slots in

**Date:** 2026-06-16
**Scope:** signup/onboarding integration only. Builds on `docs/audit-2026-05-17-hermes-vs-openclaw.md` (§8 frontend/infra blast radius). Every claim cites a file:line I read in the current worktree.

---

## 1. The exact current step sequence a new user goes through

There are **two distinct surfaces** that the prior audit's "frontend blast radius" section under-described: a Clerk-only **personal/org chooser** (`/onboarding`) and the **billing+provider+provision state machine** (`ProvisioningStepper` rendered on `/chat`). They are sequential but live in different files.

### Step A — Personal vs Org (Clerk only, no backend provider/runtime touch)
`apps/frontend/src/app/onboarding/page.tsx`:
- The chooser renders two buttons: "Personal" (`handlePersonal`, line 244-254) and "Organization" (`setMode("org")`, line 256-266).
- Personal → `user.update({unsafeMetadata:{onboarded:true}})` + `api.syncUser()` + `router.push("/chat")` (lines 120-131).
- Org → Clerk `<CreateOrganization>` then `<InviteTeammatesStep>` then `/chat` (lines 173-232).
- **This page captures NO provider choice and NO billing.** It only decides Clerk tenancy and forwards to `/chat`. The provider/runtime decision happens entirely on the next surface.

### Step B — The `ProvisioningStepper` phase machine (on `/chat`)
`apps/frontend/src/components/chat/ProvisioningStepper.tsx`. The `Phase` union (lines 36-44) is:
`payment → provider → container → gateway → channels-sidecars → agent → channels → ready`.

The phase is **derived purely from data** in the `useMemo` at lines 386-435. Ordering of the gates that matter here:
1. `payment` — `if (trigger !== "recovery" && !isSubscribed) return "payment"` (line 392). Renders `ProviderPicker` (lines 902-905 / the `needs-signup` early return at 646-648). The picker's three cards come from `PROVIDER_CARDS` in `apps/frontend/src/lib/providerCards.ts` (`chatgpt_oauth | byo_key | bedrock_claude`, lines 33-74) and each click calls `POST /billing/trial-checkout {provider_choice}` then redirects to Stripe (lines 1383-1405).
2. `provider` — `if (needsProviderStep) return "provider"` (line 394). `needsProviderStep = providerChoice !== null && !providerStepDone` (line 260), where `providerChoice` is read from the `?provider=` query param (lines 257-258). This phase renders the **inference-credential step**: `ChatGPTOAuthStep` / `ByoKeyStep` / `CreditsStep` (lines 687-708).
3. `container → gateway → channels-sidecars → agent` — all the cold-start sub-phases, derived from `container.phase`/`substatus` returned by `GET /container/status` (lines 398-416). These are passive; the backend is already provisioning.
4. `channels → ready` (lines 420-434).

**So the real captured sequence is:** Clerk tenancy (`/onboarding`) → **subscribe + pick inference provider** (`ProviderPicker` → Stripe) → **satisfy that provider's credential** (`provider` phase: OAuth / BYO key / credits) → backend auto-provisions container → ready.

### Where the provider choice is persisted (the model for runtime_kind)
- The picker's `POST /billing/trial-checkout` handler (`apps/backend/routers/billing.py:404-419`) **synchronously** writes `provider_choice` (+ optional `byo_provider`, + `oauth_connector_user_id`) to the billing row via `billing_repo.set_provider_choice` **before** creating the Stripe session. It's also threaded into Stripe subscription metadata as a webhook backup (lines 421-429).
- `billing_repo.set_provider_choice` (`apps/backend/core/repositories/billing_repo.py:231-295`) is a plain DDB `update_item` on the **billing-accounts** row (keyed by `owner_id`), validated against `_VALID_PROVIDER_CHOICES` (line 258).

---

## 2. WHERE the runtime selector should appear, and the credential-gating interplay

### Recommendation: runtime is selected on the SAME card-grid as provider, BEFORE the inference credential step — i.e. fold it into / immediately after the `ProviderPicker`, never after.

Rationale grounded in the gate logic:

- **The provision gate is provider-keyed, and Hermes reuses the exact same credentials.** `evaluate_provision_gate` (`apps/backend/core/services/provision_gate.py:156-324`) branches only on `provider_choice`: Layer 1 subscription, Layer 2 "provider must be picked" (line 248-260), Layer 3 provider-specific creds — `bedrock_claude` → credits (263-279), `chatgpt_oauth` → OAuth tokens (281-301), `byo_key` → `byo_provider` set (303-322). The audit (§5 provider auth) established Hermes consumes Bedrock/ChatGPT/BYOK the same way. **Therefore runtime choice does NOT change the gate — the same Layer-3 credential applies regardless of runtime.** This is the single most important interplay finding: **the credential gate is orthogonal to runtime_kind.** A Hermes user still has to satisfy the identical `oauth_required` / `byo_provider_required` / `credits_required` gate.

- **Runtime does NOT constrain provider, nor vice-versa** (per the audit's §5 finding that both runtimes speak Bedrock/OAuth/BYOK). So the two choices are independent axes. The cleanest UX is therefore: present runtime as a small toggle/segment on the provider picker screen (default OpenClaw), and let the user pick provider underneath it. Putting runtime AFTER the credential step (the `provider` phase) would be wrong because:
  - the credential step (`ChatGPTOAuthStep`/`ByoKeyStep`/`CreditsStep`, lines 697-705) is provider-specific and is gated/keyed independently; inserting a runtime decision after it adds a phase with no new credential to collect.
  - the gate (`provision_gate.py`) fires `provider_required` (line 249) the moment a subscription exists with no `provider_choice` — there is no symmetric `runtime_required` gate today, and we do **not** need one if runtime defaults to OpenClaw (see §3).

- **Edge case the picker already handles:** `ProviderPicker.handlePick` only submits `{provider_choice}` (line 1387-1389). To carry runtime, extend that body to `{provider_choice, runtime_kind}` so it lands on the billing row in the same synchronous write — no new round-trip, no new phase.

---

## 3. What persists the choice, and the exact moment provisioning branches on it

### Persistence: a new `runtime_kind` field, written next to `provider_choice` on the billing row, and stamped onto the containers row at provision time.

There is currently **zero** `runtime_kind` / `hermes` reference anywhere in the backend (verified: `grep -rn "runtime_kind\|hermes\|Hermes" --include='*.py' core/ routers/` returns nothing). This is greenfield. Two viable stores, and the right answer is **both, with billing-row as source of truth**:

1. **Source of truth — billing-accounts row, alongside `provider_choice`.** Extend `TrialCheckoutRequest` (`apps/backend/routers/billing.py:257-274`) with `runtime_kind: Literal["openclaw","hermes"] = "openclaw"`, and extend `billing_repo.set_provider_choice` (`billing_repo.py:231-295`) to also `SET runtime_kind`. This mirrors `provider_choice` exactly and rides the same synchronous pre-Stripe write (billing.py:410-419) + webhook backup. Default `"openclaw"` keeps every existing row + the legacy fallback safe.

2. **Stamped copy — containers row.** `container_repo.upsert` (`apps/backend/core/repositories/container_repo.py:77-94`) is a generic `{**fields}` dict-merge, so adding `runtime_kind` to the upsert payload is free. This is the runtime-of-record for an *already-provisioned* container (needed for the "can't flip in place" detection in §4).

### The branch point: `background_provision` → `provision_user_container`.

- `background_provision` (`apps/backend/core/services/container_provision.py:32-59`) is the single dispatch site. It already calls `resolve_provider_choice(owner_id)` (lines 18-29, reads `provider_choice`+`byo_provider` off the billing row) and passes them into `get_ecs_manager().provision_user_container(...)` (lines 52-57). **This is exactly where `runtime_kind` should be resolved and threaded** — add `runtime_kind` to `resolve_provider_choice`'s return and to the `provision_user_container` call.
- `provision_user_container` (`apps/backend/core/containers/ecs_manager.py:1438-1496+`) already takes `provider_choice` / `byo_provider` / `owner_type` and does **provider-specific pre-task setup** (OAuth pre-stage 1477-1483, BYO secrets 1484-1490) and registers a task definition per provision. The runtime branch (which image, which config builder, which port/healthcheck) belongs here, parallel to the existing provider branches. The task def is re-registered on every provision (the restart path calls `_register_task_definition` — confirmed at ecs_manager.py ~1625), so the image/config can legitimately differ per provision.

`ensure_user_ready` (`apps/backend/core/services/user_provision.py:49-138`) needs **no change** — it's runtime-agnostic; it dispatches `background_provision(owner_id, auth.user_id)` (line 138) and the runtime resolution happens downstream.

---

## 4. The "can't flip in place" constraint, and the UX for it

### Confirmed: switching runtimes is a re-provision, not an in-place toggle.

`provision_user_container`'s own docstring (`ecs_manager.py:1446-1454`) enumerates the four scenarios — (1) no service, (2) scaled-to-zero, (3) running → **force new deployment**, (4) error → clean up + re-provision — and states "Preserves EFS data (agent files) across all scenarios." A runtime switch is fundamentally scenario 3/4: the **container image and task definition differ** between OpenClaw and Hermes (audit §1: OpenClaw is a Node daemon, Hermes is `python -m gateway.run`; entirely different process, port — OpenClaw WS vs Hermes `127.0.0.1:8642` HTTP+SSE / `tui_gateway` WS). You cannot hot-swap the image of a running task; ECS only changes the image by registering a new task def and forcing a new deployment.

Critically, **EFS-workspace compatibility is NOT guaranteed across runtimes** (the audit's §6 marketplace-artifact and §3 config-schema sections show the on-disk `openclaw.json` and agent layout are OpenClaw-shaped). So a runtime flip is not merely "redeploy the same EFS with a new image" — it is closer to a fresh provision. The memory `project_efs_sqlite_wal_two_task_corruption` also warns that any `forceNewDeployment` at the ECS default 200% maxPercent risks two-task EFS corruption, so a runtime switch must use the single-writer (maxPercent=100) path.

### Recommended UX for existing-user runtime switch
- **Onboarding (new user):** runtime is a one-time pick on the provider screen. No flip concern.
- **Existing user (Settings / LLM panel):** surface runtime as a **destructive "Switch runtime" action**, not a silent toggle. It should:
  1. Read the current `runtime_kind` off the **containers** row (the runtime-of-record), compare to the requested one.
  2. If equal → no-op.
  3. If different → confirm dialog ("Switching to Hermes re-provisions your container. Your Friday/Atlas agents and workspace will be rebuilt. This takes a few minutes."), then write `runtime_kind` to the billing row and trigger the same re-provision path the existing `PATCH/DELETE+POST /debug/provision` or a dedicated reprovision endpoint uses.
  4. Re-render `ProvisioningStepper` with `trigger="recovery"` (lines 173-175 / phase derivation line 392 skips `payment` for recovery) so the user sees the familiar `container → gateway → … → ready` cold-start animation instead of a dead spinner.
- Because the gate is provider-keyed and runtime-orthogonal (§2), the switched container re-uses the existing credential with no re-auth. The user does **not** redo OAuth/BYO/credits.

---

## 5. Contradictions / corrections vs the prior audit

- The audit's §8 frontend blast radius implied a single onboarding surface. **In fact there are two:** `/onboarding/page.tsx` (Clerk tenancy only) and `ProvisioningStepper.tsx` on `/chat` (the actual provider+provision machine). The runtime selector belongs in the latter, co-located with `ProviderPicker`/`PROVIDER_CARDS` — NOT in `/onboarding/page.tsx`.
- The audit did not state where `provider_choice` is persisted. Confirmed: synchronously on the **billing-accounts** row at `/billing/trial-checkout` time (billing.py:410-419), read back at provision time via `resolve_provider_choice` (container_provision.py:18-29). `runtime_kind` should follow this exact lifecycle.
- No contradiction found on the credential model: the gate (`provision_gate.py`) is purely `provider_choice`-driven and runtime-agnostic, which matches the audit's §5 claim that Hermes reuses Bedrock/OAuth/BYOK. This is load-bearing: it means **adding Hermes needs no new gate code**, only a `runtime_kind` field + a branch in `provision_user_container`.
