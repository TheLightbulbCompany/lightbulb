# No-Card Free Trial — Design

**Date:** 2026-07-09 (rev. 2026-07-10 after cross-subsystem verification)
**Status:** Draft for review
**Author:** brainstormed with Claude

## Problem

Conversion is weak, and the funnel data says the leak is **upstream of payment**
(memory `project_onboarding_funnel_posthog_blindspot`): ~17 signups → 6 live
containers → ~5 ever chatted. A container is only provisioned *after* the gate
passes (card + provider), so most users never reach aha. We ask people to pay and
configure LLM plumbing to find out whether it's worth paying for.

## Goal

Let anyone sign up and reach a working agent immediately — **no card, no provider
config** — while (a) keeping the cost of a non-converting trial bounded to cents,
and (b) making it impossible to abuse as free inference for our paying users.

Two walls, attacked together:
1. **Payment/always-on wall** → free tier gets a container that **spins up/down**
   per session; 24/7 autonomy (channels, webhooks, cron, always-on) becomes a
   **paid feature**.
2. **Inference wall** → free tier runs on **Meta Muse Spark 1.1** via a separate,
   walled-off provider (not Bedrock, not metered), on a **shared Meta key**.

Shape: **3-day window + $3 total spend cap** (whichever first), one trial per
verified identity, then the existing three provider options (Bedrock / ChatGPT
OAuth / BYO) are shown.

### Cap: $3 total spend, reserve-worst-case → reconcile (token/dollar based)

The cap is **dollars of Meta spend**, not message count — a giant-context message
costs far more than a small one, so message count is the wrong unit. Enforced the
way Anthropic/OpenAI enforce token limits despite not knowing output in advance:

- **Worst case is knowable pre-call.** Input tokens are deterministic before
  inference; output is bounded by `maxTokens: 32000`. So a turn's worst-case cost =
  `input × $1.25/M + 32K × $4.25/M`, clamped by the config `contextTokens: 200000`
  → **≤ ~$0.386/turn**.
- **Pre-send (in `gate_chat`):** atomically reserve the worst-case dollar cost
  against the trial's remaining `$3 − spent`. If it won't fit, **block (fail
  closed)**. This is the reservable, abuse-proof gate (closes the parallel-fan-out
  TOCTOU — no server concurrency gate exists).
- **Post-turn (on `chat.final`):** reconcile the reservation to **actual** dollars
  computed from Meta's reported `usage` (`prompt_tokens`, `completion_tokens`) ×
  known pricing; refund the unused output allowance. The durable counter then
  tracks true spend.

**Reconciliation is load-bearing (~20× swing).** With actual-usage reconciliation a
typical ~$0.02–0.05 turn means **$3 ≈ 60–150 turns**; without it (worst-case
reservation only) **$3 ≈ 8 turns**. So "Meta `usage` flows into Isol8's accounting"
is a **hard prerequisite**, verified empirically once the key is wired (one Muse
Spark turn, diff reported usage vs. recorded). Until verified, the config per-turn
clamp (200K/32K) is the safety floor.

**Reasoning effort = cost lever.** Muse Spark defaults to `high` and bills thinking
tokens as output ($4.25/M). Set free-tier to **`medium`** to stretch the $3 and cut
latency; the $3 ceiling bounds it regardless. Tunable.

### Non-goals
- Not permanent freemium (bounded trial, converts or lapses).
- Not changing paid (always-on autonomy is what paid buys).
- Not paid scale-to-zero (separate future project — see end).

---

## Verified findings (workflow `ws9pabvsr`, 7-agent investigation, 2026-07-10)

### A. Meta provider — native exists upstream, NOT in our pinned image

`build_provider_block` (`config.py`) currently supports exactly three
`provider_choice` values (`bedrock_claude`, `chatgpt_oauth`, `byo_key`;
`billing_repo.py:228`). No `meta`/`muse` string exists anywhere in the fork
(`~/Desktop/agent`), the deployed build branch (`isol8/2026.6.1-mcp-storm` =
pinned `2026.6.1-isol8.1`), or vanilla upstream. OpenClaw's frozen provider set
`BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS` (`zod-schema.core.ts:426`) has ~60 ids, no
`meta`.

**Reconciliation:** Muse Spark launched **2026-07-09**, so a native `meta`
provider *cannot* be in our June image. The `docs.openclaw.ai/providers/meta` page
reflects OpenClaw **latest**. Both observations are correct — it's version skew.

