# Chat Concurrency: Queue + Steer + Server-Sourced Run-Active (OpenClaw 5.28) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the "session file locked (timeout 60000ms)" / "agent busy" storms and the post-approval dead-air by mirroring OpenClaw 5.28's own web-UI chat-concurrency model — a per-session message queue that drains on *server-confirmed* run completion, with Steer, targeted abort, reconnect replay, and side-channel messages.

**Architecture:** Two layers cooperate. (1) **Backend** (`connection_pool.py`/`websocket_chat.py`) becomes the *serialization authority* for ALL entrypoints (web, channels, cron, desktop): it never forwards a second `chat.send` into an active session, sources run-active from the container's authoritative signal, and forwards a clean "run truly ended" event. (2) **Frontend** (`useAgentChat.ts`) mirrors OpenClaw's client queue: every message enqueues, the queue drains only on the backend's server-confirmed run-ended signal (never on a local `clientRunId` edge), Steer injects into the live run, and queued sends replay on reconnect.

**Tech Stack:** Python 3.12 / FastAPI / pytest (backend), React 19 / TypeScript / Vitest (frontend), OpenClaw gateway protocol v4 (unchanged 5.12→5.28), `~/Desktop/openclaw` @ 2026.5.28 = the reference implementation.

---

## Background — root cause (confirmed from source)

The two error cards in the report are **one** event: concurrent runs on the same session contend for the per-session JSONL write lock.

