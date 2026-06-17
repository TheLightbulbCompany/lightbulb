# map01 — Hermes daemon + run surface

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
Primary files read: `gateway/platforms/api_server.py` (3525 lines, full), `gateway/run.py` (selected).
Every claim below is `file:line` from the actual tree.

---

## (a) FULL HTTP route table

All routes are registered in one place: `APIServerAdapter.connect()` →
`gateway/platforms/api_server.py:3400-3423`. The adapter is the ONLY HTTP
surface; there is no separate router module.

| Method | Path | Handler | api_server.py |
|--------|------|---------|---------------|
| GET | `/health` | `_handle_health` | :3400 / :918 |
| GET | `/health/detailed` | `_handle_health_detailed` | :3401 / :922 |
| GET | `/v1/health` | `_handle_health` (alias) | :3402 |
| GET | `/v1/models` | `_handle_models` | :3403 / :943 |
| GET | `/v1/capabilities` | `_handle_capabilities` | :3404 / :964 |
| POST | `/v1/chat/completions` | `_handle_chat_completions` | :3405 / :1023 |
| POST | `/v1/responses` | `_handle_responses` | :3406 / :2092 |
| GET | `/v1/responses/{response_id}` | `_handle_get_response` | :3407 / :2376 |
| DELETE | `/v1/responses/{response_id}` | `_handle_delete_response` | :3408 / :2389 |
| GET | `/api/jobs` | `_handle_list_jobs` | :3410 / :2434 |
| POST | `/api/jobs` | `_handle_create_job` | :3411 / :2449 |
| GET | `/api/jobs/{job_id}` | `_handle_get_job` | :3412 / :2497 |
| PATCH | `/api/jobs/{job_id}` | `_handle_update_job` | :3413 / :2516 |
| DELETE | `/api/jobs/{job_id}` | `_handle_delete_job` | :3414 / :2549 |
| POST | `/api/jobs/{job_id}/pause` | `_handle_pause_job` | :3415 / :2568 |
| POST | `/api/jobs/{job_id}/resume` | `_handle_resume_job` | :3416 / :2587 |
| POST | `/api/jobs/{job_id}/run` | `_handle_run_job` | :3417 / :2606 |
| POST | `/v1/runs` | `_handle_runs` | :3419 / :2862 |
| GET | `/v1/runs/{run_id}` | `_handle_get_run` | :3420 / :3158 |
| GET | `/v1/runs/{run_id}/events` | `_handle_run_events` | :3421 / :3173 |
| POST | `/v1/runs/{run_id}/approval` | `_handle_run_approval` | :3422 / :3223 |
| POST | `/v1/runs/{run_id}/stop` | `_handle_stop_run` | :3423 / :3311 |

**Spec-relevant surprise #1:** the spec (per the docstring at api_server.py:3-17)
advertises only the run/chat/responses/health endpoints, but the live route
table ALSO exposes a **full cron-jobs CRUD + trigger surface** (`/api/jobs*`,
8 routes, :3410-3417) gated only by the same single bearer key. Any integration
that mounts this server hands buyers cron control unless that key is locked
down. There is **no per-user / per-tenant scoping anywhere** — auth is one
shared static bearer token (`_check_auth`, :742-762; `hmac.compare_digest`
against a single `self._api_key`). If no key is set, ALL requests pass
(:750-751). This is fundamentally a single-tenant, local-first server.

---

## (b) Run lifecycle (create → queue → execute → terminal) and where state lives

Entry: `POST /v1/runs` → `_handle_runs` (:2862).
1. **Admission/concurrency gate** (:2873-2878): `if len(self._run_streams) >=
   self._MAX_CONCURRENT_RUNS (=10, :2797): return 429`.
2. **create**: mint `run_id = f"run_{uuid4().hex}"` (:2940); build an
   `asyncio.Queue` per run (:2945); register it in the in-memory dicts
   (:2947-2949); `_set_run_status(run_id, "queued", ...)` (:2967-2973).
3. **queue→execute**: `task = asyncio.create_task(_run_and_close())` (:3140);
   return `202 {"run_id", "status":"started"}` IMMEDIATELY (:3152-3156).
   Inside `_run_and_close` (:2975): status flips `queued`→`running` (:2977),
   agent is created (:2978), then the BLOCKING `agent.run_conversation(...)` is
   pushed to a thread via `run_in_executor(None, _run_sync)` (:3053, :3005).
4. **terminal**: on return, status → `completed`/`failed` (:3057-3086); on
   `CancelledError` → `cancelled` (:3087-3092); on exception → `failed`
   (:3102-3109). `finally` (:3119-3138) pushes the `None` SSE sentinel and pops
   the run from `_active_run_agents`/`_active_run_tasks`/`_run_approval_sessions`.

**Where state is held — ALL in-process Python dicts on the adapter instance**
(`__init__`, :658-671):
- `_run_streams: Dict[run_id, asyncio.Queue]` (:659) — live event queues.
- `_run_streams_created` (:661) — TTL sweep timestamps.
- `_active_run_agents` / `_active_run_tasks` (:663-664) — for /stop.
- `_run_statuses: Dict[run_id, dict]` (:666) — pollable status, TTL 3600s (:2799, swept :3377-3384).
- `_run_approval_sessions` (:670) — approval routing.

