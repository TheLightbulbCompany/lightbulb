# Frontend Run Notifications + Get-Started Dismissal — Design

**Date:** 2026-07-10
**Status:** Design — approved in brainstorm, pending spec review
**Author:** prez2307 (with Claude)
**Scope:** Two frontend-only quick wins split out of the larger UX-upgrade brainstorm. The
LeetCode-style dockable workspace is a **separate future design cycle** and is explicitly out of
scope here.
**Grounding:** file/line references verified this session against `origin/main` (exploration
agents + direct reads). The teams-broker idempotency and unsubscribe-on-unmount findings were
verified by hand this session.

---

## Summary (read this first)

1. **Get-started pill actually goes away.** Completing all 5 steps — or clicking "Dismiss
   checklist" — removes the bottom-right "Get started n/5" pill permanently. Today neither does:
   no code path in `GetStarted.tsx` returns `null`; completion only fires a PostHog event.
2. **Bottom-right run notifications.** While you're in the app but not looking at an agent, a
   sonner toast tells you a run is happening on it — appearing when the run starts, morphing in
   place to finished/failed on its terminal event, and clicking through to the agent. Covers
   webchat runs on any agent/session/tab **and** Teams/Paperclip-driven runs. **Zero backend
   changes.**

Both ship as small frontend PRs with no overlap with the in-flight
`feat/mobile-push-notifications` branch (which owns `connection_pool.py` right now).

---

## Goals / Non-Goals

### Goals
- Get-started pill: gone forever on completion (flicker-free across reloads) and on dismissal.
- Run toasts: one toast per run, live-updating, correctly suppressed for the run the user is
  already watching, clicking through to the same target as the mobile push deep-link
  (agent chat).
- Fix the latent shared `teams.subscribe` teardown bug (see §B3) as part of the wiring.

### Non-Goals (explicit ceiling)
- **Telegram/Discord/Slack channel runs and cron runs.** The backend deliberately does not
  forward those to the web UI (`connection_pool.py:1211` source gate — an anti-transcript-leak
  measure). Surfacing them needs a backend increment; the mobile-push v2 "durable outbound
  events" mechanism is the natural shared rail. Separate spec, later.
- **Notification history / bell inbox.** Toasts are ephemeral; missed ones have fallback
  surfaces (sessions panel, teams activity feed). A future inbox would build on the run map
  extracted into a shared store.
- **Per-agent "running" indicators elsewhere in the UI** (sidebar dots, badges). The run map
  stays private to the listener in v1; extract it into a module store (the `useActiveSession`
  pattern) when the second consumer appears — likely the dockable-workspace project.
- **Sound, browser Notification API, unread counts.** Later, if ever.

---

## A. Get-started dismissal

### Current defect (verified)
- `GetStarted.tsx:12` renders the launcher pill even when `gs.dismissed` is true; the main
  branch (`GetStarted.tsx:20-25`) always renders it too. No branch returns `null`.
- The completion effect (`useGetStarted.ts:180-188`) fires `capture("get_started_completed")`
  and persists a `completedFired` flag to Clerk `user.unsafeMetadata.getStarted` — but nothing
  reads that flag for visibility, and the hook does not expose it
  (`UseGetStarted` interface, `useGetStarted.ts:10-26`).

### Change
1. **`useGetStarted.ts`** — add `completedFired: boolean` to the `UseGetStarted` interface and
   return it (hydrated from Clerk metadata at `useGetStarted.ts:109`; set live by the existing
   completion effect).
2. **`GetStarted.tsx`** — single guard before any render:
   ```tsx
   if (gs.dismissed || gs.completedFired ||
       (gs.progress.total > 0 && gs.progress.done === gs.progress.total)) return null;
   ```

### Why both conditions
- The **live `progress` check** hides the pill the instant the 5th step ticks, before the Clerk
  metadata write round-trips.
- The **persisted `completedFired` flag** is the flicker-free gate on reload: the derived
  completion signals load via four async SWR/RPC fetches (`ChatLayout.tsx:352-477`), so
  `progress.done` briefly reads < 5 on a hard reload; without the flag the pill would flash
  then vanish.
- `dismissed` (already persisted, `useGetStarted.ts:205`) now genuinely removes the pill —
  approved product decision: completion and dismissal both remove it permanently. The re-open
  affordance is deliberately dropped.