- Lock + timeout string: `~/Desktop/openclaw/src/agents/session-write-lock.ts:43` (`DEFAULT_SESSION_WRITE_LOCK_ACQUIRE_TIMEOUT_MS = 60_000`), error template `src/agents/session-write-lock-error.ts:10-12` (`session file locked (timeout 60000ms): pid=NN …jsonl.lock`). `pid=NN` is the OpenClaw process holding the lock.
- OpenClaw deliberately does NOT serialize *distinct* messages per session — `chat.send` only coalesces same-`idempotencyKey` or same-content sends (`src/.../server-methods/chat.ts:2488-2532`); a different message while busy dispatches a *second* concurrent run that races the lock.
- **Our backend forwards-and-collides:** `routers/websocket_chat.py:659-696` dispatches every `agent_chat` with no busy check; `:1446` calls `mark_run_active` then `:1467` forwards `chat.send` regardless of an active run.
- **Our frontend amplifies:** `hooks/useAgentChat.ts` drains its queue on the local `clientRunId` busy→idle edge, which fires on the local error/terminal clear (`:717-726`, `:769-774`) — *before* the server confirms the prior run released the lock → each queued follow-up dispatches into a still-held lock → the stack of cards.
- **Dead-air (#780)** is the same family: `core/gateway/connection_pool.py:1054-1063` pops the active-run entry on EVERY terminal state including the *interim* `final` of an approval-bracketed run, so run-active drifts to false across a tool approval.

5.12→5.28 deltas that matter: protocol still v4 (no brick risk); 5.28 releases the session lock *during* model I/O (narrows but doesn't remove contention) and adds `EmbeddedAttemptSessionTakeoverError` (an overlapping send can now poison the *first* run); the version constant moved to `packages/gateway-protocol/src/version.ts`. The founders' container in the report is still on **5.12** (prod pin unchanged) — reprovision to 5.28 is a separate, owned workstream (see Prerequisites).

## The OpenClaw 5.28 reference model (the contract we mirror)

All citations are `~/Desktop/openclaw/ui/src/ui/`.

1. **Run-active is the SERVER session row, not a local guess.** `session-run-state.ts:8 isSessionRunActive(row)` reads `row.hasActiveRun`/`row.status` from `sessions.list`. Local "busy" (`app-chat.ts:138 isChatBusy = chatSending || chatRunId`) flips off only when the server row reconciles (`chat/run-lifecycle.ts:201 reconcileChatRunFromCurrentSessionRow` fires only when `row.hasActiveRun === false`/terminal).
2. **Every send goes through a per-session queue** (`app-chat.ts:530 sendChatMessageNow` → `enqueuePendingSendMessage:273`). While busy, the Send button becomes **"Queue"** (`chat/run-controls.ts:83-91`) — never an error.
3. **The queue drains only when not busy, re-triggered by the server `sessions.changed` event** (`app-chat.ts:775 flushChatQueue` early-returns on `isChatBusy`; `app-gateway.ts:799 flushChatQueueAfterSessionRunReconcile` runs it after `applySessionsChangedEvent` sets `hasActiveRun:false`).
4. **Steer** = inject a queued message into the LIVE run (`app-chat.ts:737 steerQueuedChatMessage` → `sessions.steer`), only while a run is active.
5. **Cancel** = `chat.abort` with optional `runId` (`app-gateway.ts:555`); if disconnected it is queued as `pendingAbort` and replayed on reconnect (`app-chat.ts:211`). The run is treated as done only on the server reconcile.
6. **Reconnect replay:** queued sends marked `waiting-reconnect` are replayed after hello (`app-chat.ts:842 hasReconnectableQueuedChatSends`, `:883 retryReconnectableQueuedChatSends`); orphaned runs reconcile as `interrupted`.
7. **Side-channel `/btw`** messages run alongside without disrupting the main run (`app-chat.ts:197 isBtwCommand`, `chat/side-result.ts`).
8. **Local slash-commands queue while busy** (`app-chat.ts:1048 shouldQueueLocalSlashCommand`); **queued items can be retried** when a send fails (`app-chat.ts:914 retryQueuedChatMessage`, `sendState: "failed"`).

## Features we adopt (this plan)

| # | Feature | Layer | OpenClaw ref |
|---|---------|-------|--------------|
| F1 | Backend serialize gate — never forward `chat.send` into an active session | backend | `chat.ts:2488-2532` (race we prevent) |
| F2 | Server-sourced run-active + true run-ended signal (fixes drain + dead-air #780) | backend | `session-run-state.ts`, `run-lifecycle.ts` |
| F3 | Frontend queue drains on server run-ended, not local edge | frontend | `app-chat.ts:775`, `app-gateway.ts:799` |
| F4 | "Queue" affordance while busy (button label + queued list) | frontend | `run-controls.ts`, `chat-queue.ts` |
| F5 | Steer (`sessions.steer`) — inject queued message into live run | both | `app-chat.ts:737` |
| F6 | Targeted `chat.abort {runId}` + reconnect-replayed pending abort | both | `app-gateway.ts:555`, `app-chat.ts:211` |
| F7 | Reconnect replay of queued sends + orphaned-run reconcile | frontend | `app-chat.ts:842-913` |
| F8 | Side-channel `/btw` messages (run alongside, no lock contention) | both | `app-chat.ts:197`, `side-result.ts` |
| F9 | Queued-item retry on send failure | frontend | `app-chat.ts:914` |

Out of scope (do NOT port): `talk.*` (voice), `workboard.*`, `cron.*` UI, `push.web.*`, realtime-talk modules.

## File structure (what each file owns)

**Backend**
- `apps/backend/core/gateway/connection_pool.py` — add `has_active_run(session_key)` (F1); make run-active sourcing NOT clear on interim approval `final` (F2/#780); add a `run_ended` forwarded event when a run truly terminates (F2/F3); `sessions.steer` + targeted `chat.abort` RPC pass-through (F5/F6).
- `apps/backend/routers/websocket_chat.py` — busy-gate the `agent_chat` dispatch (F1); new `agent_steer` + `agent_abort{runId}` message types (F5/F6); `/btw` side-channel routing (F8).
- `apps/backend/core/gateway/connection_pool.py` event transform (`_transform_agent_event`, `:491`) — emit the `run_ended` + (optional) `run_active` events the frontend drains on.

**Frontend**
- `apps/frontend/src/hooks/useAgentChat.ts` — drain on `run_ended` not local edge (F3); enqueue-while-busy (F4); `steer()` (F5); `cancel()` → targeted abort + don't locally mark done (F6); reconnect replay (F7); `/btw` passthrough (F8); queued retry (F9).
- `apps/frontend/src/components/chat/ChatInput.tsx` — "Queue" vs "Send" label when busy (F4); Steer/remove/retry actions on queued items (F4/F5/F9).
- `apps/frontend/src/components/chat/ChatErrorCard.tsx` + `useAgentChat.ts:72 friendlyError` — stop mapping `session file locked` to a scary card (it should be impossible after F1; keep a quiet fallback).

## Decisions (locked) & scope

- **Queue, not reject** (matches OpenClaw web UI). Frontend owns the user-facing queue; backend reject-with-`agent_busy` is the safety net for non-frontend entrypoints (channels/cron) — no backend queue (YAGNI).
- **Adopt Steer (F5)** and the full feature set above per the user's directive ("adopt steer and whatever else openclaw ships").
- **Run-active is server-sourced** — the backend is the authority; the frontend never infers run-ended from a local event.

## Prerequisites (owned elsewhere / coordinate)

- **Reprovision the founders' container to 5.28** (less lock-hold; abort-releases-lock fix) + **MCP prune** — handled by the other agent. This plan is correct for 5.28; on a 5.12 container F1 alone still removes the *collision* (the dominant cause), so it is not blocked on the reprovision.
- **Audit-on-upgrade checklist path** is stale: update `feedback_audit_openclaw_protocol_version_on_upgrade` reference from `src/gateway/protocol/version.ts` → `packages/gateway-protocol/src/version.ts` (do as part of Task 1's commit).

---

## Phase 0 — Stop the bleeding (backend serialize gate)

### Task 1: Backend `has_active_run` + busy-gate the chat dispatch (F1)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (near `has_active_runs`, `:174-211`)
- Modify: `apps/backend/routers/websocket_chat.py` (`_process_agent_chat_background`, `:1264-1478`)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py`, `apps/backend/tests/unit/routers/test_websocket_agent_chat.py`

- [ ] **Step 1: Write the failing test (pool helper).** In `test_connection_pool.py`:

```python
def test_has_active_run_true_only_for_marked_session(connection):
    connection.mark_run_active("agent:a:user-1", "a")
    assert connection.has_active_run("agent:a:user-1") is True
    assert connection.has_active_run("agent:a:user-2") is False
```

- [ ] **Step 2: Run it — expect FAIL** (`has_active_run` undefined).
Run: `cd apps/backend && uv run pytest tests/unit/core/test_connection_pool.py::test_has_active_run_true_only_for_marked_session -v`

- [ ] **Step 3: Implement `has_active_run` on `GatewayConnection`** (next to `has_active_runs`, `connection_pool.py:174`):

```python
def has_active_run(self, session_key: str) -> bool:
    """True if a run is in flight for this exact session (agent:<id>:<member>)."""
    return session_key in self._active_runs
```

Expose it at the pool level (mirror how `mark_run_active` is reached from `websocket_chat.py:1446`): add `GatewayConnectionPool.has_active_run(owner_id, session_key)` that resolves the owner's connection and returns `conn.has_active_run(session_key)` (False if no connection).

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Write the failing dispatch test.** In `test_websocket_agent_chat.py`:

```python
async def test_second_agent_chat_while_active_does_not_forward(monkeypatch, ...):
    # Arrange: pool.has_active_run -> True for this session_key
    # Act: dispatch _process_agent_chat_background a second time
    # Assert: pool.send_rpc(method="chat.send") was NOT called; an "agent_busy"
    #         error frame was pushed to the frontend instead.
```

- [ ] **Step 6: Run it — expect FAIL.**

- [ ] **Step 7: Implement the gate** in `_process_agent_chat_background` (`websocket_chat.py`), BEFORE `mark_run_active`/`chat.send` (~`:1439-1467`):

```python
if pool.has_active_run(owner_id, session_key):
    logger.info("[%s] chat.send GATED agent=%s session=%s (run active)", user_id, agent_id, session_key)
    await _push_frontend_error(owner_id, user_id, agent_id,
                               code="agent_busy",
                               message="Agent is still working on your previous message.")
    return
```

Note: this is the backend SAFETY NET. The frontend (Phase 1) won't normally send while busy, but channels/cron/desktop sends are gated here too. The `agent_busy` frame is structured (code set) so the frontend can render it as "queued/busy", not a scary error.

- [ ] **Step 8: Run both tests — expect PASS.** Then run the file suites:
`uv run pytest tests/unit/core/test_connection_pool.py tests/unit/routers/test_websocket_agent_chat.py -q`

- [ ] **Step 9: Commit** (also update the stale protocol-path note in the memory referenced above if touching that file).

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/routers/websocket_chat.py apps/backend/tests/unit
git commit -m "feat(chat): backend serialize gate — never forward chat.send into an active session"
```

---

## Phase 1 — Server-sourced run-active + frontend drain-on-server-done (F2, F3, F4)

### Task 2: Don't clear run-active on the interim approval `final` (F2 / dead-air #780)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`:1054-1130`, terminal-state handling)
- Test: `apps/backend/tests/unit/core/test_connection_pool.py`

- [ ] **Step 1: Write the failing test.**

```python
async def test_approval_bracketed_interim_final_keeps_run_active(connection):
    # A run that hits a tool approval emits an interim chat.final, then continues.
    # Simulate: mark_run_active, then deliver an interim 'final' that is followed
    # by a pending approval / continuation marker.
    connection.mark_run_active("agent:a:user-1", "a")
    await connection._handle_message(interim_final_with_pending_approval(...))
    assert connection.has_active_run("agent:a:user-1") is True  # still active
    await connection._handle_message(true_terminal_final(...))
    assert connection.has_active_run("agent:a:user-1") is False
```

- [ ] **Step 2: Run it — expect FAIL** (current code pops on any `final`).

- [ ] **Step 3: Implement.** In the terminal handler (`connection_pool.py:1054-1065`), distinguish the *interim* approval-bracketed `final` from a *true* terminal. Mirror upstream's signal: an approval-bracketed run is still active server-side. Use the run/approval markers OpenClaw 5.28 emits — read `~/Desktop/openclaw/.../server-methods/chat.ts` + `attempt.session-lock.ts` to identify the exact field (e.g. a `pendingApproval`/continuation flag on the event, or the `sessions.list hasActiveRun` projection). Only `clear_run` when the event is a true terminal (no continuation pending). Keep the existing pop for `error`/`aborted`.

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git commit -am "fix(chat): keep run-active across tool-approval interim final (dead-air #780)"
```

### Task 3: Backend emits a `run_ended` frontend event on true terminal (F2/F3)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_transform_agent_event` `:491`, terminal handling `:1065-1130`)
- Test: `apps/backend/tests/unit/core/test_chat_event_transform.py`

- [ ] **Step 1: Write the failing test** — a true terminal (`final`/`error`/`aborted`, no continuation) produces a forwarded frame `{"type": "run_ended", "runId": <id>, "outcome": "done"|"error"|"aborted"}` in addition to the existing `done`/`error` frames.

- [ ] **Step 2: Run it — expect FAIL.**

- [ ] **Step 3: Implement** — when the run truly terminates (the same condition as Task 2's `clear_run`), forward an explicit `run_ended` frame. This is the authoritative drain trigger for the frontend (replaces the frontend's local-edge inference). Keep `done`/`error` frames unchanged for existing rendering.

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git commit -am "feat(chat): forward run_ended event on true run termination"
```

### Task 4: Frontend — enqueue while busy, drain on `run_ended` (F3, F4)

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`sendMessage:106`, drain effect `:346-360`, done handler `:717-726`, error handler `:769-774`)
- Modify: `apps/frontend/src/components/chat/ChatInput.tsx`
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts(x)` (create if absent)

- [ ] **Step 1: Write the failing test** (Vitest) — mirrors `app-chat.ts` semantics:

```ts
it("enqueues a send while a run is active and drains on run_ended, not on a local error", async () => {
  // Arrange: start run A (isStreaming true, clientRunId=A)
  // Act: sendMessage("B") while busy
  // Assert: "B" is in queuedMessages and NOT sent yet
  // Act: emit a local 'error' for A (e.g. transient) — assert "B" still NOT sent
  // Act: emit backend 'run_ended' {runId: A} — assert "B" is now sent exactly once
});
```

- [ ] **Step 2: Run it — expect FAIL.**
Run: `cd apps/frontend && pnpm test useAgentChat`

- [ ] **Step 3: Implement.**
  - In `sendMessage`: if `isStreamingRef.current` (busy), push to `queuedMessages` and return (do not call `sendChat`). Mirror `app-chat.ts:530` (always-enqueue) + the `isChatBusy` gate.
  - Replace the drain trigger: remove the `clientRunId` falling-edge drain; add a handler for the backend `run_ended` event that drains exactly one queued message (and recurses if more), mirroring `flushChatQueue` (`app-chat.ts:775`). Drain ONLY when not busy.
  - In the done/error handlers (`:717-726`, `:769-774`): clear local live-state for rendering but DO NOT use them to drain the queue.

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: ChatInput "Queue" affordance.** When `isStreaming`, the send button reads "Queue" and the composer enqueues (mirror `run-controls.ts:83-91`); render `queuedMessages` with a remove (x) action (mirror `chat-queue.ts`). Add a Vitest assertion that the button label is "Queue" while streaming.

- [ ] **Step 6: Run frontend tests + lint.** `pnpm test useAgentChat ChatInput && pnpm run lint`

- [ ] **Step 7: Commit.**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/components/chat/ChatInput.tsx apps/frontend/src/hooks/__tests__
git commit -m "feat(chat): queue sends while busy, drain on backend run_ended (mirror OpenClaw web UI)"
```

---

## Phase 2 — Cancel, Steer, reconnect, side-channel, retry (F5–F9)

### Task 5: Targeted abort + don't-mark-done-locally + reconnect-replayed abort (F6)

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (add `agent_abort` message type carrying optional `runId`)
- Modify: `apps/backend/core/gateway/connection_pool.py` (`chat.abort` pass-through with `{sessionKey, runId?}`)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`cancelMessage:107`)
- Test: backend `test_websocket_agent_chat.py`; frontend `useAgentChat.test.ts`

- [ ] **Step 1: Failing backend test** — `agent_abort` with `runId` forwards `chat.abort {sessionKey, runId}`; without `runId` forwards `{sessionKey}` (mirror `app-gateway.ts:555-560`).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the `agent_abort` handler + pool `chat.abort` pass-through.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Failing frontend test** — `cancelMessage()` sends `agent_abort{runId}` and does NOT locally clear `clientRunId`/drain; it waits for the backend `run_ended` (outcome `aborted`) to drain (mirror `app-chat.ts:201` + the server-reconcile drain).
- [ ] **Step 6: Run — FAIL → implement → PASS.** `cancelMessage` issues the targeted abort; queue drain still keyed on `run_ended`. If disconnected, store a `pendingAbort` and replay on reconnect (mirror `app-chat.ts:211`, `app-gateway.ts:550-565`).
- [ ] **Step 7: Commit.**

```bash
git commit -am "feat(chat): targeted abort (runId) + reconnect-replayed pending abort; drain on server aborted"
```

### Task 6: Steer — inject a queued message into the live run (F5)

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (new `agent_steer` message type)
- Modify: `apps/backend/core/gateway/connection_pool.py` (`sessions.steer` RPC pass-through)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`steerMessage(id)`) + `ChatInput.tsx` (Steer action on queued items)
- Test: backend + frontend

- [ ] **Step 1: Failing backend test** — `agent_steer {agent_id, text, runId}` forwards `sessions.steer` for the active run (mirror `app-chat.ts:758 sendSteerChatMessage`). Verify the exact RPC name/params in `~/Desktop/openclaw/.../server-methods/*` (grep `"sessions.steer"`).
- [ ] **Step 2: Run — FAIL → implement pass-through → PASS.**
- [ ] **Step 3: Failing frontend test** — `steerMessage(id)` only works while a run is active (`isStreaming`); it sends `agent_steer` for the queued item and marks it "steered" (mirror `app-chat.ts:737`, only `host.chatRunId`). On success it removes the item from the normal queue; on no active run it is a no-op.
- [ ] **Step 4: Run — FAIL → implement → PASS.**
- [ ] **Step 5: ChatInput** — render a "Steer" action on queued items only while busy (mirror `chat-queue.ts:69-86`).
- [ ] **Step 6: Run tests + lint. Commit.**

```bash
git commit -am "feat(chat): Steer — inject a queued message into the live run (sessions.steer)"
```

### Task 7: Reconnect replay of queued sends + orphaned-run reconcile (F7)

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (reconnect effect; `sendState` on queue items)
- Test: `useAgentChat.test.ts`

- [ ] **Step 1: Failing test** — a message enqueued while `!isConnected` gets `sendState: "waiting-reconnect"`; on reconnect it is replayed exactly once; an in-flight run whose terminal was lost during disconnect is reconciled as `interrupted` and the queue resumes (mirror `app-chat.ts:842-913`, `app-gateway.ts:567-602`).
- [ ] **Step 2: Run — FAIL → implement → PASS.** Add `sendState` to `QueuedMessage`; on `isConnected` rising edge, replay `waiting-reconnect` items and flush.
- [ ] **Step 3: Commit.**

```bash
git commit -am "feat(chat): replay queued sends on reconnect; reconcile orphaned runs"
```

### Task 8: Side-channel `/btw` messages (F8)

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` (route `/btw`-prefixed messages as side-channel) + `connection_pool.py` (the side-channel RPC/param)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (detect `/btw`, send as side-channel even while busy — no enqueue) + render side results
- Test: backend + frontend

- [ ] **Step 1: Confirm the upstream mechanism.** Read `~/Desktop/openclaw/ui/src/ui/app-chat.ts:197 isBtwCommand` + `chat/side-result.ts` + how `handleSendChat` (`:930`) dispatches a `/btw` (which RPC/param signals "side"). Document the exact contract in the test.
- [ ] **Step 2: Failing backend test** — a `/btw …` message is forwarded as a side-channel run (does NOT acquire the main session lock / does NOT gate on `has_active_run`).
- [ ] **Step 3: Run — FAIL → implement → PASS.**
- [ ] **Step 4: Failing frontend test** — a `/btw` message is sent immediately even while busy (bypasses the queue), and its side-result renders distinctly (mirror `side-result-render.ts`).
- [ ] **Step 5: Run — FAIL → implement → PASS. Commit.**

```bash
git commit -am "feat(chat): /btw side-channel messages (run alongside, no lock contention)"
```

### Task 9: Queued-item retry on send failure (F9)

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`retryQueuedMessage(id)`; `sendState: "failed"`) + `ChatInput.tsx` (retry action)
- Test: `useAgentChat.test.ts`

- [ ] **Step 1: Failing test** — a queued send that fails to dispatch gets `sendState: "failed"` and a `sendError`; `retryQueuedMessage(id)` re-attempts it (mirror `app-chat.ts:914 retryQueuedChatMessage`).
- [ ] **Step 2: Run — FAIL → implement → PASS.**
- [ ] **Step 3: ChatInput** — render a Retry action on `failed` items (mirror `chat-queue.ts:55-67`).
- [ ] **Step 4: Run tests + lint. Commit.**

```bash
git commit -am "feat(chat): retry failed queued sends"
```

---

## Phase 3 — Cleanup & final verification

### Task 10: Quiet the now-impossible lock error mapping + full verification

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts:72 friendlyError`, `apps/frontend/src/components/chat/ChatErrorCard.tsx`
- Test: `ChatErrorCard.test.tsx`

- [ ] **Step 1:** With F1+F3 in place, a `session file locked` error should be unreachable from the web path. Keep a single quiet fallback mapping (no scary "Provider timeout" card) and remove the now-dead duplicate rendering path. Update `ChatErrorCard.test.tsx` accordingly.
- [ ] **Step 2: Run the full backend suite.** `cd apps/backend && uv run pytest tests/ -q` — expect all pass.
- [ ] **Step 3: Run the full frontend unit suite + lint.** `cd apps/frontend && pnpm test && pnpm run lint`
- [ ] **Step 4: Manual/dev verification** (dev container on 5.28): send a message, then send a second mid-run → second shows "Queued", drains after the first finishes (no lock error); Stop cancels; Steer injects; refresh mid-run resumes. Capture before/after.
- [ ] **Step 5: Commit.**

```bash
git commit -am "chore(chat): drop now-impossible lock-error card; final verification"
```

---

## Self-review

**Spec coverage:** F1→Task 1; F2→Tasks 2–3; F3/F4→Task 4; F6→Task 5; F5→Task 6; F7→Task 7; F8→Task 8; F9→Task 9; lock-card cleanup + verify→Task 10. Dead-air #780 = Task 2 (+3). All adopted features mapped.

**Placeholder scan:** Tasks 2, 3, 6, 8 intentionally direct the implementer to confirm an exact upstream field/RPC (the approval-continuation marker; the `sessions.steer`/side-channel param) against `~/Desktop/openclaw` before coding — these are *grounding* steps with the exact files to read, not TBD placeholders, because the precise field name must be read from source at implementation time (it is not safe to guess a wire contract).

**Type/contract consistency:** `run_ended {runId, outcome}` is produced in Task 3 and consumed in Tasks 4/5; `has_active_run(session_key)` defined in Task 1 and reused in Task 2's reasoning; `QueuedMessage.sendState` introduced in Task 7 and reused in Task 9; `steerMessage`/`cancelMessage`/`retryQueuedMessage` are the hook's public surface added in Tasks 6/5/9.

**Sequencing:** Phase 0 (Task 1) alone stops the storms on both 5.12 and 5.28 (it removes the collision). Phase 1 fixes the dead-air + makes the drain correct. Phase 2 adds the OpenClaw feature parity. Phase 3 verifies. Each task is independently committable and testable.