**Spec-relevant surprise #2:** run lifecycle state is **purely volatile**.
A gateway restart (or crash, or the self-restart machinery in run.py:3113-3211)
loses every in-flight run, all status, and all event queues. Only
`ResponseStore` (the `/v1/responses` path) and `SessionDB` persist to SQLite
(:320-446, :833-845). Runs submitted via `/v1/runs` are NOT durable — there is
no DB row, no requeue, no recovery. For an Isol8 per-user container this means
any ECS task replacement / image deploy / OOM kill silently drops the user's
running agent with no terminal event ever delivered.

---

## (c) SSE delivery mechanism

Two distinct SSE paths:
- **Chat-completions streaming** (`_write_sse_chat_completion`, :1344-1494):
  bridges a **thread-safe `queue.Queue`** (`_stream_q`, :1143) filled by the
  agent's `stream_delta_callback`, drained on the event loop via
  `loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))` (:1416).
  Emits OpenAI `chat.completion.chunk` frames + custom
  `event: hermes.tool.progress` frames (:1398-1402). 30s keepalive (:1429).
  Client disconnect → `agent.interrupt()` + task cancel (:1460-1476).
- **Run events** (`_handle_run_events`, :3173-3220): subscribes to the
  per-run **`asyncio.Queue`** populated by `loop.call_soon_threadsafe(
  q.put_nowait, event)` from the worker thread (:2828, :2958, :3001). 30s
  keepalive (:3204-3206). On the `None` sentinel it writes `: stream closed`
  and pops the queue (:3208-3218).

**Spec-relevant surprise #3 (delivery is single-consumer / fan-in, not
fan-out):** `_handle_run_events` does `q = self._run_streams[run_id]` (:3189)
and then `await q.get()` (:3204) — a destructive read off a single
`asyncio.Queue`. There is NO multiplexing. If TWO clients (e.g. the desktop app
+ a browser tab, or Isol8's backend connection-pool + a debug probe) subscribe
to the same `run_id`, they **race and split the event stream** — each event
goes to exactly one consumer. And in `finally` the FIRST subscriber to
disconnect calls `self._run_streams.pop(run_id, None)` (:3217), tearing down
the queue out from under both the run worker AND any second subscriber. SSE
here assumes exactly one reader per run.

---

## (d) Daemon process model

- **One aiohttp `web.Application` per adapter** (:3398), started via
  `AppRunner` + `TCPSite` (:3469-3472), default bind `127.0.0.1:8642`
  (:57-58, :642-646).
- **One daemon per host (HERMES_HOME).** `gateway/run.py:17102` refuses to
  start a second gateway: `if not acquire_gateway_runtime_lock(): ... "Another
  gateway instance is already running (PID …)"` (run.py:16903-16908,
  status.py:421-466). `atexit.register(release_gateway_runtime_lock)`
  (run.py:17116).
- **Refuses non-loopback bind without a key** (:3434-3440) and refuses a
  placeholder key on a network bind (:3444-3456); fails fast on port conflict
  (:3459-3467). So to expose it to Isol8's backend across the container network
  you MUST set `API_SERVER_HOST=0.0.0.0` + a real `API_SERVER_KEY`.
- **Execution thread pool:** every agent run is dispatched with
  `loop.run_in_executor(None, ...)` (api_server.py:2791, :3053) — the
  interpreter's DEFAULT `ThreadPoolExecutor`. The code NEVER calls
  `set_default_executor` and NEVER sizes `max_workers` (grep across run.py +
  status.py: zero hits). So worker count = CPython default `min(32, os.cpu_count()
  + 4)` → on Isol8's 0.5-vCPU box that is effectively ~5 threads, but each agent
  run is CPU-light/IO-bound (LLM HTTP + tool subprocess), so threads are not the
  binding constraint — the `_MAX_CONCURRENT_RUNS=10` admission cap is.

---

## CONCURRENCY VERDICT (the critical question)

**ONE daemon CAN execute MULTIPLE runs concurrently — there is NO global lock
and NO single-active-run serialization on the `/v1/runs` or
`/v1/chat/completions` paths.** Evidence:
- `_handle_runs` fires `asyncio.create_task(_run_and_close())` and returns 202
  without awaiting (:3140-3156); N such tasks coexist.
- Each run's blocking work goes to the SHARED default thread pool via
  `run_in_executor(None, …)` (:3053) — independent threads, no mutex.
- The ONLY ceiling is the admission check `len(self._run_streams) >=
  _MAX_CONCURRENT_RUNS` with `_MAX_CONCURRENT_RUNS = 10` (:2797, :2874). So up
  to **10 concurrent runs in flight per daemon**; the 11th gets `429`.
- Per-run identity (approval/session contextvars) is bound INSIDE the worker
  thread via `copy_context`/contextvars precisely "so concurrent runs do not
  share process environment state" (:3018-3025, comment at :3019-3020) — the
  code is explicitly designed for concurrency.
- The `_running_agents`/drain/"already running" serialization in run.py
  (:2014, :6127, :6913) belongs to the CHAT-PLATFORM path (Telegram/Discord/
  native gateway), NOT the API-server adapter. The API server bypasses it
  entirely (`_run_agent` at :2734 / `_run_and_close` at :2975 never touch
  `_running_agents`).

**Caveat for the spec's scope estimate:** "concurrent" here is bounded at 10
and is per-DAEMON, but the daemon is a SINGLE-TENANT, single-shared-key
process whose run state is entirely volatile in-process. Isol8's model is
one container per user, so the 10-run cap is plausibly fine per user — BUT the
spec must NOT assume durable runs, multi-subscriber SSE, or any tenant
isolation inside one daemon. All three are absent in this basis.
