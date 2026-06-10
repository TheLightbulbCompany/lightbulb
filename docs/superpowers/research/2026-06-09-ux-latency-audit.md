# UX / Latency Audit — 2026-06-09

Method: (1) code-level latency map of the signup → Stripe → provision → chat
journey, (2) live instrumented measurements against dev.isol8.co (Performance
API, WebSocket frame hook, DOM-milestone observer), (3) dev CloudWatch log
correlation (`/ecs/isol8-dev`). PostHog session replay was NOT usable — see
finding #0.

User under test: `user_3DYRa5Fn4SxXjbXVFoanzCIxKBT` (personal dev account,
container already provisioned and healthy). All timestamps UTC 2026-06-09.

---

## 0. Observability gaps that block UX measurement

1. **PostHog is not loaded on dev at all.** `window.posthog` is undefined on
   dev.isol8.co/chat; zero posthog network requests. `NEXT_PUBLIC_POSTHOG_KEY`
   is evidently unset in the dev Vercel env. Consequence: no session
   recordings, no funnel data, and every `capture()` call in the frontend
   (`chat_message_sent`, `chat_aborted`, …) is a no-op for all dev testing.
   The backend POSTHOG_* keys are also unset locally. Action: set the dev
   Vercel env key (or accept prod-only analytics and say so somewhere).
2. **Chat pipeline has no client-side logging.** Two messages were swallowed
   with zero console output (see #1). Backend `websocket_chat` logs every
   frame `method=` (good — this is what let me prove frame loss), but the
   frontend logs nothing on send/queue/drop decisions.
3. **No WS API GW access logging** — `/aws/api-gateway/isol8-dev-websocket`
   log group is empty.

## 1. CRITICAL — chat messages can vanish silently (reproduced live, 2/3 sends lost)

Three identical sends to Friday from a healthy, "Connected" chat page:

| send | wall clock | optimistic bubble | queue chip | reached backend (`ws msg type=agent_chat` in /ecs/isol8-dev) | in `chat.history` after reload |
|---|---|---|---|---|---|
| 1 | ~21:29 | never rendered | never | **NO** | **NO** |
| 2 | ~21:38:55 | never rendered | never | **NO** | **NO** |
| 3 | 21:49:23 | rendered | n/a | YES | YES |

For sends 1–2 the input cleared (so `ChatInput.handleSend` →
`onSend` → `useAgentChat.sendMessage` ran), but no path's artifact appeared:
no optimistic user bubble (`dispatchNow` appends it BEFORE the wire send,
useAgentChat.ts:1061), no "N queued" chip (ChatInput.tsx:216), no error
bubble, no console.error. The frame never left the browser (verified twice:
backend frame log + server-side history reload). Mechanism not yet pinned —
candidates: send during a reconnect window with queue state lost on remount,
or a stale-closure/race in the busy gate. Needs client-side structured
logging on every sendMessage branch to close.

UX impact: user types a message, presses Enter, message disappears forever
with zero feedback. This is the single worst UX defect found.

## 2. CRITICAL — dev chat is hard-broken right now: Bedrock AIP mint fails

> **CORRECTION (same day):** already fixed on main — PR #838 (merged
> 2026-06-09 21:58 UTC, 26 min after the failure below was observed) makes
> `copyFrom` pass the full ARN. Dev heals on the next backend deploy.
> The duplicate error-bubble rendering is explained by finding #3 (orphan
> socket double-delivers every event) — fixed in the remediation branch.

`background_provision` for this owner failed at 21:32:17:

```
ensure_owner_aips → CreateInferenceProfile ValidationException:
Value 'us.anthropic.claude-opus-4-6-v1' at 'modelSource.copyFrom' failed
to satisfy constraint: Member must satisfy ... arn:aws...:bedrock:...:
(inference-profile|foundation-model)/...
```

