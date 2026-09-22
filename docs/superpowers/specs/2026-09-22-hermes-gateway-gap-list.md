# Native Hermes gateway behaviours the bridge bypasses (gap list, 2026-09-22)

Owner decision: keep the bridge and fix specific gaps. This file only lists what we skip.

Upstream reference is hermes-agent `v2026.9.14` (= 0.21.3, our base). Bridge reference is `apps/infra/hermes/bridge/`. Delete this file when the "fixing now" rows ship.

| Native gateway behaviour | Status | Notes |
|---|---|---|
| Run ingress / Runs API (`/v1/runs`, SSE, stop) | **re-implemented** | `/invocations` envelope → `inbound/<agent>` (room) and `/internal/paperclip/v1/runs` (Paperclip), with runId/eventId idempotency (server.py:61-215) |
| Steer / stop a live run | **re-implemented** | `runs/<id>/abort|steer` → `agent.interrupt` / `agent.steer` (turn.py:228-327) |
| Session keys, one session per room+agent | **re-implemented** | `lightbulb_session_key`. History comes from the room payload, not state.db (turn.py:1137-1158, 1567) |
| Per-session turn serialization | **re-implemented** | Per-agent lock (turn.py:1424-1438) |
| Event streaming to clients | **re-implemented** | AppSync frames from AIAgent callbacks, instead of SSE (turn.py:610-815) |
| Multi-profile isolation (home, `.env`, terminal, env passthrough, MCP creds) | **re-implemented** | ContextVar home plus secret, terminal and AWS scopes (turn.py:1181-1421). Known ceiling: `$HOME` freezes at the first profile to open a shell |
| Health / busy signal | **re-implemented** | `/ping` HealthyBusy while a turn runs (server.py:670) |
| Usage reporting | **re-implemented** | `run.ended` usage for rooms, `usage/<owner>` for the rest (turn.py:296-310, 1010-1019) |
| **Completion-queue drain** (async `delegate_task` results, `notify_on_complete` background processes → follow-up turn) | **fixing now** | Nothing drains `process_registry.completion_queue`, and `delegate_task` is enabled. The result is queued and never delivered. This is the Forge 09-21 silence. Fix: a bridge drain → `delegation.completed` → backend `WORK#FOLLOWUP` in the original thread. Upstream: gateway/run_notifications.py:956-981, 1577-1637. Even upstream's API-server platform does not turn these into caller-visible runs: it writes a transcript row only (`gateway/wake.py:90-132`). So a backend-driven follow-up is the correct shape for us |
| `/ping` busy while background work runs | **fixing now** (with the drain) | `_busy` counts turns only. A live delegation or background process doesn't keep the session HealthyBusy, so AWS can park it mid-work (4 h idle) |
| Ambient "should I reply" gate on the right provider | **fixing now** | #2902 fails open. The Jev backend decision replaces it (slice 2) |
| **Post-turn memory sync / background review finish AFTER `run_conversation` returns** | **fixing now** (drain bundle) | `agent/memory_manager.py:302-308, 480-503` runs provider `sync_all` on a background worker, and `agent/background_review.py` forks a daemon thread after each turn. Our `save_profile` runs right after the turn, so these writes can miss the S3 save and are lost if the session parks. Fix: wait (bounded) for both before `save_profile` |
| Session heartbeats, `/loop` wakeups, kanban notifier/dispatcher | **not needed** | Gateway-loop wakers (run_goals.py:169-199, 444+; kanban_watchers.py). Our wakes come from the backend: the WORK bus, Paperclip routines, and the follow-up rows this bundle adds |
| Cron / in-process scheduler | **deliberately off** | `cron.allow_agent_scheduling: false`, the `cronjob` toolset disabled, and `approvals.cron_mode: deny` (hermes_profiles.py:190-193, 351-362). Paperclip routines are the only scheduler |
| Tool approvals (`/v1/runs/{id}/approval`) | **deliberately off** | `approvals.mode: off`, `unattended_mode: approve`. Human-consent gates live in our own rails (desktop, Paperclip) |
| Chat-platform adapters (Slack, Telegram, WhatsApp, …), delivery ledger/retries, pairing | **not needed** | Rooms, AppSync and the backend's canonical writer own delivery |
| Hosted rooms / room dispatch | **not needed** | Our messaging plane (DynamoDB + WORK bus) is the room system |
| Bot loop guard | **re-implemented** | The backend never dispatches the message's author, plus the bridge's 409 loop guard. Jev's `none` option adds to it |
| Agent cache / idle session reset | **not needed** | A fresh AIAgent per turn (15 s cold / 4 s warm). The session lives in room history and the profile |
| Gateway hooks (`gateway/hooks.py`, builtin_hooks) | **not needed**, watch | Plugin hooks (`ctx.register_hook`) do fire inside AIAgent. Gateway-only hooks don't. No feature of ours depends on one today |
| Housekeeping ticks: session auto-prune/archive, curator tick, skill sync, media/paste sweeps, FTS retry, memory trim, MCP config reconcile | **gap, not yet fixing** | The profile sets `sessions.auto_prune/auto_archive`, but nothing runs the tick that applies them. In-turn memory and curator writes do still run. Risk: slow profile growth in S3. Proposed: run the pure-local subset once per turn end, before save, only if it shows up in profile sizes |
| Persistence of gateway-initiated work (idle / SIGTERM save) | **gap, depends on the drain** | Saves happen only at turn end, and there is no SIGTERM save. That's harmless today because the bridge starts every turn. Once the drain starts turns, the follow-up still runs through `run_turn`, so it keeps turn-end saves. A SIGTERM save is worth adding in the same bundle |
