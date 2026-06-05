# PR #811 — Upstream Verification Audit (chat concurrency: queue / steer / cancel / /btw / busy-gate)

**Date:** 2026-06-05
**Method:** Dynamic workflow — 13 independent free-text verifiers, one per wire/behavior
contract, each reading both the Isol8 diff and the actual upstream OpenClaw 5.28 source
(`~/Desktop/openclaw` @ `689e8ec8`), then a synthesis pass. Key findings re-verified by hand
(approval flow, `deliver` gate, idempotency cites).

**Headline:** The backend serialization layer and wire contract are faithful. One real **P1**
(frontend `/btw` concurrency collision — the exact scenario the branch exists for) and its
**P2** root cause (missing side-runId discriminator) were fixed. The approval-pause mechanism
**works** for the common case but was **mis-described** in comments and has a narrow leak edge
(fixed comments; safety-valve flagged as follow-up). Several P3 comment/cite inaccuracies fixed.

## Verdict table

| Contract | Verdict | Severity | Resolution |
|---|---|---|---|
| event-segregation-by-runid | DIVERGES | **P1** | FIXED — side runs now segregated by runId |
| btw-wire-and-run (runId tracking) | PARTIAL | **P2** | FIXED — frontend mints + tracks the side runId |
| approval-pause-keeps-active | PARTIAL | **P2** | Comments corrected; leak safety-valve = follow-up |
| busy-serialize-gate | PARTIAL | P3 | Comment corrected (Isol8 adaptation, not upstream feature) |
| run-ended-event | PARTIAL | P3 | Comment corrected (Isol8-synthesized, no upstream event) |
| normal-send-wire (`deliver`) | MATCH | NONE | Made explicit `deliver:false` (was omitted ≡ false) |
| steer-wire | MATCH | NONE | — |
| abort-contract | MATCH | NONE | — |
| queue-drain-frontend | MATCH | NONE | — |
| idempotency-runid-correlation | MATCH | NONE | Stale upstream line cites corrected |
| backend-p1-clear-on-failure | MATCH | NONE | — |
| backend-p2-synthetic-terminal | MATCH | NONE | — |
| wire-self-consistency | MATCH | NONE | — |

## What was wrong, what got fixed

### P1 — `/btw` side run corrupted the primary turn (FIXED)
A `/btw` aside runs concurrently as a **separate run** sharing the sessionKey but carrying its
own runId. The frontend had no runId gate on the live-stream apply path, so a side chunk
hijacked the live stream and a side `done` folded+cleared the still-running primary turn.
Upstream gates this (`ui/src/ui/controllers/chat.ts` `handleChatEvent` `activeRunMatches`;
btw output rendered in a separate side-result card).
**Fix:** the frontend now mints the side runId (sent as `clientRunId` → backend
`idempotencyKey` → the runId every side event carries), records it in `sideRunIdsRef`, and the
event handler drops side-run stream/terminal events before they can touch the primary turn.
Tests added: side chunk doesn't hijack the live stream; side `done` doesn't fold the primary
turn; side `run_ended` doesn't drain the queue.

### P2 — approval-pause mechanism mis-described (comments FIXED; safety-valve = follow-up)
Verified upstream: when an exec tool needs an async approval, the embedded agent emits
`stream:"approval"`, `phase:"requested"` (carrying the current runId,
`embedded-agent-subscribe.handlers.tools.ts:1348-1384`); that run then emits its **genuine**
terminal `final`; the work continues as a **separate** follow-up `agent` run with a fresh
idempotencyKey (`bash-tools.exec-approval-followup.ts`; upstream bounds it at 60s via
`waitForAgentFollowupRun`). The old comments claimed the *same run continues after an interim
final* — wrong. The mechanism still works because `_active_runs` is keyed by `session_key`
(not runId), so the follow-up run's `final` clears the window.
**Leak edge (follow-up):** if the follow-up never spawns (denied-with-suppressed-followup,
subagent/cron suppression, failed spawn), no second `final` arrives → the per-session window
leaks until the connection-level parked→dead reaper tears the connection down. A bounded
per-session safety-valve timeout (mirroring upstream's 60s) would close this — **gated on
first confirming the live container's approval mode** (inline-await emits no `requested` event,
so the mechanism is dormant there; async/followup is the reachable case).

### `deliver` — was a MATCH, made explicit
The web UI's `requestChatSend` hardcodes `deliver:false` for every send. Isol8 *omitted*
`deliver`, which is behaviorally identical because the server gate is `params.deliver === true`
(`chat.ts:674`). Now set explicitly on normal + side sends for faithfulness; verified no-op.

### P3 cosmetic (FIXED)
- `run_ended` comment now states it is an Isol8-synthesized frame (no upstream `run_ended`).
- busy-gate comment now states it is an Isol8 multi-client adaptation (upstream queues
  client-side via `flushChatQueue`/`isChatBusy`; the server does not serialize).
- Stale upstream cites corrected: `clientRunId = p.idempotencyKey` → `chat.ts:2450`;
  `dedupe.get('chat:'+id)` → `:2488`; `chatAbortControllers.get` → `:2496`;
  `addChatRun` → `:2640`.

## Open follow-ups (NOT in this PR)
1. **Approval-pause safety valve** — bounded per-session timeout for a never-arriving
   follow-up; gated on confirming the live container's approval mode via ECS exec.
2. **`/btw` side-result surface** — Isol8 has no side-result card, so `/btw` answers are
   currently dropped from the web UI (safe, but not rendered). Build a side-result surface to
   display them (upstream `ChatSideResult`).
3. **Orphan-run reconnect reconcile** — upstream `app-gateway.ts` reconciles orphaned in-flight
   runs on reconnect; Isol8 only clears via a replayed abort's terminal, so a run streaming at
   disconnect (no cancel) can stay `isStreaming` after reconnect until another event arrives.
4. **`/side` alias** — confirm the server's `isBtwRequestText` actually routes `/side` (not
   just `/btw`) in the running image before relying on it.
