# Native multiplayer backend implementation plan

> **For agentic workers:** use `superpowers:subagent-driven-development` for this
> packet. Preserve every existing trust-boundary guard while moving it out of the
> browser WebSocket router, then delete the human browser path in this PR.

**Goal:** Make Lightbulb the canonical multiplayer room service, expose HTTP
commands, dispatch ordered room work to native OpenClaw channel sessions, and
publish all browser-facing live projections through AppSync.

**Architecture:** Authenticated HTTP transactions write the canonical DynamoDB
room log. A single backend worker consumes pointer-only FIFO work, drains each
agent's room cursor in order, wakes the owner's shared OpenClaw runtime, and calls
the first-party `lightbulb` plugin. Existing gateway events are transformed once
and published to AppSync rather than API Gateway browser connection IDs.

**Tech stack:** FastAPI/Pydantic, asyncio, boto3/botocore, DynamoDB transactions,
SQS FIFO, AppSync Events HTTP publish with IAM SigV4, existing OpenClaw gateway
connection pool, pytest.

**Spec:**
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`

## Ownership and prerequisites

This packet owns:

```text
apps/backend/core/config.py
apps/backend/core/containers/config.py
apps/backend/core/repositories/container_repo.py
apps/backend/core/repositories/messaging_repo.py
apps/backend/core/services/messaging.py
apps/backend/core/services/live_events.py
apps/backend/core/services/openclaw_work.py
apps/backend/core/services/runtime_rpc.py
apps/backend/core/services/human_broker_provisioning.py (delete)
apps/backend/routers/messaging.py
apps/backend/routers/runtime_rpc.py
apps/backend/routers/webhooks.py
apps/backend/core/gateway/connection_pool.py
apps/backend/core/gateway/native_rooms.py (delete)
apps/backend/core/repositories/room_read_cursor_repo.py (delete)
apps/backend/routers/websocket_chat.py
apps/backend/core/containers/ecs_manager.py
apps/backend/core/services/teams_event_broker.py
apps/backend/core/services/status_events.py
apps/backend/main.py
apps/backend/core/services/account_wipe.py
focused backend tests for those paths
```

It must not edit AWS CDK, the OpenClaw plugin/image, or frontend code. It can
develop against environment/config fakes before the AWS and image PRs merge.

The AWS packet must eventually supply:

```text
MESSAGING_TABLE
OPENCLAW_WORK_QUEUE_URL
APPSYNC_EVENTS_HTTP_ENDPOINT
APPSYNC_EVENTS_REALTIME_ENDPOINT
APPSYNC_EVENTS_REGION
```

The plugin packet must supply
`POST /plugins/lightbulb-channel/inbound/<agentId>` on the existing OpenClaw
gateway port with gateway-token authentication, and posts outbound messages to
`POST /api/v1/internal/messaging/agent-messages/<ownerId>` with the existing
per-container hook token.

## Frozen Python models

Define only once, in `core/services/messaging.py`, and export response projections
through `routers/messaging.py`. Implement every request/response DTO and status/code
mapping frozen in architecture spec section 6; do not create router-local aliases:

```python
RoomKind = Literal["channel", "dm", "group-dm"]
MemberKind = Literal["human", "agent"]
ListenMode = Literal["active", "mentions", "observe"]
AuthorKind = Literal["human", "agent", "system"]

class CanonicalMessage(BaseModel):
    message_id: UUID
    room_id: UUID
    message_seq: int
    room_revision: int
    author: Author
    text: str
    mentions: list[MemberRef]
    reply_to_message_id: UUID | None
    thread_root_message_id: UUID | None
    created_at: datetime
    run_id: str | None
