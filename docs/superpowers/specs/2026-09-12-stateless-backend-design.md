# Stateless backend — design (sub-project B)

Status: design approved in discussion 2026-09-12; written spec reviewed against the code
and the OpenClaw 2026.9.3 SDK (every claim below was checked, not assumed). Second of four
sub-projects that move the platform off its stateful backend core: A async turn contract
(`2026-09-12-async-turn-contract-design.md`, in execution), B this, C lifecycle as Step
Functions, D agents and devices off the API Gateway WebSocket door. B and D are planned
together and executed after A; B ships first because D consumes B's RPC route. Execution
runs under the ponytail rule (owner instruction 2026-09-12): the shortest working diff,
no unrequested abstraction.

## 1. Problem

The backend holds one persistent WebSocket per owner container
(`core/gateway/connection_pool.py`, ~3.2k lines) and keeps per-owner state in process
memory: connections, pending RPC futures, active runs, parked/reconnect/backoff state.
Every control RPC, the readiness signal, the trial-idle decision and non-room usage go
through that object, and the liveness row `readiness_state` reads is written only by a
successful pool connect. `service-stack.ts` therefore pins the service to
`desiredCount: 1`, and a rolling deploy is the only time two instances exist.

After sub-project A, room turns no longer need the pool: the container acks over HTTP,
publishes its frames to AppSync itself, and reports lifecycle and usage over a hook-token
route. What remains on the pool is the control plane, readiness, trial idle, usage for
runs that are not room turns, and three event families nobody consumes any more.

## 2. Principles

Inherited from A: exactly-once effects, at-least-once delivery; no clocks for
coordination; a dead remote is reported, never re-run; tenancy enforced where the
information is; fix forward, no compatibility paths.

Added for B:

- **No persistent socket from the backend to a container.** Every backend → container
  interaction is an HTTP request to the container's baked plugin (the one-shot
  operator-device pairing probe is D's to replace). Every container → backend interaction
  is a request to a hook-token route, or a direct AppSync publish.
- **Any instance serves any request.** Nothing a request needs lives in the process that
  received the previous one. State is DynamoDB, AppSync, or the container.
- **Delete, do not port.** A pool feature with no consumer after A is removed, not
  re-homed.

## 3. Scope and boundaries

### Removed

- `core/gateway/connection_pool.py` in full, with the `get_gateway_pool` /
  `get_gateway_pool_if_started` / `shutdown_containers` / `startup_containers` surface in
  `core/containers/__init__.py` and their `main.py` calls.
- `core/gateway/operator_rpc.py:gateway_rpc_oneshot` (every caller moves to
  `container_rpc.call`; `perform_signed_connect` stays, see "Untouched until D").
- `openclaw_work._dispatch_message`'s `pool.ensure_connection` warm-up (A's Task 12 left
  it with a note pointing here) and `routers/messaging.py`'s `get_gateway_pool` import.
- The pool's user-event forwarding and its whole rail, which has **no consumer**: no
  client subscribes to `runtime.approval.requested/resolved`,
  `runtime.question.requested/resolved` or `runtime.progress.changed`, and nothing calls
  the shared client's `resolveApproval` / `resolveQuestion` (checked across
  `apps/frontend`, `apps/mobile`, `packages/shared`). Delete: those five types from
  `USER_LIVE_EVENT_TYPES` (`core/services/messaging.py`) and from
  `packages/shared/src/live/types.ts` (+ `index.ts` exports); `runtime_rpc.execute_control`;
  the `/runtime/approvals/{id}/resolve` and `/runtime/questions/{id}/resolve` routes and
  their request/response models; the two client methods in
  `packages/shared/src/runtime/client.ts`. (The SDK exposes no in-process listener for
  gateway broadcasts, so a port would have needed a loopback socket; nothing justifies it.)