**DECISION (2026-07-10): Path A — bump the image for the native `meta` provider.**
Requires identifying the OpenClaw release that ships native `meta` and bumping
`openclaw-version.json` + Dockerfile `FROM` to it (dev → validate → prod), with the
usual protocol audit + fleet reprovision. Native config uses provider id `meta`,
model `meta/muse-spark-1.1`, key env var `MODEL_API_KEY`. Path B (below) is retained
as the fallback if the bump slips.

**Two integration paths:**
- **Path A (native):** bump the OpenClaw base image to the release shipping native
  `meta`, then config is trivial (`provider: meta`, model `meta/muse-spark-1.1`,
  key via `MODEL_API_KEY`). Cost: an OpenClaw version bump = protocol audit +
  full-fleet reprovision + validation (memories
  `feedback_audit_openclaw_protocol_version_on_upgrade`,
  `feedback_dockerfile_from_openclaw_version_drift`,
  `project_openclaw_version_ssot_and_deploy_topology`).
- **Path B (custom provider, ships on current image — RECOMMENDED for v1):** add a
  generic OpenAI-compatible custom provider to the generated `openclaw.json`. The
  schema requires a non-built-in id to declare `baseUrl` + `models`
  (`zod-schema.core.ts:511-517`):

```json5
{ models: { mode: "merge", providers: { meta: {
  baseUrl: "https://api.meta.ai/v1",
  api: "openai-completions",
  auth: "api-key",
  apiKey: { source: "env", id: "META_MODEL_API_KEY" },
  timeoutSeconds: 300,
  models: [ { id: "muse-spark-1.1", name: "Muse Spark 1.1", reasoning: true,
    input: ["text","image"], cost: { input: 1.25, output: 4.25 },
    contextWindow: 1048576, contextTokens: 200000, maxTokens: 32000 } ],
} } },
  agents: { defaults: { model: { primary: "meta/muse-spark-1.1",
    fallbacks: ["meta/muse-spark-1.1"] } } } }
```

**Key injection (both paths):** the shared Meta key is an **ECS task-definition
secret** (`META_MODEL_API_KEY` from Secrets Manager), attached only to free-tier
containers. Never written to `openclaw.json` on EFS (plaintext). OpenClaw's secret
resolver reads it from the environment at runtime.

**New `provider_choice` = `"meta"` branch** threads through
`build_provider_block → build_openclaw_config_dict → write_openclaw_config →
write_user_configs → provision_user_container`. Emits the block above + the model
default; no AIP ARNs (that's Bedrock-only).

### A′. Meta Model API terms — a human go/no-go gate

Meta's API ToS could not be confirmed by tooling (JS SPA). Surfaced (verify
in-browser at `ai.developer.meta.com/legal/terms-of-service` before go-live):
- *"Meta will not use your Content to train any artificial intelligence models."*
  — favorable, **but**
- **30-day retention** + an **aggregate/anonymized "train and improve" carve-out**,
  **no zero-data-retention option**, US-only public preview, undisclosed rate
  limits.
- Model API specifics: base `https://api.meta.ai/v1`, `Authorization: Bearer`,
  model `muse-spark-1.1`, **$1.25/M in · $4.25/M out** (reasoning tokens billed as
  output), $20 one-time free credits, 1M context, deliberately kept off OpenRouter.

