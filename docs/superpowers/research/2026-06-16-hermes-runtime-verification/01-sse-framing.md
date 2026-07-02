# Hermes runtime verification — Open question #1: SSE framing / resume

**Date:** 2026-06-16
**Scope:** How Hermes delivers the run event stream to an HTTP client; whether `Last-Event-ID` resume is possible; the full event taxonomy; and what breaks under an ALB idle timeout mid-run.
**Source:** Hermes v0.14.0 at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/hermes-agent` (gitignored, outside this worktree). Every claim cites a file:line I read in this pass. Isol8 infra cites the current worktree.

---

## 1. Where the stream lives

`GET /v1/runs/{run_id}/events` is registered at `gateway/platforms/api_server.py:3421` and handled by `_handle_run_events` at `api_server.py:3173-3220`. This matches the audit's citation (audit §1.2/§1.3) — the route and handler are real and current; the line numbers drifted (audit said `:3173-3220`, the handler is now `3173-3220` for the body, route reg at `3421`).

The producer side: `POST /v1/runs` (`_handle_runs`, `api_server.py:2862-3156`) creates a per-run `asyncio.Queue` (`api_server.py:2945-2947`), stores it in `self._run_streams[run_id]`, and spawns a background task `_run_and_close()` (`api_server.py:2975-3140`) that pushes event dicts into that queue. The SSE handler drains the queue and writes each event to the HTTP response.

The transport is aiohttp `web.StreamResponse` (`api_server.py:3191`), **not** `EventSourceResponse` from any SSE library — Hermes hand-rolls the SSE framing.

---

## 2. Does it emit `id:` lines? → NO

The **exact** emit code (`api_server.py:3202-3213`):

```py
while True:
    try:
        event = await asyncio.wait_for(q.get(), timeout=30.0)
    except asyncio.TimeoutError:
        await response.write(b": keepalive\n\n")
        continue
    if event is None:
        # Run finished — send final SSE comment and close
        await response.write(b": stream closed\n\n")
        break
    payload = f"data: {json.dumps(event)}\n\n"
    await response.write(payload.encode())
