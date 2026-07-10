# No-Card Free Tier — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each workstream's sub-plan task-by-task. This master plan sequences the workstreams; each workstream (WS1–WS7) gets a detailed, bite-sized, TDD sub-plan authored just-in-time at execution. `docs/superpowers/specs/2026-07-10-W2-meta-provider-wiring-plan.md` is the pattern for that depth.

**Goal:** Let anyone sign up and reach a working agent **instantly — no credit card, no LLM-provider config** — running on Meta Muse Spark, with the cost of a non-converting trial bounded to roughly **$1** (container spins down when idle + a hard **$3** inference cap over a **3-day** window); then convert them to one of the three paid providers (Bedrock-managed / ChatGPT-OAuth / BYO key).

**Architecture:** Free-tier containers run OpenClaw's native **`meta` provider** (`meta/muse-spark-1.1`), delivered as a **vendored plugin baked into the extended image on the current 6.10 base** (no OpenClaw base bump). A new free-tier mode is threaded through the existing config generator and provision path. The **$3 hard cap** lives in the existing `usage-counters` table (atomic reserve, fail-closed, reconciled from actual Meta usage); the **3-day clock** lives on the existing `billing_accounts` row (`trial_local` status + `trial_end`). Free-tier containers **scale to zero** when idle (a rebuilt, trial-scoped idle reaper) and wake on next chat. Onboarding drops the card+provider walls for the free path; conversion reuses the existing `change_provider` hot-swap. **Paid is untouched; always-on autonomy stays a paid feature.**

**Tech Stack:** FastAPI + DynamoDB (backend), AWS CDK + Docker (infra/image), Next.js 16 (frontend web), Tauri/Rust (desktop), Meta Model API (`api.meta.ai/v1/responses`, `openai-responses` transport), Stripe (conversion only), CloudWatch/PostHog (telemetry).

## Global Constraints

- **OpenClaw base image stays `2026.6.10-isol8.2`** — meta arrives as a vendored plugin, NOT a base bump. Do not bump `openclaw-version.json#upstream` or the Dockerfile `FROM`.
- **Provider identity:** provider id `meta`; model `meta/muse-spark-1.1`; base `https://api.meta.ai/v1` (`openai-responses` transport); key env var **`MODEL_API_KEY`**; free-tier reasoning effort **`medium`**; per-turn clamps `contextTokens: 200000`, `maxTokens: 32000`.
- **Pricing:** Meta **$1.25 / M input · $4.25 / M output** (reasoning tokens billed as output). Cap = **$3.00 total** (300000 microcents) per trial. Window = **3 days**.
- **Cap on spend, reserve-worst-case → reconcile.** Enforce in `gate_chat` **before** send with an atomic reserve; reconcile to actual Meta `usage` on `chat.final`. **Fail CLOSED** (deny on missing/unreadable counter) — the one inversion vs. `metronome_gate`.
- **No new DynamoDB table.** $3 spend → existing `usage-counters`; clock/status → existing `billing_accounts`; churn → existing `is_trial_blocked`.
- **Not Metronome, not Stripe-per-signup.** Meta records spend 0 to Metronome by construction; Stripe is touched only at paid conversion.
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

## WS1 — Meta provider baked into the extended dev image

**Objective:** A fresh dev container boots with provider `meta` available and `meta/muse-spark-1.1` selectable, on the current 6.10 base.

**Files:**
- Create: `apps/infra/openclaw/vendor/meta/**` (vendored copy of `extensions/meta/` from `origin/release/2026.7.1`)
- Modify: `apps/infra/openclaw/Dockerfile` (new build layer, near the Slack/Discord bake at `:249`)
- Modify: `openclaw-version.json` (`dev.tag` only, after the image builds)
- Modify: `apps/backend/core/containers/config.py` (`build_provider_block`, `:311-482`) — the `meta` provider branch (WS1 does the config *emission*; WS2 threads the *selection*)