**Gate:** since we'd relay data we don't own (free users'), a human must confirm
the no-train clause + retention govern the Model API before shipping.

### B. Billing / Metronome abuse boundary

**The wall is structurally free:** a `meta/…` model is non-Bedrock, so
`record_usage` records **spend 0** and never reaches Metronome/Stripe
(`usage_service.py:99-118`). The gap is there's no COUNT ceiling on that path.

**Two inversions vs. existing paid patterns (must implement explicitly):**
1. **Fail CLOSED.** `metronome_gate` treats a missing row as *allowed*
   (`metronome_gate.py:58-60`) — safe for paid (IAM-revoke backstop), **wrong for
   free**. The free counter must deny on a missing/unreadable row.
2. **Key on identity, not `owner_id`.** Farming defeats `owner_id` (new account =
   new owner). The one-trial claim keys on **canonicalized verified email**.

**New table `isol8-{env}-free-trial-claims`** (model on `MetronomeFastGateTable`;
grant via conditional `PutItem attribute_not_exists`, **no TTL**):
- PK `identity_key` (lowercased, `+tag`-stripped, gmail-dot-stripped verified
  email), `claimed_by_owner_id`, `claimed_at`, `trial_expires_at` (+3d),
  `spent_microcents` (atomic ADD — reconciled to actual Meta spend),
  `reserved_microcents` (transient in-flight reservations), `input_tokens` /
  `output_tokens` (observability), `status` ∈
  {active, exhausted, expired, converted}. Cap = **$3 total** (300000 microcents).
- Plus a global `free-tier:global:{YYYY-MM-DD}` spend row for the shared-key
  kill-switch.

Do **not** put free state on `billing_accounts` (a pre-sub free user has no billing
row; `is_trial_blocked` returns false for missing rows —
`provision_gate.py:97-98`). A read-only `meta_trial_converted` mirror flag on the
billing row is fine as a fast-read cache only.

**Abuse-defense checklist (enforcement point → vector):**

| # | Control | Point |
|---|---------|-------|
| 1 | Meta free-eligibility gate (verified email · not trial-blocked · not active-sub · personal-only · claim live) | new branch in `provision_gate` + pre-forward check in `gate_chat` (`connection_pool.py:1786`) |
| 2 | One-trial-per-identity (conditional PutItem, no TTL) | new `free_trial_repo.claim_if_not_exists` (model on `webhook_dedup.py:84`); called from new `POST /billing/meta-free-start` |
| 3 | Verified + canonicalized email (don't trust JWT `email`) | `clerk_admin` re-check + `identity.canonicalize_email` in claim path |
| 4 | Disposable-domain denylist (config-driven; exempt `mailsac.com`) | claim path |
| 5 | Post-turn **spend reconciliation** to actual Meta `usage` × pricing | hook `_safe_record_usage` (`websocket_chat.py:847`) → `free_trial_repo.reconcile_spend` |
| 6 | **$3 spend / expiry hard-stop, fail-closed** | Meta branch in `gate_chat` |
| 7 | **Atomic worst-case reserve** at gate (closes parallel-fan-out TOCTOU) | `gate_chat` Meta branch, `ADD reserved_microcents … return_new` like `usage_repo.py:49` |
| 8 | Global daily **spend** kill-switch on shared key | `gate_chat` reads global spend row vs `META_FREE_DAILY_GLOBAL_SPEND_CAP` |
| 9 | Churned-customer block | Meta gate consults `is_trial_blocked` (includes `canceled`) |
| 10 | Paid can't downgrade to free | `change_provider` rejects target `meta`; `gate_chat` refuses Meta if `is_subscription_active`/`is_trial_blocked` |
| 11 | Org exclusion (personal-only) | Meta gate refuses `owner_id.startswith("org_")` |
| 12 | Separate count metric, no Stripe/Metronome | `put_metric("free_tier.meta.message")`; spend 0 by construction |

Meta is an **eligibility axis, never a selectable `provider_choice`** — keep the
paid set at three; free routing is reached only via the dedicated free-start path.

### C. Container spin-up / spin-down

- Primitives intact: `stop_user_service` (desiredCount=0) / `start_user_service`
  (desiredCount=1); reaper **deleted** at flat-fee cutover (`main.py:239`). Backend
  is **single-instance**, so one asyncio reaper loop sees the whole fleet's live
  connections (flag: if backend scales out, needs a DDB lease).
- **Cold start is expensive:** ~12 min worst case today (`sidecars.channels`
  wedge, `container.py:36`), warm restart still minutes → **45-min idle threshold +
  15-min MIN_UPTIME**. Savings ≈ $0.02/hr/box; the win is the long tail of
  abandoned trials, not shaving minutes.
- **No `last_active` exists.** Add: throttled `last_active_at` on the chat
  dispatcher (`websocket_chat.py:1962`) + on `$connect`; and a **connection-level
  `last_event_at`** bumped unconditionally in `_handle_message`
  (`connection_pool.py:1088`).
- **SAFETY HOLE — autonomous runs are invisible.** cron/channel/Paperclip runs
  execute inside OpenClaw and never open an `_active_runs` window, so a container
  mid-run looks idle and could be killed mid-write (torn WAL). Reaper needs
  `last_event_at` + a pre-stop RPC probe + fail-safe (don't stop if
  unreachable/probe fails). **Largely dissolved by product tiering:** free =
  interactive-only, no channels/cron → exclude any owner with autonomy configured
  from reaping.
- **Wake entry points:** browser `/container/provision` + `/status` auto-retry
  wake a `stopped` box; **chat-to-stopped does NOT wake today** (close with a
  `GATEWAY_WAKING` typed frame + `start_user_service`); channel/Paperclip/cron have
  no wake path → **free/trial containers lose autonomous work while asleep (accept
  as a trial limitation; free tier has none anyway).**

**Reaper predicate (stop iff ALL):** `trialing`; `status==running`; uptime ≥
MIN_UPTIME; no open chat frontends; not `has_active_runs`/`_parked`; `last_event_at`
& `last_active_at` older than threshold; pre-stop probe clean; not an autonomy
owner.

### D. Client surfaces (web / desktop / iOS)

Chat rides the same gateway WS → per-user container on all three.

| Surface | Wakes stopped box? | "User left" | Landmine |
|---|---|---|---|
| **Web** | ✅ open `/chat` → `/status` side-effect | tab close (WS `1000`) | 30s ping keeps WS alive when tab hidden → **false-active**; reconnect budget ~2 min < cold start 3-4 min → socket dies before box returns; `ProvisioningStepper` sticky `reachedReady` latch doesn't re-arm on in-session re-wake |
| **Desktop (Tauri)** | ❌ node path can't wake | only app **Quit** (no window-close handler) | **Two persistent node-host WSs register in `_frontend_connections` (= owner-present)** → tray-resident app pins owner active **24/7**; spin-down never fires; auto-reconnect re-marks active while never restarting the box |
| **iOS** | ✅ foreground → `/status` | app background (OS kills socket) | **Zero push wiring** (no expo-notifications/APNs/backend push) → can't wake for inbound events or signal "ready"; user watches a 5s poll |

**Desktop fix:** exclude `connection_type in {node, operator}` from the idle/presence
signal (operator-relay already excluded, `websocket_chat.py:387`); base "idle" on
actual agent activity, not socket liveness; give node `$connect` a wake capability
or have the desktop webview drive `/provision`.

**Web fixes:** extend WS reconnect to outlast cold start; un-stick the stepper for
in-session re-wake; make read RPCs wake-aware.

**iOS lever:** land free-tier mobile on the **always-on Teams/BFF** surface (pure
REST, renders container-cold) so app-open is instant; treat Chat as on-demand
wake; **add an Expo push path (ready-notification + inbound-event wake) before
shipping mobile spin-down** — it's the one missing primitive that masks cold start.

---

## Design decisions

**Resolved (2026-07-10):**
1. **Provider path:** **Path A — bump image for native `meta`** (§A).
2. **Cap:** **$3 total spend + 3-day window**, whichever first; token/dollar based,
   reserve-worst-case → reconcile; reasoning effort `medium` (§Cap).
3. **Surface scope for v1:** **Web + Desktop.** Desktop requires the
   node-presence-poisoning fix (exclude `node`/`operator` connections from the idle
   signal + give the node path a wake capability). iOS deferred until the Expo push
   path lands.

**Open (needed before planning is final):**
4. **Cap-reached & 3-day-expiry UX** copy + the "show the three providers" handoff.
5. **Terms gate owner** — who verifies Meta's ToS in-browser before go-live.
6. **Reconciliation verification** — confirm Meta `usage` flows into Isol8
   accounting (the load-bearing prerequisite; §Cap). Do this first, before build.

## Cost model (3-day trial)

| Per trial | Container | Inference (Muse Spark) | Total |
|---|---|---|---|
| Always-on (3d) | ~$1.78 | ≤ $3 (hard cap; typ. far less) | ≤ ~$4.78 |
| Idle-deprovisioned (~3 active hrs) | ~$0.10 | ≤ $3 (typ. $0.30–1 to reach aha) | **typ. <$1**, ≤ ~$3.10 worst case |

Inference is now the hard-capped ceiling ($3); idle-deprovision reclaims the
container floor. Meta's $20 free credits + $1.25/$4.25 pricing make early volume
nearly free. Worst case per trial is bounded and known.

## Future work (parked, separate project): paid scale-to-zero

Autonomy's scheduler already lives in **Paperclip, not the container** (on-container
cron is denied, `config.py:1143-1156`). So the ~$18/mo/user always-on paid floor is
cuttable. Gaps: a **wake hook** (routine/webhook firing at a `desiredCount=0`
container currently fails — `resolve_running_container` won't wake it), **channel
ingress** (outbound-from-container, lost at zero), and **cold-start latency**
(fine for batch, bad for "respond now"). Its own design.