```

Use Pydantic camelCase aliases and `serialize_by_alias=True`; the exact public
shape is `CanonicalRoomMessage` in the frozen spec. Stored message items
additionally contain `ownerId`, `dispatchTargets`, request hash, and optional
source agent ID. Never return owner/container/gateway/outbox fields in the public
projection.

## Task 1: Implement the canonical repository

Create `apps/backend/core/repositories/messaging_repo.py` and
`apps/backend/tests/unit/repositories/test_messaging_repo.py`.

- [ ] Use the existing DynamoDB resource/client construction and table-prefix
  patterns. Reject startup in deployed environments when `MESSAGING_TABLE` is
  empty.
- [ ] Implement strongly consistent `get_room`, `get_member`, `get_message`, and
  `get_agent_delivery` reads.
- [ ] Implement paginated `list_rooms_for_member` through GSI `by-member`, then
  batch-read room `META` items. Treat the GSI as discovery only; every protected
  room operation rechecks the base membership row.
- [ ] Implement `list_messages(room_id, after_seq, limit)` as an ascending,
  strongly consistent base-partition query over `MSG#`; enforce `1 <= limit <=
  100` and return `nextAfterSeq`.
- [ ] Implement room creation as one transaction containing `META`, owner catalog,
  creator membership, requested validated member rows, agent delivery rows at
  sequence zero, and a durable idempotency row. Cap founder-release rooms at 32
  total members. Assert the exact action formula before sending:
  `fixedActions + memberRows + agentDeliveryRows <= 100`; the 32-member worst
  case uses 66 actions.
- [ ] Implement message creation with a strongly read `lastSeq` and conditional
  transaction: increment `META`, put immutable `MSG#<20-digit-seq>`, put `IDEMP`,
  and put one `WORK#MESSAGE#<seq>#AGENT#<agentId>` outbox row per dispatch target.
  Each work row stores the frozen pointer body plus SHA-256 FIFO group/dedup IDs
  (`room|ownerId|roomId|agentId` and `message|messageId|agentId`) and seven-day
  TTL. Store SHA-256 of canonical request fields. Same key/hash
  returns the original message; same key/different hash raises
  `IDEMPOTENCY_CONFLICT`.
- [ ] Implement member add/update/remove. Human removal rotates `deliveryKey` in
  the same transaction and writes one `LIVE#<eventId>#USER#<userId>` outbox row
  per affected human: `room.subscription.rotated` for remaining humans and
  `room.access.removed` for the removed human. Agent addition creates its delivery
  cursor at current `lastSeq`, so it does not replay the room's entire history as
  new work. Every `WORK#` and `LIVE#` row carries numeric epoch-seconds
  `expiresAt` seven days after creation.
- [ ] Implement conditional `advance_agent_delivery(room, agent, expected,
  through)` so concurrent/retried workers cannot skip or move a cursor backward.
- [ ] Implement read-cursor upsert bounded by current `lastSeq`.
- [ ] Implement opaque user event-channel allocation as one transaction with
  `OWNER#<owner>/USERSTREAM#<user>` -> current `channelKey`/`active` and the
  reverse `STREAM#<uuid>/META` -> owner/user/active item used by AppSync
  authorization. The same active user/owner returns the same live key; browser
  code never computes it from a Clerk ID. In the existing Clerk
  `_handle_organization_membership_deleted` path in `routers/webhooks.py`, query
  that human's room discoveries, strongly recheck each base membership, remove it,
  rotate every affected room key, and transact the normal removal/rotation `LIVE`
  rows. Only after all rooms are processed, mark both user-stream mappings
  inactive. Make repeated webhook delivery idempotent. A later rejoin allocates a
  new UUID rather than reactivating the old channel.

Write the focused tests with the implementation and let backend CI execute them.
Cover transaction shape, 20-digit ordering,
concurrent sequence retry, durable idempotency, hash conflict, strong membership
reads, GSI distrust, the 32-member/action bound, transactional work/live outboxes,
human-removal key rotation, agent joined-sequence cursor, cursor compare-and-set,
camelCase serialization, user channel stability, organization deletion across
multiple rooms, old room/user channel silence, seven-day outbox TTLs, and
fresh-key allocation after rejoin.

Commit as `backend: add canonical multiplayer repository`.

## Task 2: Implement room policy and target selection once