No backend changes; storage already exists. Analytics (`get_started_completed`,
`get_started_opened`) unchanged.

### Tests
Component test (single-file vitest): renders when incomplete; returns null for each of the
three hide conditions. `tsc --noEmit` after (frontend CI typechecks tests).

---

## B. Run notifications

### B1. Infra already in place (no new surface)
- sonner `<Toaster />` globally mounted **bottom-right** in the root layout
  (`app/layout.tsx:101`; config `components/ui/sonner.tsx:24` — richColors, closeButton,
  `visibleToasts={3}` which also caps display volume for free).
- Canonical helper `src/lib/notify.tsx` — `notify()` / `notifySuccess()` / `notifyError()`
  with `dedupeKey` (replace-not-stack), sticky errors with a Report action.

### B2. Component + mount
New **`src/components/notifications/RunNotifications.tsx`** — renders `null`; side effects
only. Mounted once in `app/chat/page.tsx` inside `GatewayProvider`, next to `MarketplaceToast`
(`page.tsx:204-208`). Receives `selectedAgentId` and `activeView` as props (the page owns both:
`activeView` derives from `?panel` at `page.tsx:38-41`). It shares the page's single gateway
WebSocket via context — no new connections.

Scope note: `GatewayProvider` is mounted per-page, so toasts fire within the `/chat` shell
(where users live). `/settings` has its own provider and is out of scope for v1.

### B3. Event sources

**Source 1 — chat frames (webchat runs on any agent/session/tab).**
`onChatMessage` (`packages/shared/src/gateway/useGatewayConnection.ts:471-479`) is an
unfiltered firehose: the backend owner-fanout (`_forward_to_frontends`) already pushes every
webchat run's frames to every one of the owner's chat connections, and `useAgentChat` merely
drops non-active frames client-side (`useAgentChat.ts:820-824`, `:837-859`). The listener
tracks a private `Map<runId, {agentId, sessionId, status}>`:
- First frame with an unseen `runId` + `agent_id` (chunk / thinking / tool_*) → loading toast
  *"{Agent} is working…"*, `dedupeKey: runId`, no auto-dismiss.
- `run_ended` (`types.ts:24` — the authoritative terminal; `outcome: done|error|aborted`):
  `done` → success toast, ~5s auto-dismiss; `error` → sticky error; `aborted` → dismiss the
  toast silently.
- A `run_ended` for a never-seen `runId` still toasts (covers WS-reconnect gaps: missed start
  frames degrade to a completion-only toast).
- Frames without `runId`/`agent_id`, and `btw_result` frames (`types.ts:97`), are ignored.
- Map entries are removed on terminal; a safety cap (e.g. 200 entries, evict oldest) bounds a
  pathological session.

**Source 2 — Teams/Paperclip run events.**
Today `teams.heartbeat.run.*` events reach the browser only while a Teams panel is open,
because `TeamsEventsProvider` mounts inside `InlineTeamsPanel` (`InlineTeamsPanel.tsx:48`).
The listener subscribes independently and maps:
- `teams.heartbeat.run.queued` → working toast, `dedupeKey: "teams:" + runId`.
- `teams.heartbeat.run.status` terminal states → success (~5s) / sticky error; cancelled →
  dismiss. **The implementation plan must pin the exact payload field names against
  `teams_event_broker.py:_handle_event` (:197-266) before wiring** — the broker wraps upstream
  Paperclip payloads verbatim.

**No cross-source dedup needed** (verified): paperclip-sourced sessions never emit chat frames
(backend source gate, `connection_pool.py:1211/:1228`), and webchat runs never emit
`teams.heartbeat.*` events. The two streams are disjoint by construction.

### B4. The shared-subscription fix — `src/lib/teamsSubscription.ts`
The broker's `subscribe` is idempotent per connection (set-based —
`teams_event_broker.py:75-82`), **but** `TeamsEventsProvider` sends `teams.unsubscribe` on
unmount (`TeamsEventsProvider.tsx:103-106`). Both consumers share one WS connection ID, so
closing a Teams panel would silently kill the listener's subscription too.