- The pool's `runtime.resource.invalidated` source (gateway `agents/config/models/skills/
  tools.changed` broadcasts). The backend's own emitters (`status_events.py`,
  `node_proxy.py`) stay; §5 adds the replacement for backend-driven mutations.
- Readiness liveness rows in `connection_service.py`: `mark_gateway_connected`,
  `mark_gateway_disconnected`, `refresh_gateway_liveness`, `get_live_gateway_conn_token`,
  `is_gateway_connected`, `_GATEWAY_LIVENESS_PREFIX`, `_LIVENESS_STALE_SECONDS`, and the
  module-level `is_gateway_connected` wrapper (only writer was the pool). The WS-door
  functions in that module stay (D).
- `routers/container.py`: `_pool_self_heal_connect`, `_self_heal_inflight`,
  `_self_heal_backoff_at`, `_self_heal_dial_allowed`, `_SELF_HEAL_BACKOFF_S`,
  `_safe_phase`, `_resolve_cold_start_phase`, the `GatewayReconnectThrottled` import, and
  the self-heal block in the status poll (its purpose was to create a pool connection).
- `core/services/cron_run_telemetry.py` and its `main.py` scheduling: the hourly
  `cron.runs` replay exists only because no socket saw cron runs end; §6's `agent_end`
  hook sees every run. (PostHog's uuid5 capture id keeps the overlap harmless during the
  roll.)
- Pool metrics and their alarms/widgets in `observability-stack.ts`: W6
  `gateway-health-check-timeout`, W8 `gateway-rpc-error-count`, W9 `chat-e2e-latency-p99`,
  W10 `chat-error-rate`, W11 `chat-session-usage-fetch-error`, W54
  `gateway-reconnect-throttle-storm`, and W7 `gateway-frontend-prune-storm` (its metric has
  had no emitter); the dashboard widgets over `chat.message.count`, `chat.error`,
  `chat.e2e.latency`, `gateway.connection.open`, `gateway.rpc.error`. `messaging.*` from A
  is the replacement. **P12 `backend-liveness` stays**: `main.py`'s
  `_running_count_gauge_loop` keeps emitting `gateway.running.count` from
  `container_repo.get_by_status("running")` (no pool needed); the pool-derived
  `gateway.connection.open/parked` gauges go.
- Tests whose subject is the pool: `tests/unit/gateway/*` except
  `test_session_key_parser.py` and `test_gate_chat_trial_cap.py` (their subjects move,
  and the tests move with them), `tests/unit/core/test_connection_pool_*`,
  `tests/unit/core/test_chat_event_transform.py`, `tests/gateway/*`,
  `tests/unit/routers/test_container_status_selfheal_dial.py`,
  `tests/chaos/fis/_chat_driver.py` (drives the retired browser socket). Every other
  test that patches `get_gateway_pool` / `send_rpc` (42 files reference the pool today)
  or `connection_service.is_gateway_connected` (5 files) patches `container_rpc.call`
  instead.

### Re-homed

| Today (on the pool) | After B |
|---|---|
| `send_rpc` in `runtime_rpc.execute_sync` (the `_PROXY_ALLOWED_METHODS` allowlist, unchanged) | `container_rpc.call`; guards and session filters unchanged |
| Literal `send_rpc` callers: `routers/admin.py` (`sessions.clear`), `routers/channels.py` (`channels.status`), `browser_state.py` (`browser.request`), `desktop_pair_live.py` (`node.pair.list/approve`, `device.pair.list/approve/remove`), `spend_service.py` (`models.authStatus`), `node_proxy.py` (`sessions.patch`), `admin_service.py` (`agents.list`, `agent.identity.get`, `skills.status`, `config.get`), `paperclip_agent_lifecycle.py` (`agents.list`, `agents.delete`), `paperclip_openclaw_pairing.pair_with_openclaw` (`agents.create`, `agents.update`; called from `catalog_service`, `company_deploy_service`, `routers/teams/agents.py`, `runtime_rpc._create_agent`, which pass it a pool today) | the same call through `container_rpc.call`; `pair_with_openclaw` loses its `gateway_pool` parameter |
| `gateway_rpc_oneshot` callers: `container_auth.py` (`models.authLogout`), `container_recover.py` (`config.apply` and its `_rpc` helper), `paperclip_openclaw_pairing._find_pending_request_id` (`device.pair.list`) | `container_rpc.call` |
| `_record_usage_from_session` / `_fetch_and_record_usage` / `_emit_run_completed` (lifecycle end → `sessions.list` → `record_usage` + `run_analytics`) | room runs: A's `run.ended` callback (done); every other run: §6 |
| `gate_chat` (a pool method that reads only `billing_repo` and the trial predicate; touches no pool state) | `core/services/chat_gate.py:gate_chat(*, user_id, owner_id, session_key)`, same body; callers `routers/messaging.py` (steer) and `routers/websocket_chat.py` |
| `_parse_session_key`, `parse_lightbulb_session_key` (module functions living in the pool file; imported by `operator_proxy.py`, `routers/messaging.py`, `tests/unit/gateway/test_session_key_parser.py`) | `core/services/session_keys.py:parse_session_key`, `parse_lightbulb_session_key`, test moved with them |
| `is_gateway_connected` → `readiness_state.resolve_runtime_phase` | §7 |
| `has_active_runs`, `is_parked`, `close_idle_user`, `get_live_gateway_conn_token` in `durable_activity.py` | §7 |
| `resolve_run`, `run_id_for_session`, `mark_run_active`, `subscribe_room_session`, room frame relay, `chat.side_result`, `sessions.changed` | deleted by A (Task 13) |

### Untouched until D

The machine WebSocket door and its relays: `routers/websocket_chat.py` (except its
`gate_chat` import), the `ws-*` Lambdas and authorizer, `node_proxy.py` (except its RPC
call), `operator_proxy.py` (except its `parse_session_key` import; its
`_tap_run_completion_telemetry` stays),
`core/gateway/node_connection.py`, and
`operator_rpc.perform_signed_connect` with its one caller
`paperclip_openclaw_pairing._probe_pairing_request` (it connects *as the agent's operator
device* to open a pairing window; that is the operator-device identity D redesigns). Their
in-process maps are why `desiredCount` stays 1 in prod until D.

### Untouched until C

Provisioning claims, the update worker and its scheduled passes, `try_claim_owner_deploy`,
the SFN watchdog handoff.

### Accepted per-instance state

`teams_event_broker.py` keeps one Paperclip client per active app user per instance (two
instances double-publish; clients dedupe by `eventId`). Typing and presence dedup maps
(`routers/messaging.py` `_typing_last`, `_presence_last`) stay per instance: a dot may
flicker across a deploy. `run_analytics._seen_run_ids` / `_run_actor`,
`product_analytics._daily_dedup_seen`, `wake_policy._suppressed_emitted` and
`routers/container.py._browser_restore_inflight` degrade to a duplicate product event,
metric sample or restore attempt. TTL read caches (JWKS, spend, ECS names) degrade to
redundant work. The SQS consumer runs on every instance by design.

## 4. Transport: the plugin RPC route

Upstream ships the exact precedent: `extensions/admin-http-rpc` registers
`POST /api/v1/admin/rpc` (`auth: "gateway"`, `match: "exact"`,
`gatewayRuntimeScopeSurface: "trusted-operator"`) and calls the SDK's
`dispatchGatewayMethod(method, params, {timeoutMs})` (`plugin-sdk/gateway-method-runtime`).
Dispatch is admitted only when the plugin manifest declares
`"contracts": {"gatewayMethodDispatch": ["authenticated-request"]}` (loader:
`canDispatchGatewayMethodsFromHttpRoute`), which stamps
`gatewayMethodDispatchAllowed: true` on the route's request scope. Its allowlist is a
compiled constant that lacks most of ours (`sessions.*`, `chat.history`,
`browser.request`, `skills.*`, `models.authLogout`, `doctor.*`, …), so B registers its own
route with the same shape:

Plugin (`apps/infra/openclaw/plugins/lightbulb-channel/src/rpc.ts`; manifest gains the
contract above). The route is registered once per process from the entry's
`registerFull(api)` through **`api.registerHttpRoute`** — not the SDK helper
`registerPluginHttpRoute` the channel routes use: only the plugin-API path consults the
manifest contract and stamps `gatewayMethodDispatchAllowed` (loader
`canDispatchGatewayMethodsFromHttpRoute`; the SDK helper copies
`gatewayRuntimeScopeSurface` and nothing else):

```
POST /plugins/lightbulb-channel/rpc      auth: gateway, match: exact, scope surface: trusted-operator
body    {method: string, params?: object, timeoutMs?: number}   timeoutMs ≤ 120000, default 30000
200     {ok: true, payload}                dispatch resolved
200     {ok: false, error: {code, message}} the gateway method returned an error (verbatim)
400     {ok: false, error: {code: "INVALID_REQUEST", message}} malformed body
504     {ok: false, error: {code: "AGENT_TIMEOUT", message}}   dispatch did not settle within timeoutMs
```

No plugin-side method allowlist: the route is reachable only with the gateway bearer
token (held by the backend and the container), and the backend's allowlists stay the
policy edge. The handler never logs `params`.

Backend (`core/services/container_rpc.py`):

```python
async def call(owner_id: str, method: str, params: dict | None = None, *, timeout_s: float = 30.0) -> Any
```

resolves the container with `runtime_rpc._running_container` (moved here; unchanged: wake
path, `RUNTIME_UNAVAILABLE` retryable when asleep), POSTs with the row's `gateway_token`,
and raises **exactly what `pool.send_rpc` raised** so callers' `except` chains keep
working: 504 → `asyncio.TimeoutError`; `ok: false` → `RuntimeError(json.dumps(error))`;
no container or transport failure → `ApiProblem("RUNTIME_UNAVAILABLE", retryable=True,
retry_after=3)` (the "waking" variant when the row is `stopped`, as `_running_container`
does today). A caller whose `except Exception` used to wrap unknown errors adds
`except ApiProblem: raise` above it so the waking message survives. No retries inside
`call`. Metrics `messaging.rpc {outcome: ok|error|timeout|unavailable,
method}` and `messaging.rpc.latency {method}` (method is a closed set of ~45 names).

Callers that today resolve `ip`/`token` themselves (`desktop_pair_live._pool_rpc`,
`container_recover._rpc`, `container_auth`) drop that and pass `owner_id`.

Scope coverage is a code fact, not a spike: a `trusted-operator` route receives
`CLI_DEFAULT_OPERATOR_SCOPES` = `operator.admin`, `operator.read`, `operator.write`,
`operator.approvals`, `operator.questions`, `operator.pairing`, `operator.talk.secrets`
(`plugin-route-runtime-scopes` → `method-scopes`), which is every scope an allowlisted
method can require. The runtime proof is the dev pass of §10 after the cutover deploy.

## 5. Resource invalidation for backend-driven mutations

`runtime_rpc.execute_sync` publishes `runtime.resource.invalidated {resource}` to the
owner's users after a successful call of a mutating method, with
`agents.create|agents.update|agents.delete → agents`, `config.set|config.patch → config`,
`skills.install|skills.update|skills.proposals.apply|skills.proposals.reject → skills`
(the clients' `RESOURCE_METHOD_PREFIXES` names). Publish failure is logged and never
fails the RPC. `agents.create` through the 202 operation path already reaches
`useAgents` via `runtime.operation.changed`. Container-originated changes (an agent
editing its own config) are no longer pushed; `useRuntimeRpc` keys already revalidate on
reconnect and the roster on a 30 s interval.

## 6. Usage and analytics for non-room runs

The plugin registers `api.on("agent_end", (event, ctx) => …)` (event: `{runId?, success,
error?, durationMs?}`; ctx: `{agentId?, sessionKey?}`): when `ctx.sessionKey`,
`ctx.agentId` and `event.runId` are all present and the key is not a Lightbulb room
session key (`agent:<agentId>:lightbulb:channel:<roomId>`, the shape the plugin itself
builds) it reads `getSessionEntry({agentId, sessionKey, readConsistency:
"latest"})` (the same read A's supervisor does) and POSTs
`{sessionKey, agentId, runId, success, error?, durationMs?, usage: {inputTokens,
outputTokens, cacheRead, cacheWrite, estimatedCostUsd, model}}` to
`POST /api/v1/internal/messaging/usage/{ownerId}` (hook token, three attempts like A's
callbacks; `error` is the gateway's classification string, never message text). The
backend route:

- ignores a room session key defensively (A's `run.ended` recorded it);
- claims a receipt `OWNER#{ownerId}` / `USAGE#{runId}` (conditional put, 7-day TTL,
  `messaging_repo.claim_usage_receipt(owner_id, run_id) -> bool`); a replay is a 200 no-op;
- records `record_usage(owner_id, user_id=owner_id, …, room_turn=False, event_kind=None)`
  (a cron, heartbeat or Paperclip turn has no dispatching member), skipping zero-token
  zero-cost runs exactly as A does;
- calls `run_analytics.schedule_run_completed(owner_id=…, parsed=parse_session_key(
  sessionKey), payload={"runId": runId, "data": usage}, phase="end" if success else
  "error")` — the same function the pool and the cron sweep call, so the product event
  is identical by construction.

Billing truth stays the Bedrock meter Lambda; this is analytics only.

## 7. Readiness, trial idle

**Readiness.** `readiness_state.compute_status` obtains `gateway_connected` by probing the
container: `container_rpc.call(owner_id, "health", timeout_s=2.0)` succeeds → `True`; any
exception → `False` (logged at info, never breaks `/status`). The unconditional early
read of `is_gateway_connected` moves into the branch that already knows the row is
`running` and not parked, immediately before `resolve_runtime_phase`, which is unchanged
(`running` + connected → `ready`, else `starting`). Cost: one Cloud Map lookup and one
in-VPC HTTP per status poll of a running row, the same cost every runtime RPC already
pays. The self-heal dial in `routers/container.py` disappears with the pool (there is no
connection to create).

**Trial idle.** `durable_activity.sweep_trial_idle_once` keeps its structure and replaces
the pool vetoes: `pool.has_active_runs(owner) or pool.is_parked(owner)` →
`await messaging_repo.has_open_runs(owner_id)` (one query on `OWNER#{ownerId}` with
`begins_with(sk, "RUN#")` and `status <> ended`, bounded by the 7-day TTL);
`pool.close_idle_user(owner)` and both `get_live_gateway_conn_token` checks are deleted
(no connection to close, no events to drain). `last_interactive_at`, the operator
connection check and the idle-stop claim are unchanged. A cron or heartbeat turn on a
trial container has no ledger row and can be stopped after the idle window, as today.

## 8. Failure handling

| Failure | Detected by | Outcome |
|---|---|---|
| container unreachable or asleep on an RPC | `_running_container` / transport error | `RUNTIME_UNAVAILABLE`, retryable; queue consumers retry through SQS, UI callers show the problem (as today) |
| dispatch exceeds `timeoutMs` | plugin route 504 | `asyncio.TimeoutError` → callers' existing `RUNTIME_UNAVAILABLE` branch |
| gateway method error | `ok: false` | `RuntimeError` with the gateway's error, as `send_rpc` raised |
| usage callback lost after three attempts | plugin log | counters miss one run; the meter Lambda still bills |
| duplicate usage callback | receipt row | 200 no-op |
| readiness probe fails on a healthy container | 2 s timeout | phase `starting` for one poll; the next poll re-probes |
| invalidation publish fails | logged | the RPC still succeeds; the panel refreshes on reconnect |
| backend deploy mid-RPC | client sees the ALB reset | the caller retries; the container holds no backend-side state |
| two instances serve one owner | — | every route is a function of its inputs and DynamoDB; §3 lists the per-instance state that tolerates duplication |

## 9. Rollout

A's shape, no compatibility path:

1. Plugin PR: manifest contract, RPC route, `agent_end` usage hook. Merge builds the
   image; nothing runs it.
2. Cutover PR: `dev.tag`/`prod.tag` pinned; `container_rpc`; every call site switched;
   the usage route + receipt; `chat_gate`; `session_keys`; readiness probe;
   `durable_activity` on the ledger; the deletions of §3; observability edits;
   CLAUDE.md's Architecture intro, `main.py` paragraph, Gateway and Container
   Orchestration tables, Messaging paragraph ("until sub-project B"), WebSocket
   Architecture item 4, Usage Flow, and the `service-stack.ts` row rewritten. Per
   environment: deploy, roll the fleet immediately, redrive the DLQ if anything
   dead-lettered during the roll.
3. Dev only, after the roll: `desiredCount: 2` by a manual `update-service` on dev (never
   on prod); run §10; then back to 1 until D.
4. `desiredCount` in prod moves in D's cutover.

## 10. Testing

- Unit: `container_rpc.call` (each mapping in §4); the usage route (hook-token gate,
  room-session ignore, receipt dedup, zero-usage skip, analytics call); `chat_gate`;
  `session_keys`; `execute_sync` invalidation publish (mutating vs read method, publish
  failure swallowed); readiness probe true/false/exception; `durable_activity` with
  `has_open_runs`; the plugin route (body validation, timeout, error passthrough, no
  `params` in logs) and the usage hook (room key skipped, retry, finality).
- Integration on dev at `desiredCount: 2`: a room turn end to end; a config change from
  the UI reflected on another tab within the 10 s throttle; a cron run's usage recorded
  once; a trial container stopped only when idle by the ledger; a rolling deploy during a
  turn, nothing lost; both instances observed serving requests (ALB target logs).
- Instruments: `messaging.run.*` (A), `messaging.rpc`, `messaging.rpc.latency`,
  `messaging.usage.callback {outcome}`.

## 11. Success criteria

- `core/gateway/connection_pool.py` does not exist; `git grep get_gateway_pool` and
  `git grep gateway_rpc_oneshot` are empty.
- Dev runs two backend tasks for the §10 checks with no failed request.
- A rolling deploy loses no usage record and no RPC beyond one retry.
- The backend opens no WebSocket to any container except the operator-device pairing
  probe (D).

## 12. Interface for D

D's Paperclip operator path and node/local-session relays leave the WebSocket door. Where
they need a container RPC, they use `container_rpc.call`. Where they need container →
backend events, they add a hook-token route with a consumer, as §6 does. D adds no second
transport and replaces `perform_signed_connect`.

## 13. Out of scope

Provisioning, update worker and claims (C); the WebSocket door and its relays (D); the
authored workspace on EFS (working-trees spec R1: stays); Teams broker consolidation;
typing and presence in DynamoDB; autoscaling policy beyond the dev proof; a user surface
for exec approvals and questions raised inside container runs (none exists today; if one
is wanted it is a new feature on a hook-token route, not a port of the pool's).