Create `apps/backend/core/services/messaging.py` and
`apps/backend/tests/unit/services/test_messaging.py`.

- [ ] Resolve current owner and human ID only from the existing authenticated
  Clerk dependency. Ignore/reject any body field named owner, author, session,
  container, gateway, delivery key, dispatch target, or sequence.
- [ ] Resolve human display snapshots through the existing Clerk metadata helper;
  resolve canonical agent UUIDs/display names from the current Paperclip/OpenClaw
  roster. Derive the frozen unique mention handle over the canonical-ID-sorted
  roster, store/expose it on agent memberships, and reject client-supplied handles.
- [ ] Require base-table membership for private operations. A room member can read
  and post. Owner/admin can mutate room metadata/membership. A removed member gets
  `ROOM_FORBIDDEN` even if a stale GSI result still lists the room.
- [ ] Validate names, text, member counts, mention IDs, reply/thread references,
  and room ownership. Enforce a 100,000-byte UTF-8 text cap for both human and
  internal agent writes. Mentioned IDs must be current room members; reply targets
  must be in the same room. A thread is a message subview rooted by
  `threadRootMessageId`, never a fourth room kind.
- [ ] Compute immutable dispatch targets at message commit:

  | Author/event | Agent target | Kind |
  | --- | --- | --- |
  | Human mentions agent | mentioned agent | `user_request` |
  | Human does not mention active listener | active agent | `room_event` |
  | Human does not mention mentions/observe agent | none | none |
  | Agent mentions peer | mentioned peer, never self | `user_request` |
  | Agent posts to room with active peer | active peer, never self | `room_event` |

  Deduplicate targets by agent ID; an explicit mention wins over ambient.
- [ ] Store sender kind/ID, event kind, and source agent so the OpenClaw plugin can
  activate native bot-loop protection. Do not implement a custom response/action
  interpreter or synthetic model prompt.
- [ ] For internal agent writes, derive mentions only from exact case-insensitive
  `@<mentionHandle>` token-boundary matches against current room agent members.
  Resolve the unique handle to the canonical UUID; absent/ambiguous matches produce
  no mention. The 9.3 outbound adapter supplies no mention array or run ID; store
  `runId: null`, map its `replyToMessageId`/`threadRootMessageId`, and let the same
  target-selection code dispatch any resolved peer mentions.
- [ ] On membership key rotation, commit the frozen `LIVE` rows in the membership
  transaction. Do not publish directly from the request path or ever reuse the
  removed user's old room channel.
- [ ] Return the frozen error shape with HTTP status 400/403/404/409/503 as
  appropriate.

Tests cover Friday+Gauge parallel mentions, ambient listeners, observe silence,
agent-to-agent visibility, source-agent omission, mention precedence, stale
membership, unique/colliding/fallback mention handles, agent-authored friendly
mention parsing, reply isolation, deterministic dispatch target storage, and
room-key rotation fanout.

Commit as `backend: enforce multiplayer room policy`.

## Task 2A: Emit the promoted Lightbulb channel configuration

Implement only after promotion PR E pins the image that contains the plugin:

- [ ] In `core/containers/config.py`, enable `plugins.entries.lightbulb` and build
  `channels.lightbulb.accounts` from the same validated/sorted agent roster used
  by `_build_agents_list`; every account key is the local agent ID and contains
  only `{enabled: true}`.
- [ ] Add one deterministic binding per agent for channel `lightbulb`, matching
  its account ID and wildcard channel peer. Preserve unrelated channel bindings
  and deduplicate on the complete Lightbulb binding key.
- [ ] Set `messages.groupChat.unmentionedInbound: "room_event"`,
  `visibleReplies: "automatic"`, and `historyLimit: 50`. Do not enable global
  session visibility, broadcast groups, WhatsApp, or A2A.
- [ ] Set `tools.sessions.visibility: "agent"` and
  `tools.agentToAgent.enabled: false`; do not inherit 9.3's global cross-agent
  session-tool default. Friday/Gauge communication uses canonical rooms.
