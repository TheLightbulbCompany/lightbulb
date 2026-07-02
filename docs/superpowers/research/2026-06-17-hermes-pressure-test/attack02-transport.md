# attack02 — TRANSPORT (SSE / Q1 / abort / approval / volatile runs)

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent @ 4c46c35e`. Every line cite
re-read independently this pass against `gateway/platforms/api_server.py` and the agent
callback sites. Posture: uncharitable. Where I disagree with map01 / the interim report,
I say so.

---

## (a) SSE: non-resumable + finally-pop-orphan + single-consumer-destructive

**CONFIRMED, and the report is RIGHT to call it "worse than stated" — but its hardening
prescription is incomplete.**

- **Non-resumable**: `_handle_run_events` writes `data: {json}\n\n` only (`api_server.py:3212-3213`),
  a `: keepalive\n\n` comment on 30s idle (:3206), and a `: stream closed\n\n` sentinel on the
  `None` frame (:3210). No `id:`, no `event:`, no `Last-Event-ID` read (the handler never touches
  request headers beyond `_check_auth`). A re-`GET .../events` after the queue is gone 404s after a
  ~1s wait loop (:3182-3187). All re-verified.
- **finally-pop-orphan**: the `finally` block unconditionally does
  `self._run_streams.pop(run_id, None)` + `_run_streams_created.pop(...)` (:3216-3218) on ANY exit —
  client disconnect, exception, or normal sentinel. The agent task keeps running (it lives in
  `_active_run_tasks`, untouched here); only the terminal `_run_statuses` row survives for polling.
- **Single-consumer-destructive (the sharp one)**: `q = self._run_streams[run_id]` (:3189) then
  `await q.get()` (:3204) is a destructive read off ONE `asyncio.Queue`. There is no fan-out, no
  tee, no refcount. **Two subscribers to the same `run_id` split the event stream** (each `q.get()`
  removes the item) AND the **first to disconnect pops the queue out from under the second AND the
  worker** (:3217) — the worker's subsequent `q.put_nowait(...)` then lands in an orphaned queue
  nobody reads. **Where I push past the report:** the report's mitigation ("backend pool must be the
  sole SSE reader") is necessary but the report misframes the worker's own behavior — after the pop,
  the worker keeps calling `q.put_nowait` on the now-detached queue (`_push` re-fetches via
  `self._run_streams.get(run_id)` at :2824 and silently no-ops once popped, but `_run_and_close`'s
  direct `q.put_nowait(None)` at :3133 holds a stale local `q` reference and succeeds into the
  detached queue — harmless GC, not an error). Net: no crash, but a mid-run reader drop is
  **silently unrecoverable**, not merely "lossy."

## (b) §2 Q1 event-taxonomy table — COMPLETE and ACCURATE (report under-credited the spec)

**CONFIRMED — and I DISAGREE with the implicit map01/report framing that the taxonomy is shaky.**
The spec's §2 Q1 table (spec lines 50-58, header at 48-49) lists exactly the events the SSE queue can
carry, and it is **exhaustive**. Authoritative source: the run queue is fed from only four producers —
`_make_run_event_callback` (`api_server.py:2832-2860`), the `_text_cb` delta pump (:2954-2965), the
`_approval_notify` closure (:2987-3003), and `_run_and_close`'s terminal puts (:3059-3116) plus the
approval-responded put (:3294-3300). Enumerated emitted `"event"` values: `message.delta` (:2959),
`tool.started` (:2836), `tool.completed` (:2844), `reasoning.available` (:2853), `approval.request`
(:2990), `approval.responded` (:3295), `run.completed` (:3074), `run.failed` (:3060 & :3112),
`run.cancelled` (:3095). That is **9 events — all 9 are in the spec table** (the report's own excerpt
truncated `approval.responded` at spec:55, making the table look one short; it is not).
**Two correctly-documented omissions, verified at the agent boundary:** the agent's
`tool_progress_callback` is invoked with only `_thinking`, `reasoning.available`, `tool.started`,
`tool.completed` (`agent/conversation_loop.py:2923,2928`; `agent/tool_executor.py:164,388,559,824`),
and `_make_run_event_callback` deliberately drops `_thinking` and `subagent_progress`
(:2858 comment + no branch for them). So delegated-subagent progress and raw thinking-prefill lines
**never reach the SSE stream** — a real UX gap for multi-step/delegated runs, correctly flagged by the
spec's "thinking block" mapping caveat but worth stating louder: **subagent work is invisible on the
wire.** No mislabeled rows found.

## (c) `/v1/runs` state fully volatile — CONFIRMED, and WORSE than "task replacement"

**CONFIRMED, and I escalate it: even a GRACEFUL shutdown drops in-flight runs with no terminal frame.**
All five run dicts are in-process only — `_run_streams`, `_run_streams_created`, `_active_run_agents`,
`_active_run_tasks`, `_run_statuses`, `_run_approval_sessions` (`api_server.py:659-670`). I grepped
every reference (:659-3384): zero serialization, zero boot-time recovery, zero requeue. `ResponseStore`
+ `SessionDB` (SQLite) persist only conversation history, never run lifecycle. **The escalation:**
`disconnect()` (the documented shutdown entrypoint, called via `_safe_adapter_disconnect`,
`run.py:1600-1616,3611,3655`) does `_mark_disconnected()` + `site.stop()` + `runner.cleanup()`
(`api_server.py:3493-3503`) and **never iterates `_active_run_tasks` to cancel them, drain queues, or
emit a terminal frame.** So the report's "ECS task replacement / image deploy / OOM silently drops the
run" understates it — a *clean* SIGTERM-driven ECS rollout does the same: in-flight runs are abandoned,
the SSE stream never gets `run.cancelled`/`run.failed`/`None`, and `_run_statuses` is lost with the
process. The client watchdog must treat "no terminal frame AND `GET /v1/runs/{id}` → 404" as a distinct
"run vanished" state, and runtime-switch / key-rotation / deploy MUST drain or hard-fail in-flight runs
first — Hermes will not drain them for you.

## (d) Abort / approval flow — CONFIRMED, with two real teeth the report glosses

- **Abort (`POST /v1/runs/{id}/stop`, :3311-3349)**: 404 if neither agent nor task is tracked (:3321);
  sets `stopping`; calls `agent.interrupt(...)` (:3328); `task.cancel()` (:3333) then a **bounded 5s
  `wait_for(shield(task))`** (:3339). **The tooth:** `run_conversation()` runs in the default
  ThreadPoolExecutor thread (:3053), which `task.cancel()` **cannot preempt** (the code's own comment,
  :3334-3337). Cancellation depends entirely on `agent.interrupt()` breaking the loop; if the agent is
  blocked inside a slow tool subprocess or an LLM HTTP call, `/stop` returns `{"status":"stopping"}`
  after 5s while the run keeps burning tokens. So "stop" is **advisory, not guaranteed** — Isol8's
  metering must not assume tokens stop at the `/stop` call.
- **Approval (`POST /v1/runs/{id}/approval`, :3223-3309)**: maps `approve`/`approved`/`allow`→`once`
  (:3243); validates `{once,session,always,deny}` (:3245); resolves via
  `resolve_gateway_approval(session_key, choice, resolve_all)` (:3272) keyed on
  `_run_approval_sessions[run_id]` (:3255); 409 if no active approval session (:3256) or nothing pending
  (:3281); on success pushes an `approval.responded` frame back onto the SSE queue (:3294-3300).
  **The teeth:** (1) approval is gated by the **same single bearer** as everything else (:3225) — there
  is no per-device / per-operator trust binding, so Isol8's "only the paired operator device may
  approve" policy must be enforced *in the backend* (which approvals it forwards), exactly as the spec's
  §189 row states. (2) The approval session key defaults to `gateway_session_key or session_id or
  run_id` (:2942) — if the backend reuses a `session_id` across runs, `resolve_all=true` could resolve
  approvals across the wrong run; the backend must send per-run-unique session keys to keep approval
  routing run-scoped.

---

## SIX BULLETS (return value)

1. **(a) SSE — CONFIRMED + sharpened.** Non-resumable (`data:`-only, no `id:`/`Last-Event-ID`,
   :3212-3213), queue popped in `finally` on ANY disconnect (:3217), single destructive `asyncio.Queue`
   per run (:3189,:3204) → two subscribers split the stream and the first disconnect detaches it from
   the worker + the second reader. A mid-run reader drop is **silently unrecoverable** (worker keeps
   `put_nowait`-ing into a detached queue, harmless but lost), not merely lossy.
2. **(b) Q1 taxonomy — CONFIRMED COMPLETE; I disagree with any "shaky" framing.** The 9 wire events
   (`message.delta`/`tool.started`/`tool.completed`/`reasoning.available`/`approval.request`/
   `approval.responded`/`run.completed`/`run.failed`/`run.cancelled`) are the *exhaustive* producer set
   (api_server.py:2832-3116,3294); spec table 50-58 matches all 9. The report's excerpt truncated
   `approval.responded` (spec:55) and made the table look short — it isn't.
3. **(b) Two confirmed drops worth stating louder:** `_thinking` and `subagent_progress` are
   deliberately NOT forwarded (:2858; agent only ever fires `_thinking`/`reasoning.available`/
   `tool.started`/`tool.completed` per conversation_loop.py:2923,2928 + tool_executor.py:164,388,559,824)
   → **delegated-subagent progress is invisible on the SSE wire**, a genuine multi-step UX gap.
4. **(c) Volatile runs — CONFIRMED + ESCALATED.** All run state is in-process dicts (:659-670), zero
   persistence/recovery/requeue (grep of all 24 refs, :659-3384). Beyond the report's "task replacement"
   framing: `disconnect()` (:3493-3503) does NOT cancel/drain in-flight runs, so even a **graceful
   SIGTERM ECS rollout** abandons runs with **no terminal frame ever** — the client must treat
   "no terminal + GET 404" as a vanished-run state.
5. **(d) Abort is advisory, not guaranteed.** `/stop` (:3311-3349) relies on `agent.interrupt()` because
   `task.cancel()` cannot preempt the executor thread (:3334-3337); a 5s bounded wait then returns
   `stopping` while a run stuck in a tool/LLM call keeps spending — metering must not assume tokens halt
   at `/stop`.
6. **(d) Approval = same-bearer, session-key-routed.** No per-device trust (:3225) → operator-device
   approval policy must live in the Isol8 backend; approval routing keys on
   `gateway_session_key||session_id||run_id` (:2942,:3255) so the backend must send per-run-unique
   session keys or risk cross-run `resolve_all` bleed.