```

Each event is written as **`data: {json}\n\n` only**. There is:
- **No `id:` line** — the SSE `id` field is never emitted, so a browser `EventSource` never populates `lastEventId`, and a manual client has no per-event cursor to echo back.
- **No `event:` line** either — the SSE *event type* is carried *inside* the JSON payload as a `"event"` key (e.g. `{"event":"message.delta",...}`), not as the SSE-level `event:` field. So all frames arrive as the default `message` SSE type; the consumer must `JSON.parse(data).event` to dispatch.
- Keepalive is an SSE comment `: keepalive\n\n` every 30s of queue idle (`api_server.py:3206`).
- Stream end is signalled by a sentinel `None` pushed onto the queue (`api_server.py:3133`), which the handler turns into `: stream closed\n\n` then closes (`api_server.py:3208-3211`).

**Confirmation that resume is unsupported server-side:** a full-file grep for `Last-Event-ID`, `Last-Event-Id`, and `last_event_id` in `api_server.py` returns **zero matches**. The handler never reads the request's `Last-Event-ID` header (`_handle_run_events`, `api_server.py:3173-3199`, reads only `run_id` from the path and the auth header). Even if a client sent `Last-Event-ID`, the server would ignore it.

**Additionally — the queue is not replayable.** `self._run_streams[run_id]` is a live `asyncio.Queue` consumed destructively by `q.get()` (`api_server.py:3204`). There is no ring buffer of past events. The `finally` block pops the queue out of the registry when the SSE connection closes (`api_server.py:3216-3218`). So even a reconnect to the *same* `run_id` cannot re-read events already drained — and if the SSE socket dropped, the very pop on disconnect destroys the queue. A second `GET .../events` after a drop returns 404 (`api_server.py:3187`, "Run not found") once the queue is gone.

> The same non-resumable pattern is used by the two OpenAI-compat streaming paths (`/v1/chat/completions` at `api_server.py:1345-1430` and `/v1/responses` at `api_server.py:1499-1880`) — both write `data:`-only frames with `: keepalive\n\n` and no `id:`. Resume is absent across all three streaming surfaces, not just `/v1/runs`.

---

## 3. Full event taxonomy on `/v1/runs/{id}/events`

Every event type string pushed onto the run queue (each is the `"event"` field of a `data:` JSON frame). Enumerated by grepping `"event":` literals in `api_server.py` and reading each emit site:

| Event string | Emitted at | Payload fields (beyond `event`,`run_id`,`timestamp`) | Meaning |
|---|---|---|---|
| `message.delta` | `api_server.py:2958-2963` (via `stream_delta_callback` `_text_cb`) | `delta` (text chunk) | Streaming assistant text token(s). This is the only assistant-text path. |
| `tool.started` | `api_server.py:2835-2841` | `tool`, `preview` | A tool invocation began. |
| `tool.completed` | `api_server.py:2843-2850` | `tool`, `duration`, `error` (bool) | A tool invocation finished. |
| `reasoning.available` | `api_server.py:2852-2857` | `text` | Reasoning/thinking text surfaced (note: `_thinking` and `subagent_progress` are explicitly **not** forwarded — `api_server.py:2858`). |
| `approval.request` | `api_server.py:2990-2994` | merges `approval_data` + `choices:["once","session","always","deny"]` | Agent needs exec/tool approval; run status → `waiting_for_approval`. |
| `approval.responded` | `api_server.py:3294-3300` | `choice`, `resolved` (count) | Approval was resolved via `POST /v1/runs/{id}/approval`; run status → `running`. |
| `run.completed` | `api_server.py:3073-3079` | `output` (final text), `usage` (token counts) | Terminal success. |
| `run.failed` | `api_server.py:3059-3064` (structured-failure path) **and** `3111-3116` (exception path) | `error` | Terminal failure. Two emit sites: structured non-retryable client errors (401/400, issue #15561) at `:3057-3070`, and raised exceptions at `:3102-3118`. |
| `run.cancelled` | `api_server.py:3094-3098` | — | Terminal cancellation (via `POST /v1/runs/{id}/stop` → `asyncio.CancelledError`). |

**Status transitions** are tracked separately in `self._run_statuses` via `_set_run_status` (`api_server.py:2801-2814`) and are pollable at `GET /v1/runs/{id}` — they are **not** themselves discrete stream events, but each terminal/approval transition is mirrored by a corresponding stream event above. Status values observed: `queued` (`:2969`), `running` (`:2977`), `waiting_for_approval` (`:2997`), `completed` (`:3082`), `failed` (`:3067`,`:3106`), `cancelled` (`:3090`).

**There is NO `run.started` / `run.queued` stream event** — the start is only the 202 JSON body `{"run_id","status":"started"}` from `POST /v1/runs` (`api_server.py:3152-3155`) and the `queued`/`running` status set internally. A pure-SSE consumer that subscribes after `running` sees its first frame only when the first `message.delta`/`tool.*` arrives.

**Terminal frames are followed by stream close**, not a dedicated `done` event: after `run.completed`/`run.failed`/`run.cancelled`, the `finally` block pushes the `None` sentinel (`api_server.py:3132-3135`) → handler writes `: stream closed\n\n` and returns. So "done" = "received a terminal `run.*` event, then the socket closed cleanly."

---

## 4. Idle-timeout failure mode + mitigation

### Re-grounding the premise — Isol8's relevant idle timeout is NOT 60s, and the ALB is NOT on the stream path

The task framing assumes "ALB 60s idle timeout." **Both halves need correcting against the live infra:**

1. **The ALB idle timeout is already 300s, not 60s.** `apps/infra/lib/stacks/network-stack.ts:79` sets `idleTimeout: cdk.Duration.seconds(300)` on the internal backend ALB (`network-stack.ts:74-81`). AWS ALB default is 60s, but Isol8 already overrode it to 5 min.

2. **The per-user container is NOT reached through the ALB.** The backend discovers each per-user container by **direct private IP** via ECS `describe_tasks` — `ecs_manager.discover_ip` (`apps/backend/core/containers/ecs_manager.py:1182`, called at `:1263`, using `describe_tasks` at `:1979`). The ALB sits only on the *client → FastAPI backend* leg (API Gateway WS → VPC Link → NLB → ALB → FastAPI per CLAUDE.md). So if the backend opens the Hermes SSE connection backend→container, **no ALB is in that path at all** — only intra-VPC routing, which has no application-layer idle timeout (just NLB/conntrack idle, default 350s for established flows).

The keepalive every 30s (`api_server.py:3206`) keeps any reasonable idle timer (300s ALB, 350s NLB) from firing on a slow run, because a comment frame counts as activity on the connection. So **on Isol8's current topology a long-but-active or even idle-with-keepalive run does not trip an idle timeout.**

### Where a timeout actually bites

The real exposure is the **client → backend leg**, which today is **API Gateway WebSocket** (not the ALB). API Gateway WS has a hard **10-minute idle timeout** and a **2-hour max connection duration** that cannot be raised. If Hermes-over-SSE is bridged onto the existing WS chat channel, the WS keepalive (ping/pong in `useGateway.tsx`) already handles idle; the 2h max-duration cap is the only hard ceiling and is far beyond any single run.

If instead a future design exposes the Hermes SSE *directly* to the browser through the ALB (HTTP path), the 300s ALB idle timeout applies — but the 30s keepalive defeats it, so it still won't fire mid-run.

### What breaks if a timeout *does* fire (and the mitigation ranking)

If any proxy hop *did* drop the connection mid-run (e.g. a future hop with a sub-30s idle timeout, or a transient network blip):

- **The run keeps executing.** `_run_and_close()` is a detached `asyncio.create_task` (`api_server.py:3140`) independent of the SSE socket. The agent finishes server-side.
- **But the events are lost.** On SSE disconnect, the `finally` at `api_server.py:3216-3218` pops the queue. Any events produced after the drop are pushed to a queue nobody is draining (and after the pop, `_push`/`_text_cb` find `q is None` and silently drop — `api_server.py:2824-2826`). The terminal `run.*` event and final `output` are gone from the stream.
- **No reconnect-and-replay is possible** because (a) no `id:` cursor and (b) no event buffer (§2). A reconnect to `.../events` returns 404 once the queue is popped.
- The client *can* still recover the final result via `GET /v1/runs/{run_id}` polling (`_handle_get_run`, `api_server.py:3158`), which reads `self._run_statuses` and includes `output`/`usage`/`error` on terminal states (`_set_run_status`, `api_server.py:3080-3086`). So **status polling is the de-facto resume mechanism** — but it loses all intermediate deltas/tool events; you only get the final blob.

**Cleanest mitigation, ranked:**

1. **Do nothing infra-side — rely on the existing 30s keepalive + 300s/350s idle budgets.** On Isol8's actual topology (backend→container direct IP, no ALB; client→backend over API GW WS with its own keepalive), there is no 60s timeout to fix. This is the correct first answer: the premise's 60s number does not exist in our stack. **(Lowest cost, already true.)**
2. **If a real drop must be survived without losing deltas: add server-side resume to Hermes** — emit `id:` lines (monotonic per-run seq) and keep a bounded replay ring per `run_id`, honoring `Last-Event-ID` on reconnect. This is a Hermes-fork change (the upstream stream is fundamentally non-resumable today) and is the only way to recover *intermediate* events. **(Highest fidelity, requires forking `api_server.py`.)**
3. **Cheap middle ground: treat `GET /v1/runs/{id}` polling as the reconnect path.** On SSE drop, the backend re-subscribes if the queue still exists, else falls back to polling run status for the terminal `output`. Loses intermediate deltas but never loses the final answer, and needs no Hermes change. **(Bump idle timeout is NOT needed — it's already 300s.)**

Recommendation: option 1 is the honest answer to the stated question (the 60s premise is wrong for our stack); option 3 is the pragmatic guard for genuine network blips; option 2 only if product wants gapless mid-run resume across reconnects.

---

## 5. Contradictions with the prior audit

- The audit (§1.3 step 9) said keepalive is `": keepalive\n\n" every 30s on idle` and cited `X-Accel-Buffering: no` at `:3196`. **Both confirmed** — keepalive at `api_server.py:3206`, `X-Accel-Buffering: no` at `api_server.py:3196`. No contradiction; the audit was accurate here.
- The audit did **not** state whether `id:` lines are emitted (it was listed as open question #1). **Now answered: NO.** The audit's implicit assumption that the SSE stream is a plain `data:`-only feed is correct and now verified.
- Nothing in the audit contradicts these findings; this fills the open question rather than overturning a claim.