Fix: a tiny module-level refcounted helper — `acquire(send)` / `release(send)` — that sends
`{type:"teams.subscribe"}` on 0→1 and `{type:"teams.unsubscribe"}` on 1→0, and re-sends
subscribe on gateway reconnect while refcount > 0 (both call sites already resubscribe on
connect today; the helper centralizes it). `RunNotifications` and `TeamsEventsProvider` both
switch to it. Net effect with the listener always mounted: the subscription is effectively
always-on inside `/chat`, and panel open/close no longer churns it.

### B5. Suppression rule
No toast when `frame.agent_id === selectedAgentId && activeView === "chat"` — the user is
watching that run (cross-tab runs on the watched agent stream into this tab's chat via
run-adoption, `useAgentChat.ts:996-1015`, so suppression is correct there too). Everything
else toasts, including runs on the selected agent while a panel is open (the chat is hidden —
the user can't see completion).

This rule also covers `/btw` side runs in the practical case: they're initiated from the
active composer, so they target the watched agent and are suppressed. **Accepted residual:** a
side run initiated in *another tab* is indistinguishable from a normal run in v1 (the
`run_ended` frame carries no side-run marker) and may toast. Mirrors the mobile-push branch's
side-run exclusion intent without a backend change.

### B6. Click-through + content
- Chat-run toasts: fire the existing `selectAgent` window CustomEvent
  (`ChatLayout.tsx:78-82` / `page.tsx:157-168`) and clear `?panel` via `shellNav`
  (`src/lib/shellNav.ts`) — landing on that agent's chat, the **same target as the mobile push
  deep-link** (`isol8://agent/<agentId>?session=<key>`). Session-level selection piggybacks on
  the CustomEvent payload only if it already supports it; agent-level landing is acceptable
  for v1.
- Teams-run toasts: `?panel=teams-activity` via `shellNav.pushSearchParams`.
- Titles use agent display names from the already-cached `useAgents` list (SWR `agents.list`),
  falling back to `"Agent"`.
- Analytics: `run_notification_shown` / `run_notification_clicked` PostHog captures with
  `{source: "chat"|"teams", outcome}` props.

### B7. Error posture
The listener is a pure consumer: malformed frames are ignored; every handler is
try/catch-wrapped so a toast bug can never break chat or the gateway connection. Divergence
from mobile push (pushes only on success) is deliberate: web errors are sticky because they're
actionable in-app.

### Tests
- Frame→action logic extracted as a **pure reducer** (`runToastReducer(state, frame) →
  {state', actions[]}`) and unit-tested exhaustively: unseen-run start, terminal outcomes,
  never-seen terminal, suppression rule, btw_result ignore, eviction cap.
- One component test with a mocked gateway context verifying subscribe/acquire lifecycle and
  that actions invoke `notify` with the right dedupeKeys.
- `teamsSubscription` unit test: refcount 0→1→2→1→0 send sequence + reconnect re-subscribe.
- Single-file vitest runs only (frontend suite is RAM-heavy); `tsc --noEmit` after.

---

## Delivery / rollout

- **Two small PRs off fresh worktree branches**, both frontend-only (Vercel auto-deploy on
  merge; no backend, no CDK, no env vars):
  1. `GetStarted` dismissal fix (Section A) — trivial diff.
  2. Run notifications (Section B) — component + helper + tests. Carries this spec doc.
- No coordination needed with `feat/mobile-push-notifications` (disjoint files); the two
  features are complementary surfaces on the same `run_ended` signal.
- No migration: get-started state already lives in Clerk metadata; notifications are stateless.

## Decisions (resolved in brainstorm)

- **Slice:** quick wins now; dockable workspace (dockview-react was the research outcome) gets
  its own design cycle next.
- **Coverage:** webchat + Teams/Paperclip, frontend-only. Channel/cron coverage deferred to the
  shared backend rail (mobile-push v2).
- **Toast UX:** one live toast per run updating in place; not completion-only, not two toasts.
- **Get-started:** completion AND dismissal both remove the pill permanently.
- **Wiring:** self-contained listener (Approach 1) over hoisted provider + run registry
  (Approach 2). Nothing consequential lost: store extraction is a later refactor, teams SWR
  freshness is unchanged from today, and the one real hazard (shared subscription teardown) is
  fixed by the refcount helper — the one piece of Approach 2 worth taking now.

## Open questions
- None blocking. (Teams heartbeat payload field names are an implementation-time verification,
  not a design unknown.)
