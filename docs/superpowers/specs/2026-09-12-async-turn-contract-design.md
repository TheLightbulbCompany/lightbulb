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

`workspaceId` is an opaque UUID minted lazily, once per owner, as the messaging-table
row `OWNER#{ownerId}` / `WORKSPACE {workspaceId}` (`messaging_repo.ensure_workspace_id`)
and stamped as `workspaceId` on every `ROOM#{roomId}/META` and `STREAM#{key}/META` row at
creation (a one-shot backfill stamps existing rows). Clerk ids never appear in paths.
`deliveryKey` and `userKey` keep their current rotation semantics.

Authorization in the `ws` namespace handler:

- `onSubscribe` (OIDC only): member row exists for `sub` in the room, room META's
  `deliveryKey` matches the path (rooms); user-stream mapping active and owned by
  `sub` (users). Same logic as today, new path shape. IAM subscribers are refused.
- `onPublish` (IAM only): OIDC publishers are refused by the namespace auth mode. The
  handler reads the same META row the subscribe path reads (room META or stream META),
  refuses unless its `workspaceId` equals the path segment, and — when the caller ARN is
  an assumed role named `lightbulb-{env}-owner-bedrock-{ownerId}` — refuses unless the
  row's `ownerId` equals that `ownerId`. Any other IAM caller that IAM admitted (backend
  service role, projector role) may publish to any workspace. The handler's only data
  source stays the messaging table. Room channels additionally require the current
  `deliveryKey`; user channels require `active`.

IAM: the backend service role and the projector get `appsync:EventPublish` on the `ws`
namespace ARN. Every per-owner container task role gets the same action on
`arn:aws:appsync:{region}:{account}:apis/*/channelNamespace/ws` through the CDK-managed
`OwnerBedrockBasePolicy` (already attached to every owner role) and the
`OwnerBedrockBoundary`. No per-owner inline policy and no backfill.

Open verification (task 1 of the plan): confirm the exact `ctx.identity` field that
carries the IAM caller ARN in an Events handler by publishing once from a
container-shaped role into a scratch handler that logs `ctx.identity`.

## 4. Turn contract

### Dispatch (backend SQS consumer)

1. Read the `WORK#` pointer; resolve the running container (wake path unchanged).
2. `runId = uuid5(RUN_NS, "{agentId}:{messageId}")` — deterministic, so a redelivered
   message lands on the same ledger row. Conditional-put the row with
   `status=dispatched`; an existing row means redelivery and dispatch proceeds.
3. POST the ingress body to `/plugins/lightbulb-channel/inbound/{agentId}` with one
   required object `run: {runId, workspaceId, frameChannel, frameEndpoint}`
   (`frameChannel` is the exact room channel path, `frameEndpoint` the AppSync Events
   HTTP endpoint). A body without `run` is a 400; there is no compatibility path.
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
| `run.ended` | terminal | `outcome` ∈ completed \| error \| aborted \| ignored \| gate_error, `errorKind`, `usage {model, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, estimatedCostUsd}` — required unless the outcome is `ignored` or `gate_error` |

The plugin reads `usage` from its own session store entry (`getSessionEntry`, the row
the gateway patches at run end), so the backend records usage straight from the
callback and never calls `sessions.list` on the turn path. An unknown `runId` is a 404;
a replayed callback is a 200 no-op. A failed run pushes a notification to the
triggering member from the same callback; a completed run's reply is already pushed by
the projector.

### Frames (container → AppSync)

Published by the container to `frameChannel` with its task role. Same vocabulary the
clients consume today (`run_active`, `thinking`, `chunk`, `tool_start`,
`tool_progress`, `tool_end`, `done`, `run_ended`, `error`), every frame stamped with
`runId`. Coalesced: at most one publish per 100 ms carrying up to 5 events. On AppSync
429 the plugin drops `chunk` and `thinking` frames first and always delivers lifecycle
frames; a run never fails because of streaming. Usage is never derived from frames.

### Final reply

Unchanged: the plugin POSTs to `/internal/messaging/agent-messages/{ownerId}` (hook
token); the body carries `runId` whenever the send belongs to an in-flight run (the
agent's `message` tool can also send outside one, so the field is optional). A present
`runId` is verified against the ledger — it must be this owner's run for this room and
agent — or the receipt is refused with `RUN_NOT_FOUND`. A run that already closed still
accepts its late reply (durable retries, sweeper closures): delivery is at-least-once.
The receipt fence stays; the backend no longer guesses a run id from gateway state.

### Run controls

New plugin routes, gateway-bearer auth: `POST /plugins/lightbulb-channel/runs/{runId}/abort`
and `POST /plugins/lightbulb-channel/runs/{runId}/steer {text}`. The backend's
`abort_run` and `steer_run` call them over HTTP. `sessions.messages.subscribe` for
rooms is removed.

## 5. Run ledger

Row `OWNER#{ownerId}` / `RUN#{runId}` in the messaging table (owner-keyed because every
reader — callback route, abort, steer, status — knows the owner but not the room):

`runId, ownerId, roomId, workspaceId, agentId, triggerMessageId, eventKind, status
(dispatched|started|ended), outcome (completed|error|aborted|ignored|gate_error|
not_admitted), errorKind, dispatchedAt, startedAt, endedAt, lastHeartbeatAt, usage,
sessionKey, sweep ("open" while not ended), deadlineAt (epoch seconds), expiresAt (7 days)`.

A sparse GSI `by-run-deadline` (`sweep`, `deadlineAt`) lets the sweeper query open runs
past their deadline without scanning.

Transitions are conditional writes: `started` only from `dispatched`; `heartbeat`
only while not `ended`; `ended` once. Replayed callbacks are no-ops returning 200.
`GET /messaging/runs/{runId}`, the room "Working" state, `messaging.reply.latency`
and usage analytics read this row. No run state lives in process memory.

Sweeper (backend, one-minute tick, the only clock): `dispatched` older than 120 s
without `started` → `ended/error(agent_did_not_start)`; `started` with
`lastHeartbeatAt` older than 90 s → `ended/error(agent_stopped_responding)`. Both
conditional; both publish an `error` frame followed by a `run_ended{error}` frame from
the backend (the terminal pair the clients already render).

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

No compatibility paths and no feature flag: there are no external users, and every
prod container is rolled in the same sitting as the deploy.

1. Identity spike (half a day). Result recorded in this spec.
2. Plugin PR: run contract required on ingress, immediate ack, background turn,
   callbacks with usage, direct frames, abort/steer routes. Merging builds the image
   tag; nothing runs it yet.
3. Cutover PR (one change, or a short stack merged back to back): `dev.tag` and
   `prod.tag` pinned to that image; `ws` namespace + handler + IAM + `by-run-deadline`
   GSI + queue redrive, `rooms`/`users` deleted; backend ledger, callback route,
   sweeper, ledger-backed run status, consumer without visibility extension,
   abort/steer over HTTP, and deletion of the relay (`subscribe_room_session`,
   `_room_session_subs`, `_publish_room_frame`, the room branches of `_active_runs`);
   publisher, projector, live-config and the web/mobile clients on the new paths.
   Before each environment's deploy, the one-shot backfill stamps `workspaceId` on
   its existing rows (script deleted afterwards).
4. Per environment: deploy, then roll the fleet immediately (`POST /container/updates
   {owner_id:"all", update_type:"task_definition_refresh"}`). A pointer that reaches a
   not-yet-rolled container during the roll is answered 400, retried five times at
   60 s and dead-lettered; `scripts/openclaw_work_redrive.py` replays the DLQ once the
   roll is complete. Dev first, verified; then one prod dispatch and the prod roll.

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
