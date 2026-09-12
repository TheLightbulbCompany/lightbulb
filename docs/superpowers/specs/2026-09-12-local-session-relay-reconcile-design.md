# Local-session relay: reconcile lost acks, isolate bad sessions, never go dark

Disposable planning doc for one fix wave on the desktop mirror (`apps/desktop/src-tauri/src/local_sessions/transport.rs`) and the backend relay (`apps/backend/core/services/local_session_relay.py`, `routers/websocket_chat.py`). Delete when shipped.

## 1. What happened (prod, 2026-09-12)

Desktop 0.4.102 (PR #2565) mirrored this Mac's Claude sessions into prod. At 12:40Z one session had a 5-event batch in flight (`afterSeq` 378 → 383). The relay applied it (prod row `eventSeq` = 383) but the desktop never processed the ack. From then on the desktop's registration for that session was answered with cursor 383, the desktop's reconciliation accepted only `after == 378` or `379`, returned `Err("upload cursor conflict")`, dropped the socket, slept one second, reconnected, and re-registered every session from the first key. API Gateway logs show one new connection every ~4.5 s with exactly three frames each (two good registrations, then the bad one), continuously from 12:40Z to 16:12Z, then nothing: the supervisor loop stopped and never reconnected. Sessions whose keys sort after the bad one were never registered at all.

Neither side logged anything: the desktop transport has no log statements, the backend relay logs one warning unrelated to this, and the router's `local.session.error` frame carries only a code.

## 2. Root causes

1. **Ack tolerance predates batching.** The registration/heartbeat ack check (`after == after_seq || after == after_seq + 1`) was written when a batch was one event. Batches are now up to 32 events, so any lost ack leaves the desktop behind by the batch size and every reconnect is rejected. The backend already answers a re-sent batch idempotently (same first seq + hash → the original end cursor), so the desktop can simply accept a server cursor equal to `after_seq + pending.len()` as the lost ack.
2. **All-or-nothing connection.** Every per-session check failure and every `local.session.error` frame tears the whole connection down, and registration is sequential in key order, so one bad session starves every session after it.
3. **Tight reconnect loop.** `delay` is reset to 1 s on every successful connect, so a connection that dies right after connecting never backs off.
4. **Silent death.** When the discovery or network loop ends (or panics), `supervise` returns and the relay stays dark until the app restarts, with no log line.

## 3. Design

**Reconciliation (desktop).** On `local.session.registered` / `local.session.heartbeat.ack` with cursor `after`:
- `after == after_seq` → nothing to do.
- a batch is pending and `after == after_seq + pending.len()` → the relay applied it and the ack was lost: acknowledge it locally (advance cursor, drain queue, clear pending) and continue.
- anything else → **resync**: adopt `after` as the cursor, drop the local backlog (`pending`, `queue`), enqueue one system marker `[Lightbulb could not mirror some output: this Mac and the relay disagreed about the transcript cursor]` (kind `text`, role `system`, which the web renders as a meta row), persist, continue. Never tear down.

**Per-session quarantine (desktop).** A `stale generation acknowledgment` or `session identity changed` verdict, or a `local.session.error` frame that names a session (`sessionId`) or arrives while that session's registration/heartbeat RPC is in flight, quarantines that one session for the rest of the connection: it is removed from `registered`, skipped for registration and uploads, and retried on the next connection (its persisted state is untouched). Protocol-level faults (unsolicited/mismatched ack, invalid ids, missing fields, stale clock) still close the connection, since they mean the peer is broken.

**Backoff (desktop).** `reconnect_delay(previous, lived)`: a connection that lived ≥ 30 s resets the delay to 1 s; a shorter one doubles it, capped at 30 s. So the worst case is two connects a minute, not thirteen.

**Supervisor (desktop).** `supervise` becomes a loop over `run_once` (the current discovery + network select): when it returns and `stop` is not set, log the reason, mark status `disconnected`, sleep with backoff (1 s doubling to 60 s, reset to 1 s after an iteration that lasted ≥ 60 s), and run again — release builds abort on panic (`Cargo.toml`), so a panic is a visible crash, never a silent dark relay, and this loop only needs to cover the loops that end on their own.

**Logging (both sides, ids only, never transcript text or paths).** Desktop: `[local-sessions]` lines in the app log for connection open (mirror count), connection end (reason, lifetime), registration acked (key, session, cursor), lost-ack reconciled, resync, quarantine (key, reason), error frames (code, session), supervisor exit/restart. Backend: every `_conflict()` names its reason (`ownership`, `older connection`, `generation chain`, `cursor expected=X got=Y`, `batch hash`, `connection`), registration logs session + cursor at INFO, and the router's error frame gains `sessionId` (copied from the rejected frame when it is a string ≤ 64 chars) and a WARNING with code, frame type, session and connection ids.

**Compatibility.** Old desktops ignore the new `sessionId` field. The backend is deployed first; the desktop change works with or without it.

## 4. Out of scope

Anything in the web view (the "Loading conversations…" investigation is separate); Codex-specific behaviour; the skill-on-upgrade gap; the deferred items listed on PR #2565.

## 5. Acceptance in prod

Install the new DMG on this Mac: the wedged record (`0ec3e5c1…`, session `fb5ca661…`) must reconcile to cursor ≥ 383 with no pending batch within a minute of launch; a fresh `claude` session must register; API Gateway `$connect` rate for this device returns to the baseline (a handful per five minutes); the app log shows the new `[local-sessions]` lines.
