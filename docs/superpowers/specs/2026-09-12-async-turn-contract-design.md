# Async turn contract and workspace-scoped channels — design (sub-project A)

Status: approved in discussion 2026-09-12. First of four sub-projects that move the
platform off its stateful backend core: A (this), B stateless backend, C lifecycle as
Step Functions, D agents and devices off the API Gateway WebSocket door. Order agreed:
A → B, C in parallel with A, D last.

## 1. Problem

An agent turn in a room is dispatched by an HTTP POST from the backend to the
container's plugin ingress that only returns when the turn ends (minutes). To survive
that, the SQS consumer extends message visibility every 120 s and the queue allows 360
receives; the backend also holds a persistent WebSocket to every container to receive
run frames, keeps per-run state in process memory (`_active_runs`,
`_room_session_subs`, `_pending_rpcs`), and is therefore pinned to one instance.
Room run frames reach clients only by transiting the backend.

## 2. Principles

- **Exactly-once effects, at-least-once delivery.** Every write that a retry could
  repeat is keyed (idempotency key or `runId`) and conditional. Delivery is retried
  until acknowledged. Nothing is dropped silently.
- **No clocks for coordination.** No leases, heartbeat-elected owners or stale
  reapers. The one permitted timeout is detecting a dead remote (a run that never
  started or stopped heartbeating), and it produces a visible error, never a re-run.
- **A run that dies mid-turn is an error.** The platform cannot make third-party side
  effects idempotent, so it does not re-run turns; it reports them.
- **Tenancy is enforced where the information is.** IAM sees the namespace; the
  namespace handler sees the caller and the channel path; the handler decides.
- **Fix forward.** There are no external users; old channel paths and namespaces are
  removed in the same change that introduces the new ones.

## 3. Channel model

One AppSync Events namespace, `ws`. The existing `rooms` and `users` namespaces are
deleted.

| Channel | Publishers | Subscribers | Carries |
|---|---|---|---|
| `/ws/{workspaceId}/rooms/{roomId}/{deliveryKey}` | backend (message events), container (run frames) | room members via Clerk OIDC | room message events, run frames |
| `/ws/{workspaceId}/users/{userKey}` | backend, container | one human via Clerk OIDC | message-available, approvals, questions, progress, subscription-rotated |

`workspaceId` is an opaque UUID minted once per workspace and stored on the containers
row (`workspace_channel_id`); Clerk ids never appear in paths. `deliveryKey` and
`userKey` keep their current rotation semantics.

Authorization in the `ws` namespace handler:

- `onSubscribe` (OIDC only): member row exists for `sub` in the room, room META's
  `deliveryKey` matches the path (rooms); user-stream mapping active and owned by
  `sub` (users). Same logic as today, new path shape. IAM subscribers are refused.
- `onPublish` (IAM only): OIDC publishers are refused. The backend service role may
  publish to any workspace. Any other IAM caller must be a container task role: the
  role name embeds the owner id; the handler does one consistent read of the
  messaging-table row `WS#{workspaceId}` / `META` (written at provision, carries
  `ownerId`) and refuses unless that `ownerId` equals the one parsed from the caller
  ARN. The handler's only data source stays the messaging table. Room channels
  additionally require the current `deliveryKey` (existing check).

IAM: the backend service role and every per-owner container task role get
`appsync:EventPublish` on the `ws` namespace ARN only (`apis/{id}/channelNamespace/ws`).
The container permission boundary (`OwnerBedrockBoundary`) gains the same statement.

Open verification (task 1 of the plan): confirm the exact `ctx.identity` field that
carries the IAM caller ARN in an Events handler by publishing once from a
container-shaped role into a scratch handler that logs `ctx.identity`.

## 4. Turn contract

### Dispatch (backend SQS consumer)

1. Read the `WORK#` pointer; resolve the running container (wake path unchanged).
2. Mint `runId` (ULID). Conditional-put the run ledger row with `status=dispatched`.
3. POST the ingress body to `/plugins/lightbulb-channel/inbound/{agentId}` with three
   new fields: `runId`, `workspaceId`, `frameChannel` (the exact room channel path).
4. Delete the SQS message on 200/202/409. On timeout or 5xx, leave it for redelivery.
   Visibility timeout 60 s, no extension, `maxReceiveCount` 5.

### Plugin ingress response

- `202 {runId, status: "accepted"}` after the effect-once claim on `eventId` and
  structural admission (member, kind). Returned within milliseconds; the ambient LLM
  gate and the turn itself run after the ack.
- `200 {runId, status: "replayed"}` for a duplicate `eventId`.
- `409 {error: "not_admitted"}` for permanent synchronous refusals (bot-loop guard,
  non-member).

### Callbacks (container → backend)

`POST /api/v1/internal/messaging/runs/{ownerId}/events`, hook-token auth, body
`{runId, type, at, ...}`, at-least-once, idempotent.

| type | when | payload |
|---|---|---|
| `run.started` | gateway session accepted the turn | `sessionKey` |
| `run.heartbeat` | every 30 s while running | — |
| `run.ended` | terminal | `outcome` ∈ completed \| error \| aborted \| ignored \| gate_error, `errorKind`, `usage {model, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}` |

### Frames (container → AppSync)

