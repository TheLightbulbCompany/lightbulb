# No-Card Free Tier — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each workstream's sub-plan task-by-task. This master plan sequences the workstreams; each workstream (WS1–WS7) gets a detailed, bite-sized, TDD sub-plan authored just-in-time at execution. `docs/superpowers/specs/2026-07-10-W2-meta-provider-wiring-plan.md` is the pattern for that depth.

**Goal:** Let anyone sign up and reach a working agent **instantly — no credit card, no LLM-provider config** — running on Meta Muse Spark, with the cost of a non-converting trial bounded by a hard **$3/day** inference cap over a **3-day** window (~$9 worst case, typically far less) plus idle spin-down; then convert them to one of the three paid providers (Bedrock-managed / ChatGPT-OAuth / BYO key).

**Architecture:** Free-tier containers run Meta Muse Spark via OpenClaw's **native `meta` provider** (`meta/muse-spark-1.1`, `openai-responses` → `api.meta.ai/v1`), on the **7.1 base a parallel effort is landing** (7.1 satisfies the plugin's host-compat ≥6.11/7.1). If that base bundles meta it's built-in; otherwise we vendor `extensions/meta` (loads fine on 7.1). The **$3/day spend cap** lives as a **date-keyed daily field on the existing `billing_accounts` row** (atomic `ADD`, reserve-worst-case → reconcile from Meta's own reported `usage`, **fail-closed**) — the row `gate_chat` already reads; NOT `usage-counters` (broken/analytics-only), NOT Metronome/Stripe (can't hard-cap non-Bedrock in real time), NOT a new table, NOT Clerk (no atomic op). The **3-day clock** is `trial_local` status + `trial_end` on the same row. Free-tier containers **scale to zero via event-driven teardown** (extended `_grace_close` on last disconnect + one-time boot reconcile + a ping-deadline — **no periodic sweeper**). Onboarding drops the card+provider walls for the free path; conversion reuses `change_provider`. **Always-on autonomy (channels, webhooks, Paperclip routines) is a paid feature, enforced at runtime backend chokepoints** (`has_always_on_entitlement` → `hooks_proxy` 402 + `gate_chat` autonomous-session-key refusal + spin-down for channels) so container/Paperclip config edits can't bypass it. Paid is untouched.

**Tech Stack:** FastAPI + DynamoDB (backend), AWS CDK + Docker (infra/image), Next.js 16 (frontend web), Tauri/Rust (desktop), Meta Model API (`api.meta.ai/v1/responses`, `openai-responses` transport), Stripe (conversion only), CloudWatch/PostHog (telemetry).

## Global Constraints

- **Base image** builds from the `isol8/agent` fork, **being bumped to 7.1 by a parallel effort** (7.1 satisfies the meta plugin's host-compat ≥6.11/7.1). Meta = the **native `meta` provider** — built-in if that 7.1 base bundles meta, else vendor `extensions/meta` (loads on 7.1). ⚠️ **WS1 depends on the 7.1 base being in dev.** That bump owns: the config-schema **6.10→7.1 audit against our `config.py`** (not yet done — a changed/removed key crash-loops the container), the fork-patch rebase, the **Codex app-server 0.143+ breaking change**, and the fleet image-swap for paid containers (protocol unchanged).
- **Provider identity:** model `meta/muse-spark-1.1`; base `https://api.meta.ai/v1` (`openai-responses` transport); key env `MODEL_API_KEY`; free-tier reasoning effort `medium`; per-turn clamps `contextTokens: 200000`, `maxTokens: 32000` (the per-turn *reserve* bound → worst-case ~$0.39/turn; tunable — the daily cap bounds total regardless).
- **Pricing / cap:** Meta **$1.25/M in · $4.25/M out** (reasoning billed as output). **Cap = $3.00/DAY** (300000 microcents), **date-keyed so it resets each day**; **3-day** window (~$9 worst case, typically far less).
- **Cap = reserve-worst-case → reconcile, FAIL-CLOSED.** In `gate_chat`, atomic `ADD` reserve *before* send; reconcile to Meta's reported `usage` on `chat.final`; **deny on missing/unreadable, never refund on reconcile failure** → Isol8 can never exceed $3/day (the cost-safety guarantee).
- **Cap home = the existing `billing_accounts` row** (date-keyed daily `spend` field, atomic `ADD`) — the row `gate_chat` already reads. **NOT `usage-counters`** (broken — see note), **NOT Metronome/Stripe** (can't hard-cap non-Bedrock in real time), **NOT Clerk** (no atomic op), **no new table.** Clock/churn also on `billing_accounts` (`trial_local`/`trial_end`; `trial_expired` → `TRIAL_BLOCKED_STATUSES`).
- **Always-on entitlement is RUNTIME-enforced, not config-enforced.** New `has_always_on_entitlement(owner)` (False for `trial_local`) enforced at backend chokepoints the agent/Paperclip can't rewrite: `hooks_proxy` → 402, `gate_chat` → refuse autonomous session keys (`paperclip:`/`cron:`/`hook:`); channels die under spin-down. Always-on = channels + webhooks + Paperclip routines (paid); chat = free.
- **`usage-counters` is broken + unused here** (display wrong 3 ways, reconcile scripts dead/unrun). The cap does not touch it. Fixing/deleting the usage UI is a **separate cleanup, out of scope.**
- **Secret handling:** `MODEL_API_KEY` is an ECS task-def secret referenced via `fromSecretNameV2` from `isol8/<env>/meta-model-api-key` (already stored in dev). Never in `openclaw.json`/EFS, never in code.
- **Meta is an eligibility axis, never a selectable `provider_choice`.** Keep `_VALID_PROVIDER_CHOICES` at three; `change_provider` rejects target `meta`.
- **Personal-only** free tier (refuse `owner_id.startswith("org_")`). **One trial per Clerk account** (v1); farming defense (canonical-email keying) deferred.
- **Terms gate:** a human confirms Meta's Model-API no-train + retention terms before real free-user data flows (blocks go-live, not build).
- **Branch + PR, never direct main; no push without approval; watch CI after push.**

---

## Workstream sequence & dependency spine

```
WS1 (meta in image) → WS2 (provider wiring + reconcile GATE) → WS3 ($3 trial boundary) → WS4 (onboarding) → WS5 (conversion)
                                                     WS6 (spin-down) ─────────────────────────────────── WS7 (surfaces)   [parallel track]
```
Critical path: **WS1 → WS2 → WS3 → WS4 → WS5.** WS6/WS7 are provider-agnostic and run alongside.

---

## WS1 — Native `meta` plugin in the extended dev image (on the 7.1 base)

**Objective:** A fresh dev container boots with the native `meta` provider active and `meta/muse-spark-1.1` **locked** as the model (not user-changeable), on the 7.1 base.

**Files:**
- Create: `apps/infra/openclaw/vendor/meta/**` (vendored `extensions/meta/` from `origin/release/2026.7.1`) — *skip if the 7.1 base already bundles meta*
- Modify: `apps/infra/openclaw/Dockerfile` (bake layer near the Slack/Discord bake at `:249`)
- Modify: `openclaw-version.json` (`dev.tag`, after the image builds)
- Modify: `apps/backend/core/containers/config.py` (`build_provider_block`, `:311-482`) — the `meta` provider branch

**Tasks:**
1. **Confirm the 7.1 dev base.** Verify the parallel 7.1 bump is in dev and whether it **bundles `extensions/meta`** (`enabledByDefault:true`). If bundled → skip tasks 2–3's vendoring (avoid double-install). If not (e.g. a beta.3-tag base) → vendor it (task 2).
2. **Vendor the plugin** (only if not bundled). `git -C ~/Desktop/openclaw.nosync archive <7.1-ref> extensions/meta | tar -x -C apps/infra/openclaw/vendor/meta --strip-components=1`; record the source SHA in `VENDORED_FROM`. Add a Dockerfile COPY/bake layer placing it at `/app/dist/extensions/meta` (root-owned, mirroring the existing plugin bakes). Confirm `openclaw.plugin.json` (`id: "meta"`, `providers: ["meta"]`) lands in the discovery dir.
3. **`config.py` meta branch.** In `build_provider_block`, add the `meta` branch: `default_model = {primary: "meta/muse-spark-1.1"}` **locked** (no other-provider fallback — model not user-changeable), reasoning effort `medium`, `contextTokens: 200000`, `maxTokens: 32000`; no AIP ARNs. (Selection wiring is WS2.)
4. **Build + validate on the 7.1 dev base.** Rebuild the extended image via `build-openclaw-image.yml`, verify the tag in ECR, flip `openclaw-version.json#dev.tag`. Boot a throwaway `meta` container and confirm: no crash-loop, no double-install, `openclaw.json` validates against the 7.1 schema, provider `meta` registers, a single completion returns.

**Testable deliverable:** a dev container on the 7.1 base whose gateway is healthy and runs `meta/muse-spark-1.1`.

**Depends on:** the **7.1 base landing in dev** (parallel bump). **Risk:** double-install if the base bundles meta — task 1 resolves which path; on 7.1 the plugin's host-compat (`≥6.11/7.1`) is satisfied, so it loads.

---

## WS2 — Free-tier provider wiring + reconciliation verification

**Sub-plan already written:** `docs/superpowers/specs/2026-07-10-W2-meta-provider-wiring-plan.md`. Summary of its tasks:

**Objective:** A free-tier owner provisions a container that runs `meta`, and actual Meta token usage is captured into Isol8 accounting (the load-bearing check for the $3 cap).

**Files:** `apps/infra/lib/stacks/auth-stack.ts` + `service-stack.ts`/`container-stack.ts` (secret `fromSecretNameV2` + conditional task-def injection), `apps/backend/core/containers/config.py`, `core/services/container_provision.py` (`resolve_provider_choice`), `core/services/usage_service.py` (Meta $ computation).

**Tasks (ordered; Step 0 first):**
1. **Step 0 — reconciliation verification (GATE).** With the dev image + `MODEL_API_KEY`: provision a dev container in `meta` mode, run several Muse Spark turns (small + large + reasoning), and confirm what `sessions.list` / `_safe_record_usage` records vs. Meta's reported `usage`. **Decision gate:** actual tokens flow → proceed; else fix (container-side usage hook) or fall back to realistic-estimate reservation. Write a one-page verdict.
2. **Secret injection (CDK).** Reference `isol8/<env>/meta-model-api-key` via `fromSecretNameV2`; inject `MODEL_API_KEY` into the task def **only for free-tier** containers.
3. **Free-tier selection.** `resolve_provider_choice` returns `meta` for a trial owner (reads the trial flag from WS3, not `billing_accounts.provider_choice`); thread through `background_provision → provision_user_container → write_user_configs → write_openclaw_config → build_openclaw_config_dict`.
4. **Meta spend computation.** Add a `meta/` cost path computing `spend = input×$1.25/M + output×$4.25/M` from Meta's reconciled `usage` → written to the WS3 **daily-spend field on `billing_accounts`** (not Metronome, not `usage-counters`).

**Testable deliverable:** a free-tier dev container streams a Muse Spark reply and its real spend lands in the `billing_accounts` daily-spend field. **Depends on:** WS1 + the stored key.

---

## WS3 — Trial lifecycle + $3/day cap + always-on entitlement (backend)

**Objective:** A no-card owner provisions a trial, chats on Meta, is hard-stopped at **$3/day or 3 days elapsed** (fail-closed, atomic, on the `billing_accounts` row — no new table), and **cannot enable always-on features** (channels / webhooks / Paperclip routines).

**Files:**
- Modify: `core/services/provision_gate.py` — add `trial_local`/`trial_expired`; new **`has_always_on_entitlement(owner)`** (False for `trial_local`); `evaluate_meta_free_gate`
- Modify: `core/repositories/billing_repo.py` — `trial_local` status + `trial_end`; a **date-keyed daily `meta_spend` value** with atomic `ADD … RETURN ALL_NEW` reserve/reconcile helpers; lightweight row creation
- Modify: `core/gateway/connection_pool.py` `gate_chat` (`:1786-1847`) — (a) cap branch: reserve → block at $3/day or expiry (fail-closed) → global kill-switch; (b) **refuse autonomous session keys** (`paperclip:`/`cron:`/`hook:`) when `not has_always_on_entitlement`
- Modify: `routers/hooks_proxy.py` (`:113`) — **402 before forward** when `not has_always_on_entitlement`
- Modify: `routers/websocket_chat.py` `_safe_record_usage` (`:847`) — post-turn reconcile into the daily field
- Modify: `routers/teams/work.py` (routine create/trigger/patch, `:44-98`) — reject routine-enable for non-entitled (the biggest open hole today)
- Create: `routers/billing.py` → `POST /billing/meta-free-start`; Modify `billing_service.py` `change_provider` — reject target `meta`
- Test: `tests/unit/**` per file

**Tasks (each = TDD cycle):**
1. **Trial state + daily-spend field on the billing row.** `create_trial_local(owner_id)` → `subscription_status="trial_local"`, `trial_end=now+3d`. Add `meta_spend` as a **per-date** value (`meta_spend.<YYYYMMDD>`) with atomic `ADD … RETURN ALL_NEW` reserve/reconcile helpers. `provision_gate`: `trial_local` provision-eligible; `trial_expired` ∈ `TRIAL_BLOCKED_STATUSES`.
2. **`POST /billing/meta-free-start`.** Personal-only · not `is_trial_blocked` · not active-sub → create `trial_local` row (one per `owner_id`) → next `/container/status` poll provisions the meta container.
3. **`$3/day` reserve + block in `gate_chat`.** Atomic `ADD` today's worst-case reserve (`input_est×$1.25/M + maxTokens×$4.25/M`, ≤~$0.39) to `meta_spend.<today>`; block when today's `reserved+spent ≥ $3` **or** `now > trial_end`; **deny on missing/unreadable** (fail-closed). Structured `{blocked, code:"trial_daily_limit"/"trial_expired", message}`.
4. **Post-turn reconcile.** `_safe_record_usage` → actual Meta $ (WS2.4) → replace today's reservation with actual; **never refund on reconcile failure** (keep worst-case).
5. **Always-on entitlement (the ~4 runtime edits).** `has_always_on_entitlement` predicate + `hooks_proxy` 402 + `gate_chat` autonomous-session-key refusal + `teams/work.py` routine-enable rejection — all for non-entitled owners. (Channels handled by the WS6 reaper.)
6. **Guards.** `change_provider` rejects target `meta`; refuse `owner_id.startswith("org_")`.
7. **Global kill-switch.** A daily aggregate spend value checked in `gate_chat` vs. `META_FREE_DAILY_GLOBAL_SPEND_CAP` — bounds the shared key's blast radius.

**Testable deliverable:** integration tests — `trial_local` owner blocked `trial_daily_limit` at $3 today, unblocked next day; blocked `trial_expired` after 3d; parallel sends can't exceed $3/day (atomic); missing counter denies; a webhook to a trial owner → 402; a `paperclip:`/`cron:` run refused. **Depends on:** WS2.

---

## WS4 — Onboarding flow (frontend web)

**Objective:** Signup → **instant chat on the free trial**, with the card and provider steps removed from the free path; a clear "start free" entry; funnel instrumented.

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — free-tier path skips payment + provider phases; drives `POST /billing/meta-free-start` then polls to ready
- Modify: `apps/frontend/src/hooks/useProvisioningState.ts` — recognize `trial_local` as an active/allowed state (200), map its phases
- Modify: `apps/frontend/src/components/chat/*` onboarding entry — a "Start free — no card" CTA calling `meta-free-start`
- Modify: PostHog instrumentation (per memory `project_onboarding_funnel_posthog_blindspot`): `trial_started`, `trial_first_agent_response`, `trial_first_successful_task`, `trial_message_sent`
- Test: `apps/frontend` vitest (single-file only per memory `feedback_frontend_vitest_ram_heavy`) + `tsc --noEmit`

**Tasks:**
1. **Free-start entry.** New-user landing offers "Start free (no card)" → `POST /billing/meta-free-start` → begins provisioning poll. (Existing "subscribe" path remains for users who want to pay immediately.)
2. **Stepper free path.** For `trial_local`, `STEPS` = provision → gateway → ready (no payment, no provider). Reuse the existing cold-start pod/stepper UI.
3. **Status state machine.** `useProvisioningState` treats `trial_local` as allowed (renders chat when ready), not "blocked".
4. **Funnel events.** Emit the trial events above so we can tune the cap against first-successful-task.

**Testable deliverable:** E2E (dev) — a fresh Clerk signup reaches a streaming Friday reply **without** ever seeing a card or provider screen. **Depends on:** WS3 (the `meta-free-start` endpoint + `trial_local` state). **Risk:** don't regress the existing paid onboarding path — gate the new flow on the free entry only.

---

## WS5 — Free → paid conversion + provider switch

**Objective:** A trial user who hits the $3/3-day wall can add a card, pick one of the three providers, and their container switches off Meta onto the chosen provider — reusing existing machinery.

**Files:**
- Modify: `apps/frontend/src/components/chat/*` — the cap-reached / expiry **paywall** state (rendered from the `trial_daily_limit`/`trial_expired` gate codes) → "add card + choose provider"
- Modify: `apps/backend/routers/billing.py` / `core/services/billing_service.py` — a convert path: paid Checkout (`create_flat_fee_checkout`, `trial_days=0`) → on success flip `trial_local` → active + set `provider_choice`
- Reuse (no change): `change_provider` → `_hot_swap_provider` (Bedrock↔ChatGPT hot-patch) / cold reprovision (BYO / into-Bedrock)
- Modify: the free→paid transition must **swap the container off Meta**: on conversion, re-resolve provider and re-emit config (Meta → chosen provider) via the existing config-patch/reprovision path
- Test: backend unit + a dev E2E convert

**Tasks:**
1. **Paywall UI.** The `trial_daily_limit`/`trial_expired` block renders an upgrade card (not a dead spinner) — CTA to Checkout + provider picker.
2. **Convert endpoint.** On paid Checkout success: `subscription_status → active`, set `provider_choice` (the three), clear the trial fields.
3. **Provider swap off Meta.** After conversion, resolve the new `provider_choice` and apply: Bedrock↔ChatGPT via `change_provider` hot-swap; BYO/into-Bedrock via cold reprovision. Confirm the container drops the Meta provider + `MODEL_API_KEY` (task-def secret detaches on the paid task def).
4. **Guard.** A converted user can never re-enter the free tier (`is_trial_blocked` now true).

**Testable deliverable:** dev E2E — a capped trial user adds a card, picks ChatGPT-OAuth, and the next chat runs on ChatGPT (not Meta), with billing active. **Depends on:** WS3 (+ WS2 for the provider machinery). **Note:** ChatGPT-OAuth is org-enabled but owner-gated; BYO/Bedrock switch is a cold reprovision (preserves EFS).

---

## WS6 — Free-tier scale-to-zero, EVENT-DRIVEN (backend) — *parallel track*

**Objective:** A trial container sleeps on the last-disconnect *event* and wakes on next chat — **no periodic sweeper** — and is never killed mid-run.

**Files:**
- Modify: `core/gateway/connection_pool.py` — extend `_grace_close` (`:2002`) to `stop_user_service` for free-tier after an idle grace; add a per-owner frontend-ping deadline (Layer 3)
- Modify: `routers/websocket_chat.py` — the frontend `ping` handler (`:632`) resets the deadline + chat-to-stopped wake (`GATEWAY_WAKING` + `start_user_service`)
- Modify: `main.py` lifespan — a **one-time boot reconcile** (Layer 2) next to `_resume_provisioning_transitions` (`:366`); fill the no-op `startup_containers()`
- Modify: `core/services/connection_service.py` — write/refresh `ttl` on `store_connection` (`:82`, currently never written — the latent dead-TTL bug)

**Tasks (event layers — NO periodic sweep):**
1. **Layer 1 — sleep on last disconnect.** Extend `_grace_close` (fires when the owner's last frontend WS disconnects): after `close_user`, if free-tier **and** not `has_active_runs`/`_parked` **and** not an autonomy owner → `stop_user_service`. Use a longer idle grace for ECS teardown (~2–5 min) than the 30 s socket-close; a reconnect cancels it.
2. **Layer 2 — backend-restart recovery.** One-time boot reconcile: `get_by_status("running")` → free-tier → `query_by_owner` (GSI) → zero live chat conns → arm teardown. Fired by the boot event, **once — not periodic.**
3. **Layer 3 — lost-`$disconnect` recovery.** A per-owner deadline reset by the existing frontend `ping` (`:632`); if no ping within the window → `remove_frontend_connection` → flows into Layer 1. Also write/refresh the ws-connections `ttl` on that ping so Layer 2's GSI read is honest across restarts.
4. **Wake-on-chat.** A send to a `stopped` container → `start_user_service` + `GATEWAY_WAKING`; frontend polls `/status`, auto-retries at `ready`.
5. **Reaper-safety = the entitlement.** Because trial containers have **no** always-on features (WS3), there is no autonomous work to kill → `trial_local` is treated as **non-autonomy** and is sleep-eligible; the mid-run guard (`has_active_runs`/`_parked`) still protects an in-flight turn. (This also closes the channels gap: a slept trial container can't hold a 24/7 channel socket.)

**Testable deliverable:** unit tests — sleeps only on last-disconnect + idle + no-active-run; never sleeps a paid/always-on owner; boot reconcile sleeps a leaked idle container; ping-deadline covers a lost disconnect. Dev test: idle trial container reaches `stopped`, wakes on next chat. **Honest residual:** a tab that keeps pinging while the human walked away stays awake (human-inactivity is a separate axis). **Depends on:** the WS3 entitlement makes the sleep safe; otherwise provider-agnostic.

---

## WS7 — Client surfaces: web + desktop cold-start handling — *parallel track*

**Objective:** Web and desktop gracefully handle a container that spun down under them, and desktop stops falsely pinning the container "active".

**Files:**
- Modify: `packages/shared/src/gateway/useGatewayConnection.ts` — extend reconnect budget to outlast cold start (≥5 min), or auto-retry on a slow cadence instead of stopping
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — un-stick the `reachedReady` latch so an in-session spin-down re-arms the cold-start UI
- Modify: `apps/frontend/src/hooks/useGatewayRpc.ts` / read surfaces — make reads wake-aware (trigger a `/status` refresh instead of spinning)
- Modify: `apps/backend/routers/websocket_chat.py` (`:387-394`) — **exclude `connection_type in {node, operator}` from `add_frontend_connection`/the idle-presence signal** (desktop node-host must not pin the owner active)
- Modify: desktop wake — either give the node `$connect` path a wake, or have the desktop webview drive `POST /container/provision`

**Tasks:**
1. **Web reconnect + stepper.** Reconnect outlasts cold start; stepper re-arms on mid-session drop.
2. **Web reads wake-aware.** Panel reads against a stopped container trigger a wake, not an infinite spinner.
3. **Desktop presence fix.** Node/operator connections no longer count as owner presence (so the reaper can fire for desktop users); add a desktop wake path.

**Testable deliverable:** web — a mid-session spin-down shows the cold-start UI and recovers on wake; desktop — a tray-resident app no longer blocks the reaper. **Depends on:** WS6. **Risk:** iOS is **deferred** (needs an Expo push path); free-tier mobile lands on the always-on Teams surface until then.

---

## Cross-cutting: telemetry & go-live gates

- **Telemetry:** `trial_started`, `trial_first_agent_response`, `trial_first_successful_task`, `trial_cap_reached`, `trial_converted`, `trial_lapsed`; server-side `trial_daily_limit`/`trial_expired` block metric (client-independent). Tune the $3/3-day values against first-successful-task once data lands.
- **Go-live gates (block launch, not build):** (1) human ToS review of Meta's Model-API terms; (2) Meta billing card added; (3) WS2 Step-0 reconciliation verified green; (4) prod secret `isol8/prod/meta-model-api-key` stored; (5) the **parallel 7.1 base bump** landed in dev+prod with its config-schema **6.10→7.1 audit against our `config.py`** passing (a changed/removed key crash-loops the container).
- **Parallel dependency (not ours to build, but WS1 blocks on it):** another effort bumps the base to 7.1 (native-meta host-compat) — owns the fork-patch rebase, the config-schema 6.10→7.1 audit, the Codex app-server 0.143+ break, and the paid-fleet image-swap (protocol unchanged).

---

## Self-review (against the spec)

- **Spec coverage:** provider (WS1/WS2), $3 cap + trial lifecycle (WS3), no-card onboarding (WS4), free→paid switch (WS5), spin-down (WS6), surfaces (WS7), terms/telemetry (cross-cutting). ✅ every spec section maps to a workstream.
- **Consistency:** `MODEL_API_KEY` / `meta/muse-spark-1.1` / **`$3/day`** (300000 microcents/day) / `trial_local`/`trial_expired` / **cap on the `billing_accounts` daily field** / **`has_always_on_entitlement`** / `trial_daily_limit` used identically across WS1–WS7 and the Global Constraints.
- **Decomposition:** each workstream produces an independently testable deliverable; the per-task bite-sized TDD steps are authored per-workstream at execution (W2 sub-plan is the pattern).
- **Deferred, explicitly:** canonical-email anti-farming; iOS surface; the always-on **reconciler + platform-wide hardening** (v1 uses the runtime chokepoints only); `usage-counters` fix-or-delete cleanup — all noted, none block v1. The **7.1 base bump is a parallel dependency**, not deferred.