- [ ] Set top-level `session.groupScope: "per-group"` and assert that the same
  config makes inbound and outbound routing equal
  `agent:<agentId>:lightbulb:channel:<roomId>`.
- [ ] Add `tests/unit/containers/test_config_lightbulb_channel.py` covering one
  and two agents, sorted/deduplicated bindings, empty-roster fallback, ambient
  config, explicit group scope/session equality, explicit cross-agent session-tool
  denial, preservation beside Slack, and exact plugin enablement.

Commit as `backend: configure native Lightbulb channel accounts`.

## Task 3: Expose the messaging HTTP API

Create `apps/backend/routers/messaging.py` and
`apps/backend/tests/unit/routers/test_messaging.py`.

- [ ] Implement every route frozen in architecture spec section 6.
- [ ] Serialize the exact `LiveConfig`, room/member, message page, cursor, typing,
  abort/steer, runtime-operation, and error DTOs with the frozen success/status
  table. Contract tests compare exact camelCase bodies and reject unknown request
  fields.
- [ ] Add internal
  `POST /api/v1/internal/messaging/agent-messages/{ownerId}`. Strongly read the
  owner-keyed container row through the existing timing-safe
  `_require_hook_token(ownerId, request)` pattern, require the body agent to be a
  configured current room member, derive its author from the server roster, and
  reject owner/room/agent mismatch. Accept an opaque 1..256-byte stable `eventId`;
  `(ownerId,eventId)` plus the request hash is the durable agent-write idempotency
  key. Return `201` with exactly
  `{messageId:<string>,roomId:<string>,createdAt:<UTC RFC-3339 string>}`; an equal
  retry returns the same status and body, while a changed body returns 409
  `IDEMPOTENCY_CONFLICT`. Never scan or add an index by hook token.
- [ ] Add hook-authenticated
  `GET /api/v1/internal/messaging/agent-messages/{ownerId}/receipts/{eventId}`.
  Resolve only the durable `(ownerId,eventId)` agent-write receipt and return `200`
  with exactly the same `{messageId,roomId,createdAt}` body as POST, or the standard
  `ApiError` at `404` without creating/sending anything.
  This is the OpenClaw unknown-send reconciliation read path.
- [ ] `GET /live-config` returns AppSync HTTP and realtime endpoints, region,
  schema version 1, and the signed-in user's opaque channel key. It never returns
  IAM credentials or publishing authority.
- [ ] `POST /rooms/{roomId}/messages` returns the canonical committed message,
  including sequence and ID, before any model result. It updates durable
  `lastInteractiveAt` through the existing container repository.
- [ ] `POST /typing` accepts only a current room member, rate-limits by
  `(userId,roomId)` in process to one update per second, stamps identity/time, and
  publishes to the current room AppSync channel. Typing is not stored.
- [ ] `POST /runs/{runId}/abort` and `/steer` resolve the server-owned room-agent
  session and current active run; they never accept a session key. Preserve the
  existing billing gate for steer and idempotent abort semantics.
- [ ] Add dedicated authenticated approval/question resolution routes at
  `/api/v1/runtime/approvals/{id}/resolve` and
  `/api/v1/runtime/questions/{id}/resolve`; derive owner/member and call the same
  guarded OpenClaw methods as today.
- [ ] Mount `messaging.router` and the runtime action routes in `main.py` with
  OpenAPI tag `messaging`.

Router tests use dependency overrides and fakes; cover all auth, validation,
idempotency, room membership, browser-supplied-author rejection, outbound owner
binding, timing-safe hook rejection, stable opaque event IDs, server-side agent
mention parsing, receipt hit/miss, same key same/different body, and missing-agent
paths. They must not call AWS or a live container.

Commit as `backend: expose multiplayer room commands`.

## Task 4: Move the browser RPC proxy to HTTP without losing guards

Create:

```text
apps/backend/core/services/runtime_rpc.py
apps/backend/routers/runtime_rpc.py
apps/backend/tests/unit/services/test_runtime_rpc.py
apps/backend/tests/unit/routers/test_runtime_rpc.py
```

- [ ] Move—not copy—the following policy and behavior out of
  `routers/websocket_chat.py` into `RuntimeRpcService`:
  `_PROXY_ALLOWED_METHODS`, `_RUN_DISPATCH_METHODS`,
  `_SESSION_KEY_PARAM_BY_METHOD`, `_SESSION_KEYS_LIST_PARAM_BY_METHOD`,
  `_FORBIDDEN_PARAMS_BY_METHOD`, session-key ownership, session/search result
  filtering, seeded-agent delete guard, Paperclip agent create/update/delete
  mirroring, EFS cleanup, container resolution/wake, billing/run gates, and error
  normalization.
- [ ] Remove the old browser WebSocket `req` branch after its policy is moved.
  Node/operator machine branches remain and cannot call this human runtime API.
- [ ] Add `POST /api/v1/runtime/rpc`:

  ```json
  {"requestId":"uuid","method":"config.get","params":{}}
  ```

  Synchronous success is `200 {"ok":true,"payload":...}`; failure is the frozen
  error body. Reject all run-dispatch methods with the dedicated HTTP endpoint in
  `details.use`.
- [ ] Preserve result filtering but remove `_fit_rpc_result_for_frame` from the
  HTTP response path; HTTP does not have the API Gateway 32-KB frame limit.
- [ ] For `agents.create`, transact a durable operation row
  `OWNER#<owner>/OPERATION#<uuid>` with requester, sanitized parameters, state
  `queued`, and timestamps plus `OWNER#<owner>/WORK#OPERATION#<uuid>` containing
  the pointer and precomputed FIFO IDs SHA-256(`runtime|ownerId`) and
  SHA-256(`operation|operationId`). EventBridge Pipes sends the work row to
  `openclaw-work.fifo`; the request path never calls SQS. Return
  `202 {"operationId":"..."}`.
- [ ] Add `GET /api/v1/runtime/operations/{operationId}`. Only the requesting
  owner/member can read it. Completed/failed updates publish to that user's private
  AppSync channel.
- [ ] Keep normal runtime requests synchronous through
  `GatewayConnectionPool.send_rpc`; HTTP cancellation must unregister the pending
  request future without closing the owner gateway connection.

Run the existing method-allowlist, session-key, pairing, Paperclip-meta,
permission-mode, wake-gate, and delete-guard tests against the extracted service in
addition to new HTTP tests. Delete browser-socket tests; retain the focused
node/operator WebSocket authorization and relay tests.

Commit as `backend: move guarded runtime RPC to HTTP`.

## Task 5: Publish live events with IAM

Create `apps/backend/core/services/live_events.py` and
`apps/backend/tests/unit/services/test_live_events.py`.

- [ ] Implement one publisher using botocore `AWSRequest`/`SigV4Auth`, service
  `appsync`, current task-role credentials, and the configured HTTP endpoint.
- [ ] Expose only `publish_room(room_id, event)` and
  `publish_user(owner_id, user_id, event)`. Each call strongly resolves the
  current active delivery/user key internally immediately before signing; no
  producer may pass or cache a channel key. Validate UUID path segments and
  schema version before signing.
- [ ] Serialize AppSync's required body as one channel plus an array containing
  one stringified event. Apply a short connect/read timeout and one retry for 429
  or 5xx. A 2xx is successful only when the response contains `successful[0]`
  and an empty `failed` array. Reject unknown frozen event types and serialized
  events over 230 KiB. A publish failure is logged/metriced without token,
  Authorization header, message text, or event payload.
- [ ] Publish exactly the registry in architecture spec section 8. Normalize only
  `agent_id -> agentId` and `session_id -> sessionId` inside
  `room.run.frame.frame`; keep the existing discriminated frame variants.