**Tasks:**
1. **Vendor the plugin.** `git -C ~/Desktop/openclaw.nosync archive origin/release/2026.7.1 extensions/meta | tar -x -C apps/infra/openclaw/vendor/meta --strip-components=1`. Commit the vendored source (record the source SHA in a `VENDORED_FROM` file). Deliverable: source present + builds standalone (`npm ci && npm run build` inside it).
2. **Dockerfile bake layer.** Add a layer that copies `vendor/meta`, runs its build, and places output in `/app/dist/extensions/meta` with the same ownership/pattern as the existing plugin bakes. Verify the plugin's `openclaw.plugin.json` (`id: "meta"`, `providers: ["meta"]`) lands in the discovery dir.
3. **`config.py` meta emission.** In `build_provider_block`, add the `meta` branch returning `(providers_config, default_model, plugin_entries)`: enable the `meta` plugin entry, `default_model = {primary: "meta/muse-spark-1.1", fallbacks: ["meta/muse-spark-1.1"]}`, reasoning effort `medium`, `contextTokens: 200000`, `maxTokens: 32000`; no AIP ARNs. (Selection wiring is WS2.)
4. **Build + validate on dev.** Rebuild the extended image via `build-openclaw-image.yml` (capture the `<upstream>-<sha>` tag), verify it in ECR, then flip `openclaw-version.json#dev.tag`. Boot a throwaway dev container with a hand-set `meta` config and confirm: no crash-loop, `openclaw.json` validates, provider `meta` registers, a `Playground`-style single completion returns.

**Testable deliverable:** a dev container whose gateway is healthy and lists `meta/muse-spark-1.1` as an available model.

**Depends on:** nothing (key/card not required to build). **Risk:** the plugin may assume a 7.1 runtime API — validate boot on 6.10 (the `openai-responses` adapter + standalone-provider mechanism both exist ≥6.9). If it fails to load, fall back to the generic OpenAI-compatible custom-provider block (documented in the design spec §A Path B).

---

## WS2 — Free-tier provider wiring + reconciliation verification

**Sub-plan already written:** `docs/superpowers/specs/2026-07-10-W2-meta-provider-wiring-plan.md`. Summary of its tasks:

**Objective:** A free-tier owner provisions a container that runs `meta`, and actual Meta token usage is captured into Isol8 accounting (the load-bearing check for the $3 cap).

**Files:** `apps/infra/lib/stacks/auth-stack.ts` + `service-stack.ts`/`container-stack.ts` (secret `fromSecretNameV2` + conditional task-def injection), `apps/backend/core/containers/config.py`, `core/services/container_provision.py` (`resolve_provider_choice`), `core/services/usage_service.py` (Meta $ computation).

**Tasks (ordered; Step 0 first):**
1. **Step 0 — reconciliation verification (GATE).** With the dev image + `MODEL_API_KEY`: provision a dev container in `meta` mode, run several Muse Spark turns (small + large + reasoning), and confirm what `sessions.list` / `_safe_record_usage` records vs. Meta's reported `usage`. **Decision gate:** actual tokens flow → proceed; else fix (container-side usage hook) or fall back to realistic-estimate reservation. Write a one-page verdict.
2. **Secret injection (CDK).** Reference `isol8/<env>/meta-model-api-key` via `fromSecretNameV2`; inject `MODEL_API_KEY` into the task def **only for free-tier** containers.
3. **Free-tier selection.** `resolve_provider_choice` returns `meta` for a trial owner (reads the trial flag from WS3, not `billing_accounts.provider_choice`); thread through `background_provision → provision_user_container → write_user_configs → write_openclaw_config → build_openclaw_config_dict`.
4. **Meta spend computation.** In `record_usage`, add the `meta/` cost path: `spend = input_tokens × $1.25/M + output_tokens × $4.25/M` from reconciled usage → the WS3 spend counter (not Metronome).

**Testable deliverable:** a free-tier dev container streams a Muse Spark reply and its real spend lands in `usage-counters`. **Depends on:** WS1 + the stored key.

---

## WS3 — Trial lifecycle + $3 hard boundary (backend)