`SYSTEM_PROFILE_ID` (core/billing/bedrock_aip.py:58, on main) passes bare
model IDs where `copyFrom` requires a full ARN. The opus entry
(`us.anthropic.claude-opus-4-6-v1`) raises first, aborting the whole AIP
mint for any owner without persisted AIPs. Adjacent: `workspace
deleted_auth_profile_store` logged in the same second. The successful test
send (#1, row 3) got back **"Error: LLM request failed." — twice** (duplicate
error bubbles, raw error text, no retry affordance).

Also note the haiku entry has a `:0` version suffix while opus/sonnet don't —
the dict is internally inconsistent.

## 3. WebSocket connection behavior

- **Every /chat page load opens 2 WebSocket connections** (backend logs show
  same-second `WS connect` pairs for every load: 21:28:47×2, 21:38:39×2,
  21:40:19×2 …). Doubles authorizer invocations, DDB connection rows, and
  fan-out work. Find the second mount (second GatewayProvider? Teams broker?).
- **Idle churn:** 15 connects in ~20 min from ~3 open tabs with no user
  activity (pairs at 21:40:42, 21:41:29, 21:43:37, 21:46:47). Idle tabs are
  reconnecting every ~1–3 min. Each reconnect is a window where sends can be
  queued/lost (see #1).
- Frontend reconnect backoff `[1s,2s,4s,8s,16s×6]`, ping 30s, connection
  timeout 10s, RPC timeout 30s (useGateway.tsx:22-25). Nested worst case
  10s+30s=40s before a user-visible failure.

## 4. Measured warm-path numbers (dev, healthy container)

| milestone | value |
|---|---|
| HTML TTFB (Vercel) | 31 ms |
| domInteractive | 170 ms |
| Clerk JS fetched | 144 ms → loaded ~400 ms |
| **first API call possible (Clerk session ready)** | **~1,140 ms** |
| 6 API calls (billing/account, credits, users/me, container/status, users/sync, marketplace search) | all parallel at ~1,140 ms, each ~245 ms — no waterfall ✅ |
| "Connected" indicator + agents list | **~1,960 ms** |
| backend gateway→container connect (pooled, warm) | 466 ms (ws_upgrade 40 + handshake 118 + verify 240) |

Verdict: warm path is genuinely good (~2 s to interactive chat). The
problems are the cold/first-run path and failure-path feedback, not the
happy path.

- Papercut: `/container/status` returned in ~246 ms but `users/sync` (POST)
  fires on every load; `marketplace/listings/search?seller_id=…` fires on
  every /chat load (seller listings — needed only for the Publish surface).
- Papercut: `container/updates` fetched 3× within 800 ms of load
  (1411/1792/2208 ms) — duplicate SWR keys or three mounts.

## 5. Cold path / provisioning flow (code-level, not re-measured live)

From the code audit (file:line refs verified by the explore pass):

1. **Stripe checkout → provisioning dead window.** ~~After returning from
   Stripe, `useBilling`'s 30 s SWR dedup leaves a dead spinner until the
   webhook lands.~~ **CORRECTION: not real on main** — ChatLayout already
   polls `refreshBilling()` (an SWR `mutate()`, which bypasses dedup) every
   2 s for up to 30 s on `?checkout=success` return (ChatLayout.tsx:183-190).
   The audit's code pass read the stale `billing-metering-remediation`
   branch for this file.
2. **Provisioning progress is time-faked.** ProvisioningStepper shows
   "gateway" at T+45 s and "channels" at T+180 s by hardcoded timers
   (ProvisioningStepper.tsx:55-56) regardless of real state. Replace with
   real signals (the backend already polls ECS every 10 s and knows the
   actual phase).
3. **`/container/status` poll cost.** Frontend polls 3 s; blocked-state
   cadence 5 s→30 s after 60 s. Backend cold-start poller runs
   describe_services + list_tasks + describe_tasks sequentially every 10 s
   (ecs_manager.py:1649-1688) with no caching — ~30 AWS calls per user
   cold-start; O(N) at fleet scale.
4. **Fargate cold start itself (~3–4 min) dominates** first-run UX. Options
   if it ever needs to shrink: pre-warmed task pool, or at minimum honest
   progress (see 5.2).

## 6. Button / feedback papercuts

- Send button ignores connection state (`isDisabled = disabled ||
  isUploading`, ChatInput.tsx:170) — clickable while disconnected.
- Subscribe/provider CTA has no in-button loading state; Stripe redirect
  gives no feedback between click and page swap (ProvisioningStepper.tsx:1414).
- Agent error bubbles render raw strings ("Error: LLM request failed."),
  duplicated, with no retry affordance.

## 7. PostHog prod data pass (added same day, admin session, project 380894)

Last 30 days of prod events (queried via HogQL):

- ~~**Chronic completion gap:** ~50% of `chat_message_sent` never gets a
  `chat_completed`.~~ **CORRECTION: instrumentation artifact.**
  `capture("chat_message_sent")` fires TWICE per send — once in
  `ChatInput.handleSend` (ChatInput.tsx:121, `{has_files}`) and once in
  `dispatchNow` (useAgentChat.ts:1241, `{agent_id, message_length}`) —
  both since PR #383 (2026-04-24), so `sent` is double-counted for the
  whole window. Corrected ratio: ~334 actual sends → 287 completed + 15
  aborted ≈ **90% accounted for**. The residual ~10% is the real
  loss/error rate — still worth chasing, but not a 50% catastrophe.
  Fix: dedupe (drop one call site or rename ChatInput's to
  `chat_send_clicked`).
- **59 rage clicks**, 42 of them on `/chat` against unlabeled elements,
  plus rage clicks on "Retry", "Restart Gateway", "Cancel", "Control" —
  users hammering recovery controls.
- **Exceptions:** `ApiError` ×22 (top values: "deploy failed" ×15,
  "PATCH /api/issues/… → 422" ×6), React minified error #418 (hydration
  mismatch) ×5, Clerk chunk-load failures ×2.