- [ ] Unit-test signing, exact event/channel registry, response-body success and
  failure, retry classification, payload cap, log redaction, malformed-key
  rejection, stale room-key suppression, and inactive user-stream suppression
  with a fake HTTP client/credentials.

Then change producers:

- [ ] In `core/gateway/connection_pool.py`, route sessions matching
  `agent:<agent>:lightbulb:channel:<roomUuid>` to the room's current delivery key
  and AppSync. Continue internal OpenClaw RPC response handling, run tracking,
  usage accounting, push notifications, and node/operator logic unchanged.
- [ ] Before publishing a room frame, require it to arrive on the owner-bound
  gateway connection, parse the exact Lightbulb session key, strongly read
  `META.ownerId`, and strongly verify current agent membership. Publish only the
  frozen room-safe chunk/thinking/tool/run variants. Route approvals, questions,
  private progress, and existing personal invalidations to the user channel;
  reject or drop unknown event and session shapes.
- [ ] In `status_events.py`, publish personal status invalidations to each
  affected user's private channel.
- [ ] In `teams_event_broker.py`, replace API Gateway browser fanout with private
  user-channel publishing. Keep the Paperclip upstream WebSocket client.
- [ ] Change approval/question/progress producers to the same private user
  publisher.
- [ ] Delete API Gateway browser fanout and connection-table lookups from these
  producers. Do not add a second sink or compatibility flag.

Commit as `backend: publish browser projections through AppSync`.

## Task 6: Consume ordered OpenClaw work

Create `apps/backend/core/services/openclaw_work.py` and
`apps/backend/tests/unit/services/test_openclaw_work.py`.

- [ ] Long-poll the one FIFO queue with up to ten messages and 20-second wait.
  Run distinct message groups concurrently behind one bounded semaphore; never
  concurrently process the same group.
- [ ] Support two pointer schemas:

  ```json
  {"kind":"room_dispatch","roomId":"...","agentId":"...","throughSeq":42}
  {"kind":"runtime_operation","ownerId":"...","operationId":"..."}
  ```

  EventBridge Pipes places the outbox `payload.M` in the SQS body. Deserialize
  that DynamoDB AttributeValue map with boto3 `TypeDeserializer`, then validate
  exactly one schema; do not accept a complete stream record or arbitrary RPC
  parameters.

- [ ] For room dispatch, read the per-agent delivery cursor, query canonical
  messages through `throughSeq`, skip non-targeted messages while advancing, and
  call only targeted entries in sequence order.
- [ ] Immediately before every plugin call, strongly re-read the target agent's
  membership. If it was removed after the message transaction, acknowledge that
  stale target without dispatch (advance its remaining cursor when present; a
  missing cursor is already terminal). A later add initializes at the current
  sequence and cannot revive the stale wake.
- [ ] Resolve/wake the current owner container through existing
  `container_wake.request_wake_silent` and
  `EcsManager.resolve_running_container`. Do not cache task IPs or tokens in the
  queue.
- [ ] Before plugin ingress, call the existing
  `GatewayConnectionPool.ensure_connection` for that owner/task and mark the
  server-owned Lightbulb session active. This keeps the native gateway event
  stream attached even though no browser socket exists. Let the existing native
  terminal event clear the active run; clear it explicitly if ingress fails.
- [ ] Build the exact plugin ingress body with current membership/roster. Add
  only rows with `messageSeq < current.messageSeq`, take the last 50 in
  oldest-to-newest order, and drop additional oldest entries until the complete
  UTF-8 JSON body cap of 1,048,576 bytes holds. Exclude the current row and later
  rows already visible in the same batch drain. POST to
  `http://<resolvedIp>:18789/plugins/lightbulb-channel/inbound/<agentId>` using the
  existing gateway token. Do not call the fork-only `sessions.room.*` RPCs.
- [ ] Extend SQS visibility while an accepted native dispatch is running. Delete
  the message only after every target message through `throughSeq` is accepted and
  its cursor conditionally advances.