**Objective:** A no-card owner can provision a trial, chat on Meta, and is hard-stopped at $3 spent **or** 3 days elapsed — fail-closed, atomic, reusing existing tables.

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py` (`_ACTIVE_STATUSES`, `TRIAL_BLOCKED_STATUSES`, gate eval) — add `trial_local`/`trial_expired`; new `evaluate_meta_free_gate`
- Modify: `apps/backend/core/repositories/billing_repo.py` — `trial_local` status + `trial_end` field; lightweight row creation
- Modify: `apps/backend/core/repositories/usage_repo.py` — `increment_reserved`/`reconcile_spend` on a fixed `meta_trial` key (atomic `ADD … RETURN ALL_NEW`, model on `:49-63`)
- Modify: `apps/backend/core/gateway/connection_pool.py` (`gate_chat`, `:1786-1847`) — the Meta branch: reserve → block at $3/expiry (fail-closed) → global kill-switch
- Modify: `apps/backend/routers/websocket_chat.py` (`_safe_record_usage`, `:847`) — post-turn reconcile
- Create: `apps/backend/routers/billing.py` → `POST /billing/meta-free-start`
- Modify: `apps/backend/core/services/billing_service.py` (`change_provider`) — reject target `meta`
- Test: `apps/backend/tests/unit/**` per file

**Tasks (each = TDD cycle):**
1. **Trial state on the billing row.** `create_trial_local(owner_id)` writes `subscription_status="trial_local"`, `trial_end = now + 3d`. `provision_gate`: `trial_local` is provision-eligible; `trial_expired` ∈ `TRIAL_BLOCKED_STATUSES` (churn reuse).
2. **`POST /billing/meta-free-start`.** Personal-only, not `is_trial_blocked`, not active-sub → create the `trial_local` row (one per `owner_id`) → the next `/container/status` poll provisions the meta container.
3. **Atomic spend reserve + block in `gate_chat`.** Meta branch: `increment_reserved(owner_id, worst_case_microcents)` (worst case = `contextTokens×$1.25/M + maxTokens×$4.25/M`), block when `reserved+spent ≥ 300000` **or** `now > trial_end`; **deny on missing/unreadable row** (fail-closed). Emits a structured `{blocked, code:"trial_limit"/"trial_expired", message}` the client already renders.
4. **Global kill-switch.** A daily aggregate spend row checked in the same branch vs. `META_FREE_DAILY_GLOBAL_SPEND_CAP`.
5. **Post-turn reconcile.** `_safe_record_usage` → compute actual Meta $ (WS2.4) → replace the reservation with actual (`reconcile_spend`).
6. **Guards.** `change_provider` rejects target `meta`; `gate_chat` refuses Meta if `is_subscription_active`/`is_trial_blocked`; refuse `owner_id.startswith("org_")`.

**Testable deliverable:** integration test — a `trial_local` owner sends messages until $3 reserved → next send blocked `trial_limit`; a clock-advanced owner blocked `trial_expired`; parallel sends can't exceed $3 (atomic reserve); missing counter denies. **Depends on:** WS2.

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
- Modify: `apps/frontend/src/components/chat/*` — the cap-reached / expiry **paywall** state (rendered from the `trial_limit`/`trial_expired` gate codes) → "add card + choose provider"
- Modify: `apps/backend/routers/billing.py` / `core/services/billing_service.py` — a convert path: paid Checkout (`create_flat_fee_checkout`, `trial_days=0`) → on success flip `trial_local` → active + set `provider_choice`
- Reuse (no change): `change_provider` → `_hot_swap_provider` (Bedrock↔ChatGPT hot-patch) / cold reprovision (BYO / into-Bedrock)
- Modify: the free→paid transition must **swap the container off Meta**: on conversion, re-resolve provider and re-emit config (Meta → chosen provider) via the existing config-patch/reprovision path
- Test: backend unit + a dev E2E convert

**Tasks:**
1. **Paywall UI.** The `trial_limit`/`trial_expired` block renders an upgrade card (not a dead spinner) — CTA to Checkout + provider picker.
2. **Convert endpoint.** On paid Checkout success: `subscription_status → active`, set `provider_choice` (the three), clear the trial fields.
3. **Provider swap off Meta.** After conversion, resolve the new `provider_choice` and apply: Bedrock↔ChatGPT via `change_provider` hot-swap; BYO/into-Bedrock via cold reprovision. Confirm the container drops the Meta provider + `MODEL_API_KEY` (task-def secret detaches on the paid task def).
4. **Guard.** A converted user can never re-enter the free tier (`is_trial_blocked` now true).

**Testable deliverable:** dev E2E — a capped trial user adds a card, picks ChatGPT-OAuth, and the next chat runs on ChatGPT (not Meta), with billing active. **Depends on:** WS3 (+ WS2 for the provider machinery). **Note:** ChatGPT-OAuth is org-enabled but owner-gated; BYO/Bedrock switch is a cold reprovision (preserves EFS).

---

## WS6 — Free-tier container spin-down (backend) — *parallel track*

**Objective:** A trial container scales to zero when idle and wakes on next chat, without ever killing a mid-run container.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` — connection-level `last_event_at` bumped unconditionally in `_handle_message` (`:1088`) + a pool accessor
- Modify: `apps/backend/core/repositories/container_repo.py` — `last_active_at` field
- Modify: `apps/backend/routers/websocket_chat.py` — throttled `last_active_at` write in the chat dispatcher (`:1962`) + on `$connect` (`:394`)
- Create: `apps/backend/core/services/idle_reaper.py` — the trial-scoped reaper predicate + pre-stop RPC probe
- Modify: `apps/backend/main.py` — schedule `_free_tier_idle_reaper_loop` in lifespan (mirror `_running_count_gauge_loop`, `:239/:379`) + register in `BACKGROUND_TASKS`
- Modify: `apps/backend/routers/websocket_chat.py` — chat-to-stopped-container wake (`GATEWAY_WAKING` frame + `start_user_service`)

**Tasks:**
1. **Activity tracking.** Add `last_active_at` (throttled) + connection `last_event_at` (unconditional).
2. **Reaper loop.** Stop iff: `trialing`/`trial_local` · `status==running` · uptime ≥ 15 min · no open chat frontends · not `has_active_runs`/`_parked` · `last_event_at`/`last_active_at` older than 45 min · pre-stop probe clean · **not** an autonomy owner (cron/channels configured). Action: `stop_user_service`.
3. **Wake-on-chat.** A send to a `stopped` container kicks `start_user_service` + returns `GATEWAY_WAKING`; frontend polls `/status` and auto-retries the send at `ready`.

**Testable deliverable:** unit tests for the predicate (never reaps mid-run / autonomy owner / paid) + a dev test that an idle trial container reaches `stopped` and wakes on next chat. **Depends on:** none (provider-agnostic). **Risk:** the mid-run safety hole — the `last_event_at` + probe + fail-safe (don't stop if unreachable) are mandatory.

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

- **Telemetry:** `trial_started`, `trial_first_agent_response`, `trial_first_successful_task`, `trial_cap_reached`, `trial_converted`, `trial_lapsed`; server-side `trial_limit`/`trial_expired` block metric (client-independent). Tune the $3/3-day values against first-successful-task once data lands.
- **Go-live gates (block launch, not build):** (1) human ToS review of Meta's Model-API terms; (2) Meta billing card added; (3) WS2 Step-0 reconciliation verified green; (4) prod secret `isol8/prod/meta-model-api-key` stored.

---

## Self-review (against the spec)

- **Spec coverage:** provider (WS1/WS2), $3 cap + trial lifecycle (WS3), no-card onboarding (WS4), free→paid switch (WS5), spin-down (WS6), surfaces (WS7), terms/telemetry (cross-cutting). ✅ every spec section maps to a workstream.
- **Consistency:** `MODEL_API_KEY` / `meta/muse-spark-1.1` / `$3` (300000 microcents) / `trial_local` / `trial_expired` used identically across WS1–WS7 and the Global Constraints.
- **Decomposition:** each workstream produces an independently testable deliverable; the per-task bite-sized TDD steps are authored per-workstream at execution (W2 sub-plan is the pattern).
- **Deferred, explicitly:** canonical-email anti-farming; iOS surface; full 7.1 base bump — all noted, none block v1.