- **Telemetry gaps:** prod's `chat_completed` events carry ONLY
  `agent_id` — though main already emits `duration_ms` +
  `assistant_message_length` (useAgentChat.ts:1082), the prod build
  predates it, so historical turn latency is unmeasurable; the
  double-capture of `chat_message_sent` is removed in the remediation
  branch (dispatchNow is now the single capture point).
  `onboarding_step_completed` (343 events) has a null `step` property,
  so the onboarding funnel can't be localized. **Session replay: 0
  recordings in 30 days** — replay is not enabled; the saved recording
  views are empty shells.
- Chat events stop after 06-05: VERIFIED a usage stop, not a capture
  break. `/chat` pageviews collapsed in lockstep (84/day on 06-02 →
  0–2/day from 06-06), other prod pageviews keep flowing, and the
  analytics diff in the suspected window (#821) is type-only. One user
  (`user_3DBS55…`, the founders' account) produced 620 of 668 sends and
  last chatted on prod web 06-05 13:09 — exactly when the prod container
  crash-loop investigation started. Subsequent chatting happens on
  dev/desktop (both dev.isol8.co, no PostHog key) and channels, none of
  which emit prod web events.

## Priority order (impact × effort)

1. **#1 silent message loss** — add branch logging to sendMessage/dispatchNow,
   pin and fix; messages must never vanish without UI.
2. **#2 bedrock_aip copyFrom ARN bug** — small fix, currently breaks every
   new dev provision (and prod owners without persisted AIPs).
3. **#5.1 post-Stripe dead window** — `mutate()` on checkout-return; biggest
   first-impression win.
4. **#3 double WS connection + idle churn** — halves connection load, shrinks
   the loss window for #1.
5. **#5.2 real provisioning progress** — trust during the unavoidable 3–4 min
   cold start.
6. **#0 PostHog on dev** + client-side chat logging — so the next audit can
   use real session data.
7. Papercuts: duplicate `container/updates` fetches, send-button state,
   error-bubble dedup/retry.