- [ ] For `runtime_operation`, strongly read the durable operation, transition
  queued -> running with an expiring worker lease, call the extracted
  `RuntimeRpcService`, store terminal result/error, and publish completion to the
  requester's user channel. A redelivery may reclaim `running` only after its
  lease expires; terminal states are no-ops.
- [ ] Invalid schema goes to retry/DLQ without logging parameters. Duplicate and
  stale wakes exit successfully from cursor/operation state. Treat plugin 202 as
  accepted whether it executed or replayed the persistent effect. Document the
  unavoidable at-least-once crash window: failure after native dispatch but before
  its persistent commit can execute the agent again; canonical outbound and
  side-effecting tools must independently deduplicate stable IDs.
- [ ] Start the worker in `main.py` lifespan, expose it in
  `system_health.BACKGROUND_TASKS`, request a graceful stop before gateway pool
  shutdown, and finish or return visibility before the ECS stop timeout.

Tests cover multi-agent concurrency, same-agent ordering, duplicate wake, cursor
race, removed target, container wake, gateway auth, byte-bounded history packing,
plugin refusal/replay, visibility extension, crash/retry, reclaim of an expired runtime-operation lease, terminal
operation no-op, async operation resumption, and graceful shutdown.

Commit as `backend: dispatch ordered room work to OpenClaw`.

## Task 7: Replace browser presence with durable activity

- [ ] Add/update durable `last_interactive_at` on authenticated room messages,
  steer, and cost-bearing runtime commands. Do not update it for AppSync
  subscriptions, typing, history reads, or status polling.
- [ ] Replace browser connection-count/park dependency in trial stopping with the
  durable timestamp plus existing active-run, Paperclip work, billing-plan, and
  gateway-liveness vetoes.
- [ ] Run the check from one small periodic task in `main.py`. Conditional
  container state transitions make duplicate execution safe during a rolling ECS
  deploy; no leader-election service is added.
- [ ] Keep founder/paid always-on behavior independent from browser connectivity.
- [ ] Add focused tests proving an idle subscription does not keep compute alive,
  active work vetoes stop, recent authenticated command vetoes stop, and duplicate
  sweeps do not double-stop.

Commit as `backend: derive compute activity from durable commands`.

## Task 8: Delete the obsolete human room/broker path and finish CI

- [ ] Update `core/services/account_wipe.py` to delete owner catalog entries,
  every owned room partition, and the owner's user-stream mapping. Preserve
  resumable/idempotent wipe behavior.
- [ ] Add wipe tests for partial retry and cross-owner isolation.
- [ ] Delete `core/gateway/native_rooms.py`,
  `core/services/human_broker_provisioning.py`, and
  `core/repositories/room_read_cursor_repo.py`; remove all imports, ECS config
  seeding, webhook calls, account-wipe steps, dedicated table configuration, and
  tests that exist only for them.
- [ ] Delete `container_repo`'s `human_broker_devices`/`human_broker_access`
  projections and helpers, `trustedBrokerProfiles` config emission, and encrypted
  human-broker seed material. Keep no dormant broker credential or allowlist path.
- [ ] Reduce `routers/websocket_chat.py` and `GatewayConnectionPool` to internal
  OpenClaw RPC/events plus node/operator relay. Remove browser `chat` and
  `native_human` request/fanout/presence code, while retaining paired node
  `node.invoke`/`node.invoke.result` and signed Paperclip operator behavior.
- [ ] Remove the three fork-only room methods from the HTTP runtime allowlist so
  no browser command can reach them. The methods may remain inert in the pinned
  base image until the next routine fork rebase drops those patches.
- [ ] Update system health to surface queue worker failure and AppSync publisher
  configuration.
- [ ] Push the branch and rely on `backend.yml` for unit/contract checks; fix only
  failures caused by this packet.
- [ ] PR description must list the extracted security guards, HTTP endpoints,
  queue schemas, AppSync event families, and explicit retained machine sockets.

The backend PR is ready to merge when backend CI passes, forbidden legacy symbols
have no application callers, and node/operator machine-relay tests still pass.