Published by the container to `frameChannel` with its task role. Same vocabulary the
clients consume today (`run_active`, `thinking`, `chunk`, `tool_start`,
`tool_progress`, `tool_end`, `done`, `run_ended`, `error`), every frame stamped with
`runId`. Coalesced: at most one publish per 100 ms carrying up to 5 events. On AppSync
429 the plugin drops `chunk` and `thinking` frames first and always delivers lifecycle
frames; a run never fails because of streaming. Usage is never derived from frames.

### Final reply

Unchanged: the plugin POSTs to `/internal/messaging/agent-messages/{ownerId}` (hook
token); the body now carries `runId` explicitly. The receipt fence stays.

### Run controls

New plugin routes, gateway-bearer auth: `POST /plugins/lightbulb-channel/runs/{runId}/abort`
and `POST /plugins/lightbulb-channel/runs/{runId}/steer {text}`. The backend's
`abort_run` and `steer_run` call them over HTTP. `sessions.messages.subscribe` for
rooms is removed.

## 5. Run ledger

Row `ROOM#{roomId}` / `RUN#{runId}` in the messaging table:

`runId, roomId, workspaceId, agentId, triggerMessageId, eventKind, status
(dispatched|started|ended), outcome, errorKind, dispatchedAt, startedAt, endedAt,
lastHeartbeatAt, usage, sessionKey, ttl (7 days)`.

Transitions are conditional writes: `started` only from `dispatched`; `heartbeat`
only while not `ended`; `ended` once. Replayed callbacks are no-ops returning 200.
`GET /messaging/runs/{runId}`, the room "Working" state, `messaging.reply.latency`
and usage analytics read this row. No run state lives in process memory.

Sweeper (backend, one-minute tick, the only clock): `dispatched` older than 120 s
without `started` → `ended/error(agent_did_not_start)`; `started` with
`lastHeartbeatAt` older than 90 s → `ended/error(agent_stopped_responding)`. Both
conditional; both publish a single `run_ended{error}` frame from the backend.

## 6. Failure handling

| Failure | Detected by | Outcome |
|---|---|---|
| container unreachable/asleep at dispatch | POST fails / wake needed | message stays queued; wake requested |
| permanent refusal | 409 | ledger `ended/not_admitted`; message deleted; no frame |
| accepted, never starts | sweeper | `ended/error`, one error frame |
| dies mid-turn | sweeper | `ended/error`, one error frame, no re-run |
| container restarts, dispatch replayed | plugin effect-once | `replayed`, no second turn |
| reply re-posted after restart | receipt fence | original returned, no second message |
| callback lost or duplicated | at-least-once + conditional writes | replay no-op; lost `run.ended` closed by sweeper as error |
| delivery key rotates mid-run | AppSync refuses the container's publish | tail of stream lost; final message and ledger unaffected |
| backend deploy mid-turn | — | nothing in process memory; callbacks land on any instance |
| AppSync 429 | plugin | chunks dropped first; lifecycle frames kept |

## 7. Rollout

1. Identity spike (half a day). Result recorded in this spec.
2. Infra: `ws` namespace + handler + IAM (boundary and per-owner policies); delete
   `rooms`/`users`. `workspace_channel_id` is minted at provision and written both to
   the containers row and as the messaging-table row `WS#{id}`/`META {ownerId}`; a
   one-shot backfill does the same for existing containers (script deleted after it
   runs). Backend publisher, projector, web and mobile clients switch paths in the
   same PR set.
3. Backend: run ledger, callback route, sweeper, ledger-backed run status, consumer
   without visibility extension, abort/steer over HTTP. The WebSocket frame relay
   stays behind a per-owner flag on the containers row (`turn_contract_v2`), default
   off.
4. Plugin: immediate ack, background turn, callbacks, direct frames, abort/steer
   routes. Image build → dev pin → dev verification → prod pin → fleet roll.
5. Flip `turn_contract_v2` per owner as containers reach the new image; when the
   fleet is on it, delete the relay path, `_room_session_subs`, `_active_runs`'
   room branches, and the visibility-extension code.

## 8. Testing

- Unit: every ledger transition and its rejection; sweeper thresholds; consumer
  delete/redeliver decisions per response class; handler tenancy (ARN parsing,
  workspace mismatch, delivery-key mismatch, OIDC publisher refused).
- Contract: a fake plugin server exercising 202/200-replayed/409, callback ordering,
  duplicate callbacks, a lost `run.ended`.
- Integration on dev: one turn end to end; kill the container mid-turn (one error
  frame, no duplicate reply); restart it (replay, no second turn); roll the backend
  mid-turn (no loss); rotate a delivery key mid-turn (tail lost, ledger closes).
- Acceptance instruments: `messaging.reply.latency` p95, `messaging.dispatch.targets`
  bucket-0 share, `messaging.work.queue_wait` p99 (alarm tightened to 30 s after
  cutover), `messaging.worker.alive`, `messaging.projector.push`.

## 9. Success criteria

- No request on the turn path outlives a few seconds; queue-wait alarm at 30 s.
- A backend deploy during an active turn loses no frames and no reply.
- A container restart mid-turn yields exactly one error frame and zero duplicate replies.
- Backend CPU no longer scales with streamed tokens.

## 10. Out of scope

Generic RPC-over-HTTP route and deleting the connection pool (B); Step Functions
lifecycle and rehoming the update worker's cron passes (C); Paperclip operator and
desktop relays (D); personal chat (already on rooms); config-as-data (dropped).
