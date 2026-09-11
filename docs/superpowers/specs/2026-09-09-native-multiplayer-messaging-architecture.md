# Native multiplayer messaging architecture

**Status:** approved architecture; implementation contracts frozen  
**Baseline:** Lightbulb `origin/main` at
`00b8b48d5671d5fc8d4d549ab2748151a07a1a9c`; OpenClaw fork
`v2026.9.3-isol8.2` at `a681accbfd1871845e684937ba7af57ecf155f55`  
**Product:** Lightbulb is the messaging application. OpenClaw is its native AI
runtime. Paperclip is its durable work-control plane.

This document replaces the OpenClaw-canonical native-room experiment and the
browser-facing API Gateway WebSocket protocol. It is the shared contract for the
parallel implementation packets in `docs/superpowers/plans/`.

## 1. Decision

Remove Lightbulb's custom browser/mobile WebSocket protocol. Browser and mobile
clients use:

- Clerk-authenticated HTTP for commands, writes, reads, abort, steer, approvals,
  questions, and reconciliation;
- AWS AppSync Events for managed server-to-client live delivery; and
- DynamoDB as the canonical ordered room/message store.

"Remove WebSockets" does **not** mean replacing every protocol that happens to use
a WebSocket. AppSync Events itself uses a managed WebSocket connection. These
separate machine protocols remain because events are not a substitute for them:

| Transport | Decision | Reason |
| --- | --- | --- |
| Browser/mobile -> API Gateway WebSocket | Remove | It duplicates auth, reconnect, fanout, presence, correlation, and connection storage that AppSync/HTTP already provide. |
| Browser/mobile <- AppSync Events | Add | AWS owns connection infrastructure, OIDC/IAM authorization, and elastic fanout; a small shared client implements the documented wire protocol. |
| Browser/mobile -> FastAPI HTTP | Keep/expand | Request/response and durable commands belong on HTTP. |
| Backend -> OpenClaw gateway WebSocket | Keep | This is OpenClaw's native authenticated RPC/event protocol. |
| Tauri node host <-> API Gateway WebSocket | Keep | Bidirectional `node.invoke`/`node.invoke.result` device relay. Move to AWS IoT Core only as a separate measured project. |
| Paperclip operator <-> API Gateway WebSocket | Keep | Signed bidirectional operator RPC used by the current Paperclip adapter. |
| Browser -> container VNC WebSocket | Keep | Binary interactive display protocol, unrelated to chat. |
| Browser -> Paperclip proxy WebSocket | Keep | Paperclip UI's own live transport, unrelated to Lightbulb room delivery. |

In the target architecture the API Gateway socket is a machine relay only: its authorizer accepts
paired device credentials and the Paperclip service token, never Clerk browser
sessions; no `chat` or `native_human` connection rows remain.

## 2. Why AppSync Events is the right browser transport

The current browser socket owns application connection IDs, DynamoDB connection
rows, connect/disconnect Lambdas, management-API fanout, ping/park/reconnect logic,
and a multiplexed request/response protocol. That is infrastructure Lightbulb has
to operate before it can deliver one chat event.

AppSync Events is a managed serverless pub/sub service. It supports OpenID Connect
for clients, IAM for backend publishing, namespace-level `onSubscribe` handlers,
DynamoDB integrations, 240-KB events, and elastic server-side connection/fanout
management. It removes the browser connection table and fanout loop without
changing the product's durable model. A dependency-free shared client implements
AWS's small documented WebSocket protocol, including reconnect and subscription
acknowledgements. Clients reconcile from HTTP because AppSync is ephemeral and can
deliver duplicates or gaps.

The choice is deliberately narrow:

- AppSync is the live projection, not the database.
- HTTP is the command path; clients never publish canonical messages to AppSync.
- DynamoDB sequence numbers, not arrival time, define room order.
- SQS is only the durable OpenClaw work wakeup; it is not another transcript.
- No generic event bus, event-sourcing framework, Kafka, Redis, or per-tenant queue
  is introduced.

### Current `main`: keep, migrate, or delete

Today both web and mobile instantiate
`packages/shared/src/gateway/useGatewayConnection.ts`. That hook fetches a Clerk
token, opens the API Gateway socket, owns reconnect/backoff, 30-second pings,
pending RPC correlation, chat frames, and park/unpark state. API Gateway invokes
`websocket-authorizer`, `ws-connect`, and the default-message Lambda; those Lambdas
persist/classify a connection and forward an internal-secret-authenticated HTTP
request to `routers/websocket_chat.py`. That router multiplexes human chat, generic
OpenClaw RPC, Tauri node, and Paperclip operator traffic. The process-local
`GatewayConnectionPool` then holds the backend-to-container OpenClaw socket and
uses the API Gateway Management API plus connection-table lookups to fan output
back to browser connection IDs. The replacement splits this multiplexed path at its
real boundaries: human commands become HTTP, human delivery becomes AppSync, and
the three internal/machine protocols keep their existing sockets.

The already-merged native-room work is useful evidence, but it is not the target
transport. Treat it as follows so there is only one room domain:

| Current code on `main` | Disposition | Why |
| --- | --- | --- |
| `openclaw-version.json` 9.3 tag/digest evidence and image qualification | Keep | This is the verified fork/image baseline. |
| `core/gateway/connection_pool.py` / `GatewayConnectionPool` | Keep and narrow | It is the internal OpenClaw RPC/session engine. Remove only browser fanout and the `NativeRoomPool` attachment. |
| `core/gateway/native_rooms.py` and `sessions.room.*` callers | Delete in backend PR C | These fork-only 9.3 RPCs implement an OpenClaw-canonical restricted room around one agent session; the canonical multi-agent Lightbulb room plus native channel replaces them. |
| `human_broker_provisioning.py`, broker profiles, per-human OpenClaw devices/seeds | Delete in backend PR C | Humans are canonical Lightbulb senders, not provisioned OpenClaw operator devices. |
| `room_read_cursor_repo.py` and the dedicated room-read-cursors table | Delete | There are no user cursors to migrate; new read cursors live in the single messaging table beside room membership. |
| API Gateway WebSocket connection table and Lambdas | Keep for machine rows only | Tauri nodes and Paperclip operators still need bidirectional relay; remove `chat`/`native_human` rows and Clerk authorization. |
| browser/mobile gateway providers and presence-driven wake/park | Delete in frontend PR D | HTTP commands own wake decisions; UI socket presence must not control container lifecycle. |
| PR #2404 room components and accessibility/uncertain-send tests | Reuse | Presentation and behavior are valuable; replace its synthetic fixtures and `sessions.room.*` contracts. |

There is no staged cutover or compatibility sink. PRs A, C, and D remove their
owned legacy human paths while adding the replacement. All feature PRs merge
before one production dispatch, then only the founder container is rerolled and
accepted.

## 3. Product and ownership model

| Concern | Canonical owner |
| --- | --- |
| Rooms, membership, messages, authorship, reply/thread links, unread state | Lightbulb/DynamoDB |
| Live web/mobile projections | AWS AppSync Events |
| One agent's context, tools, queue, compaction, memory, recovery | OpenClaw |
| Goals, projects, issues, routines, approvals, runs, artifacts | Paperclip |
| User and organization identity | Clerk, revalidated by Lightbulb/AppSync |

A normal conversation does not create a Paperclip issue. A human or agent
explicitly links a room, thread, or message to a goal/project/issue/artifact.
Paperclip routines own durable scheduling. OpenClaw heartbeats may notice work and
speak, but do not become a second scheduler.

## 4. OpenClaw 9.3 facts and Lightbulb mapping

OpenClaw 9.3 already provides the primitives Lightbulb should compose:

- deterministic agent-owned channel sessions;
- structured sender, conversation, route, mention, reply, and inbound-history
  context;
- native `user_request` and ambient `room_event` turns;
- `visibleReplies: "automatic"`, which delivers addressed `user_request` finals
  and finals from utility-gate-approved ambient turns normally;
- native channel ingress, routing, message adapters, per-session serialization,
  compaction, memory, and pair-based bot-loop protection (20 peer events per
  60-second window, then a 60-second cooldown by the upstream default);
- WhatsApp-only experimental broadcast groups, which prove multi-agent fanout but
  give each agent an isolated session and do not make agents see peer replies; and
- the A2A 1.0 channel for authenticated external agent peers. A2A is not an
  in-container group-chat engine and currently lacks streaming, push, cancel,
  durable tasks, and multi-tenant routing.

Therefore:

1. A Lightbulb room is one visible canonical conversation.
2. An OpenClaw session still has one executing agent.
3. Every participating agent gets its own session:
   `agent:<agentId>:lightbulb:channel:<roomId>`.
   `agentId` is the existing canonical Paperclip/OpenClaw UUID; Friday and Gauge
   are friendly names/mention handles, not substitute agent IDs.
4. Multiple humans appear as authenticated senders in each agent's channel
   context; they do not become one OpenClaw identity.
5. Mentioned agents receive `user_request` turns in parallel.
6. Unmentioned `active` listeners enter through the ambient relevance gate as
   `room_event`; approved turns run as non-command `user_request` turns so their
   final is delivered automatically.
7. `mentions` and `observe` members do not run on unmentioned messages. Their next
   turn receives recent canonical history.
8. Agent room posts become canonical Lightbulb messages and are delivered to peer
   agents, never echoed to their author.
9. Addressed and gate-approved ambient request finals deliver automatically.
   Cross-agent coordination for this release happens in the
   canonical room or through explicit Paperclip work, not private session tools.
10. One OpenClaw runtime is shared by one workspace/company, never across tenants.

The existing `apps/backend/core/gateway/native_rooms.py` calls
`sessions.room.create`, `sessions.message.append`, and
`sessions.execution.dispatch`. Those methods are Lightbulb fork additions in
`v2026.9.3-isol8.2`, not upstream multiplayer. They bind a restricted room to one
agent-owned session and rely on trusted human-broker devices; they do not provide
one canonical Slack-like room shared across independent Friday and Gauge sessions.
The new product therefore removes every Lightbulb caller, broker identity, and
allowlist entry. The inert fork methods may remain in the pinned base until the
next routine fork rebase drops those patches; no new product code depends on them.

## 5. OpenClaw integration: plugin, not fork patch

The Lightbulb channel lives at
`apps/infra/openclaw/plugins/lightbulb-channel/` and is baked into the extended
image. The pinned 9.3 plugin SDK plus the A2A channel implementation prove that the
necessary public seams exist. A2A uses plugin auth itself; gateway auth is supported
by the public HTTP-route contract:

- `defineChannelPluginEntry` and `createChatChannelPlugin` for a supported
  external channel entry rather than bundled-only helpers;
- `registerPluginHttpRoute({ auth: "gateway" })` for private backend ingress;
- `resolveChannelInboundRouteEnvelope` for agent/session routing;
- `resolveStableChannelMessageIngress` for channel policy and identity admission;
- `buildChannelInboundEventContext` for sender, room, mentions, reply context, and
  `inboundHistory`;
- `channelRuntime.inbound.dispatch` for native execution; and
- `defineChannelMessageAdapter` for native `message` delivery;
- `createIngressEffectOnce` for restart-persistent, claim/dispatch/commit replay
  suppression; and
- `readJsonWebhookBodyOrReject` for bounded route-body parsing.

No new agent-fork PR or `isol8.3` tag is part of this plan. The extended image stays
on fork `v2026.9.3-isol8.2` and adds the Lightbulb plugin. A fork change is opened
only if the plugin's pinned-9.3 compile/qualification test demonstrates a missing
public seam. This keeps the next upstream OpenClaw upgrade a base/tag bump rather
than a product re-port.

Configure one Lightbulb channel account per local agent and explicitly set
`session.groupScope: "per-group"`. The server-authored `accountId` is the agent
identity used by the outbound adapter; the model never chooses its author ID. A
dynamic group conversation ID is the Lightbulb room ID. The pinned resolver already
defaults to `per-group`, but the explicit setting and qualification assertion keep
inbound and outbound routing from drifting after an upstream upgrade.

### Private ingress

The backend calls a gateway-authenticated plugin route, not a browser RPC method:

```http
POST /plugins/lightbulb-channel/inbound/<targetAgentId>
Authorization: Bearer <existing OpenClaw gateway token>
Content-Type: application/json
```

```ts
type LightbulbRoomIngress = {
  version: 1;
  eventId: string; // canonical messageId; stable retry key
  targetAgentId: string;
  eventKind: "user_request" | "room_event";
  room: {
    id: string;
    name: string | null;
    kind: "channel" | "dm" | "group-dm";
    revision: number;
  };
  sender: {
    kind: "human" | "agent" | "system";
    id: string;
    displayName: string;
  };
  message: CanonicalRoomMessage;
  mentions: Array<{ kind: "human" | "agent"; id: string }>;
  participants: Array<{
    kind: "human" | "agent";
    id: string;
    displayName: string;
    mentionHandle?: string;
    listenMode?: "active" | "mentions" | "observe";
  }>;
  recentHistory: CanonicalRoomMessage[];
};
```

The plugin constructs native structured channel context. It never prepends a fake
system/transcript message. It rejects a target whose configured Lightbulb account
does not equal `targetAgentId`, a malformed room ID, or an unknown event kind. It
passes `groupPolicy: "open"` to native admission because the gateway token plus
Lightbulb's current canonical membership check is the trust boundary; the SDK
otherwise defaults group ingress to disabled.
It sets the supplied kind in `contextBinding.inboundEventKind` for native admission.
After an ambient relevance decision returns `DISPATCH`, it sets
`message.inboundEventKind` to `user_request` so the approved final is visible;
agent-sent and ambient ingress still set `command: false` and
`CommandInterpretationSuppressed: true`.

The complete serialized ingress body is capped at 1,048,576 bytes with
`readJsonWebhookBodyOrReject`. For current message sequence N, the backend selects
only rows with `messageSeq < N`, keeps the last 50, orders them oldest-to-newest,
and drops additional oldest rows until the entire JSON body fits. It excludes the
current message and later rows already present in the same queue drain, while
retaining the separate 100,000-byte message-text and 128-participant limits.
The plugin maps each accepted canonical history row to the exact 9.3 `HistoryEntry`
shape `{sender: author.displayName, body: text, timestamp:
Date.parse(createdAt), messageId}` before assigning `message.inboundHistory`; it
never passes `CanonicalRoomMessage[]` directly to the SDK.

`resolveStableChannelMessageIngress` is not a dedupe store. One account-scoped
`createIngressEffectOnce` guard uses `pluginId: "lightbulb"`, namespace prefix
`lightbulb-inbound-<accountId>`, the canonical message ID, and effect `dispatch`.
It returns 202 for an already committed replay, releases before-effect failures,
and fails closed on persistent-state errors. Execution is still at least once: a
process death after native dispatch succeeds but before the guard commits can run
the model again. Canonical outbound writes are independently idempotent, and tools
that cause external side effects must use their own idempotency keys; the plan does
not claim impossible exactly-once model/tool execution.

### Native outbound

The native message adapter posts:

```http
POST /api/v1/internal/messaging/agent-messages/<ownerId>
GET  /api/v1/internal/messaging/agent-messages/<ownerId>/receipts/<eventId>
Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}
```

```json
{
  "eventId": "preparedMessageId or deliveryQueueId:partIndex",
  "roomId": "server-routed channel target",
  "agentId": "server-bound Lightbulb account id",
  "text": "I found two failures in onboarding.",
  "replyToMessageId": null,
  "threadRootMessageId": null
}
```

The internal writer contract is frozen across the backend and image packets:

```ts
type AgentMessageReceiptResponse = {
  messageId: string;
  roomId: string;
  createdAt: string; // canonical UTC RFC-3339 timestamp
};
```

`POST` returns `201` with exactly this body; an equal idempotent replay returns
the same status and body. Receipt `GET` returns `200` with the same body or `404`
with the standard `ApiError`. The plugin validates the response and maps it to
the exact 9.3 adapter receipt:

```ts
{
  primaryPlatformMessageId: response.messageId,
  platformMessageIds: [response.messageId],
  parts: [{ platformMessageId: response.messageId, kind: "text", index: 0 }],
  sentAt: Date.parse(response.createdAt),
}
```

The plugin supplies `ownerId` from `ISOL8_OWNER_ID` and `agentId` from its account
context. The adapter API has no mentions or run ID, so the backend resolves exact
case-insensitive `@<mentionHandle>` tokens against the current room roster and
stores the matching canonical agent UUID plus `runId: null`; it maps `replyToId`
and `threadId` when supplied. `eventId` is a
bounded opaque idempotency key, not an RFC-4122-only field: use
`deliveryQueueId` plus its zero-based part index when present, otherwise use
`preparedMessageId`; fail closed if neither stable host value exists. The backend strongly
reads the owner-keyed container row, timing-safely compares its hook token, verifies
current room and agent membership, and uses the same canonical message writer as
human messages. No token-to-owner table scan or new hook-token index is added.

The channel declares durable-final capabilities for text, replies, threads, and
message-sending hooks plus unknown-send reconciliation, enables automatic text
reconciliation, and does not define a text chunker. Inbound `user_request` dispatch supplies a core-managed
durable delivery to `lightbulb:<roomId>` and a direct fallback that always throws;
there is no path around the stable prepared/queue ID supplied to the native message
adapter. The delivery boundary accepts final payloads only. Gate-approved ambient
finals use the same durable path; declined ambient turns never reach the full model.
If a send becomes ambiguous, the adapter
queries the owner-scoped receipt endpoint with `<queueId>:0`; an existing canonical
receipt becomes the normalized OpenClaw `sent` receipt, 404 is `not_sent`, and a
transient/invalid result remains `unresolved` for retry.

## 6. Browser/HTTP contract

All browser requests use the existing Clerk bearer token. The backend derives
`ownerId`, human identity, author snapshot, room sequence, dispatch targets,
OpenClaw session keys, and container routing.

### Messaging API

```http
GET    /api/v1/messaging/live-config
GET    /api/v1/messaging/rooms
POST   /api/v1/messaging/rooms
GET    /api/v1/messaging/rooms/{roomId}
PATCH  /api/v1/messaging/rooms/{roomId}
POST   /api/v1/messaging/rooms/{roomId}/members
DELETE /api/v1/messaging/rooms/{roomId}/members/{kind}/{memberId}
GET    /api/v1/messaging/rooms/{roomId}/messages?afterSeq={n}&limit={1..100}
POST   /api/v1/messaging/rooms/{roomId}/messages
PUT    /api/v1/messaging/rooms/{roomId}/read
POST   /api/v1/messaging/runs/{runId}/abort
POST   /api/v1/messaging/runs/{runId}/steer
POST   /api/v1/messaging/typing
```

`live-config` returns the environment's AppSync HTTP and realtime endpoints, AWS
region, the signed-in user's opaque channel key, and an event-schema version. The
API does not return IAM credentials or allow client publishing.

The public JSON contract is camelCase. Python may remain snake_case internally,
but Pydantic response models must serialize aliases and contract tests compare the
exact JSON. Threads are message subviews, not room records; the founder release
has no public-room discovery or implicit join.

```ts
type RoomKind = "channel" | "dm" | "group-dm";
type MemberKind = "human" | "agent";
type MemberRole = "owner" | "admin" | "member";
type ListenMode = "active" | "mentions" | "observe";

type MemberRef = { kind: MemberKind; id: string };

type ApiErrorCode =
  | "INVALID_REQUEST"
  | "ROOM_FORBIDDEN"
  | "ROOM_NOT_FOUND"
  | "MEMBER_NOT_FOUND"
  | "MESSAGE_NOT_FOUND"
  | "RUN_NOT_FOUND"
  | "OPERATION_NOT_FOUND"
  | "IDEMPOTENCY_CONFLICT"
  | "MEMBER_LIMIT"
  | "RUN_NOT_ACTIVE"
  | "RATE_LIMITED"
  | "RUNTIME_UNAVAILABLE"
  | "INTERNAL_ERROR";

type ApiError = {
  code: ApiErrorCode;
  message: string;
  retryable: boolean;
  reconcileFromSeq?: number;
  retryAfter?: number; // seconds
  details?: Record<string, unknown>;
};

type RoomMember = MemberRef & {
  displayName: string;
  mentionHandle: string | null; // server-owned and unique for agents; null for humans
  role: MemberRole;
  listenMode: ListenMode | null; // null for humans
  joinedAt: string;
  joinedSeq: number;
};

type RoomSummary = {
  roomId: string;
  name: string | null;
  kind: RoomKind;
  roomRevision: number;
  lastMessageSeq: number;
  lastMessageAt: string | null;
  unreadCount: number;
  updatedAt: string;
};

type RoomDetail = RoomSummary & {
  deliveryKey: string;
  members: RoomMember[];
  lastReadSeq: number;
  createdAt: string;
};

type RoomListResponse = { rooms: RoomSummary[] };

type CreateRoomRequest = {
  clientRoomId: string; // UUID, durable idempotency key
  name: string | null;
  kind: RoomKind;
  members: Array<{
    kind: MemberKind;
    id: string;
    role: "admin" | "member";
    listenMode: ListenMode | null;
  }>;
};

type PatchRoomRequest = { name: string | null };

type UpsertRoomMemberRequest = {
  kind: MemberKind;
  id: string;
  role: "admin" | "member";
  listenMode: ListenMode | null;
};

type CanonicalRoomMessage = {
  messageId: string;
  roomId: string;
  messageSeq: number;
  roomRevision: number;
  author: {
    kind: "human" | "agent" | "system";
    id: string;
    displayName: string;
  };
  text: string;
  mentions: Array<{ kind: "human" | "agent"; id: string }>;
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
  createdAt: string;
  runId: string | null;
};

type CreateMessageRequest = {
  clientMessageId: string; // UUID, durable idempotency key
  text: string;
  mentions: MemberRef[];
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
};

type MessagePage = {
  messages: CanonicalRoomMessage[];
  nextAfterSeq: number | null;
};

type ReadCursorRequest = { lastReadSeq: number };
type ReadCursorResponse = { roomId: string; lastReadSeq: number };

type TypingRequest = { roomId: string; isTyping: boolean };

type AbortRunResponse = {
  runId: string;
  state: "aborting" | "already-terminal";
};

type SteerRunRequest = { text: string };
type SteerRunResponse = { runId: string; state: "accepted" };

type RuntimeError = Pick<ApiError, "code" | "message" | "retryable">;

type RuntimeOperation = {
  operationId: string;
  state: "queued" | "running" | "succeeded" | "failed";
  createdAt: string;
  updatedAt: string;
  result: unknown | null;
  error: RuntimeError | null;
};

type LiveConfig = {
  schemaVersion: 1;
  httpEndpoint: string;
  realtimeEndpoint: string;
  region: string;
  userChannelKey: string;
};
```

Agent mention handles are server-owned roster data. Normalize the friendly agent
name to lowercase ASCII `[a-z0-9_-]`, collapse separators, and cap at 32 characters;
if it is empty use `agent-<first8 canonical UUID>`. Compute collisions over the
canonical-ID-sorted roster and suffix every colliding candidate—truncated as needed
to stay within 32 characters—with its first eight UUID hex characters. Store/expose
the resulting unique handle on agent membership
and structured channel participants. Human-authored structured `mentions` remain
authoritative. Agent-authored text resolves only exact token-boundary
`@<mentionHandle>` matches; an absent or ambiguous handle produces no mention,
never a guessed identity.

Response and status contracts are exact:

| Endpoint | Success |
| --- | --- |
| `GET /live-config` | `200 LiveConfig` |
| `GET /rooms` | `200 RoomListResponse` |
| `POST /rooms` | `201 RoomDetail` |
| `GET /rooms/{roomId}` | `200 RoomDetail` |
| `PATCH /rooms/{roomId}` | `200 RoomDetail` |
| `POST /rooms/{roomId}/members` | `200 RoomDetail` |
| `DELETE /rooms/{roomId}/members/{kind}/{memberId}` | `204` |
| `GET /rooms/{roomId}/messages` | `200 MessagePage` |
| `POST /rooms/{roomId}/messages` | `201 CanonicalRoomMessage` |
| `PUT /rooms/{roomId}/read` | `200 ReadCursorResponse` |
| `POST /typing` | `204` |
| `POST /runs/{runId}/abort` | `200 AbortRunResponse` |
| `POST /runs/{runId}/steer` | `200 SteerRunResponse` |
| `GET /runtime/operations/{operationId}` | `200 RuntimeOperation` |

`POST /rooms`, `PATCH /rooms/{roomId}`, member upsert, message creation, read,
typing, and steer accept exactly the request types above; abort accepts an empty
body. `RoomDetail` is returned only after a strong membership read. Agent members
must use role `member` and a non-null listen mode; human members use a null listen
mode. The creator is always the human `owner` and is omitted from `members` in the
create request.

Every non-401 HTTP failure returns exactly `ApiError`; optional keys are absent,
not null. `reconcileFromSeq` appears only when a canonical room refetch can repair
the failure, `retryAfter` only on 429/503, and sanitized `details` only for the
existing runtime-RPC compatibility surface. The founder-release code/status
registry is `INVALID_REQUEST`/400, `ROOM_FORBIDDEN`/403,
`ROOM_NOT_FOUND`/404, `MEMBER_NOT_FOUND`/404, `MESSAGE_NOT_FOUND`/404,
`RUN_NOT_FOUND`/404, `OPERATION_NOT_FOUND`/404,
`IDEMPOTENCY_CONFLICT`/409, `MEMBER_LIMIT`/409, `RUN_NOT_ACTIVE`/409,
`RATE_LIMITED`/429, `RUNTIME_UNAVAILABLE`/503, and `INTERNAL_ERROR`/500.
Authentication middleware retains its existing 401 response. No endpoint returns
raw DynamoDB items or OpenClaw errors.

All canonical writers reject message text over 100,000 UTF-8 bytes. This leaves
headroom under DynamoDB's item limit and AppSync's 240-KB event limit after the
envelope, mentions, and author metadata are added. Live publishers serialize and
reject any complete event over 230 KiB before calling AppSync.

Message input:

```json
{
  "clientMessageId": "client UUID",
  "text": "@Friday and @Gauge investigate the launch drop",
  "mentions": [
    { "kind": "agent", "id": "11111111-1111-4111-8111-111111111111" },
    { "kind": "agent", "id": "22222222-2222-4222-8222-222222222222" }
  ],
  "replyToMessageId": null,
  "threadRootMessageId": null
}
```

A successful response is the canonical stored message. Repeating the same
`(roomId, author principal, clientMessageId)` with the same body returns the same
message. Reusing it with different content returns `IDEMPOTENCY_CONFLICT`.

Errors keep one transport-independent shape:

```json
{
  "code": "ROOM_FORBIDDEN",
  "message": "You no longer have access to this room.",
  "retryable": false,
  "reconcileFromSeq": 42
}
```

### Existing runtime control

Move the current WebSocket `req` proxy to
`POST /api/v1/runtime/rpc` without widening its default-deny method allowlist,
forbidden-parameter checks, session ownership checks, result filtering, wake
logic, or Paperclip mirror side effects. This is the shortest safe migration for
config, models, skills, tools, sessions, browser start, and health panels.

Room messages, abort, steer, approval resolution, and question resolution use
product endpoints rather than compatibility RPC. Operations that can exceed the
HTTP integration timeout, including `agents.create`, return
`202 { "operationId": "..." }`; progress and completion arrive on the private user
event channel and are queryable over HTTP.

## 7. Canonical DynamoDB model

Use one on-demand, CMK-encrypted, point-in-time-recoverable `messaging` table with
DynamoDB Streams (`NEW_IMAGE`) and numeric epoch-seconds TTL attribute `expiresAt`
enabled. Only one-shot `WORK#` and `LIVE#` outbox rows expire after seven days;
canonical data does not.

| PK | SK | Purpose |
| --- | --- | --- |
| `OWNER#<ownerId>` | `ROOM#<roomId>` | room directory projection |
| `ROOM#<roomId>` | `META` | owner, room fields, `revision`, `lastSeq`, `deliveryKey` |
| `ROOM#<roomId>` | `MEMBER#<kind>#<id>` | current role, listen mode, joined sequence |
| `ROOM#<roomId>` | `MSG#<20-digit-seq>` | immutable canonical message and stable dispatch target IDs |
| `ROOM#<roomId>` | `IDEMP#<kind>#<id>#<clientMessageId>` | durable retry receipt and request hash |
| `ROOM#<roomId>` | `READ#human#<id>` | last read sequence |
| `ROOM#<roomId>` | `DELIVERY#AGENT#<agentId>` | last drained canonical sequence for that agent |
| `ROOM#<roomId>` | `WORK#MESSAGE#<seq>#AGENT#<agentId>` | transactional one-shot outbox record for one agent wake |
| `ROOM#<roomId>` | `LIVE#<eventId>#USER#<userId>` | transactional private-user notification outbox |
| `OWNER#<ownerId>` | `USERSTREAM#<userId>` | current opaque private-channel key and active membership projection |
| `STREAM#<opaqueKey>` | `META` | private AppSync user-channel owner/user/active reverse mapping |
| `OWNER#<ownerId>` | `OPERATION#<operationId>` | status/result for an asynchronous runtime command |
| `OWNER#<ownerId>` | `WORK#OPERATION#<operationId>` | transactional one-shot outbox record for runtime work |

Membership rows project to a sparse `by-member` GSI:

```text
memberKey = OWNER#<ownerId>#<kind>#<id>
memberSort = ROOM#<roomId>
```

Authorization always uses a strongly consistent base-table read; the GSI is only
for room-directory discovery.

Room creation is capped at 32 total members for the founder release. In the
worst case its three fixed actions, 32 membership rows, and 31 agent-delivery
rows use 66 of DynamoDB's 100 transaction actions.

Message creation strongly reads `META.lastSeq`, then conditionally transacts:

1. increment `META.lastSeq` and update activity;
2. put the immutable `MSG` with server-resolved dispatch targets; and
3. put the durable `IDEMP` receipt with a hash of canonical request fields; and
4. put one `WORK#MESSAGE` outbox item per dispatch target.

On conditional conflict the service rereads and retries. DynamoDB's transaction
client token is short-lived, so the `IDEMP` item is the lasting retry guarantee.
One extremely hot room is intentionally one partition; shard only after a measured
room approaches 100 writes/second.

## 8. AppSync channel and revocation contract

Clients subscribe to at most two channels for the MVP:

| Channel | Subscriber | Contents |
| --- | --- | --- |
| `/rooms/<roomId>/<deliveryKey>` | current human room members | canonical messages, run/tool stream events, typing, roster and room revisions |
| `/users/<opaqueUserChannelKey>` | exactly one signed-in human | approvals, questions, operation progress, health/config/teams invalidations, room-key rotation notices |

AppSync channel segments permit only alphanumeric characters and dashes and cap
each segment at 50 characters. Both keys are server-created UUIDs. Do not put raw
Clerk IDs in a channel path.

Connection and subscription auth use a dedicated Clerk JWT template named
`lightbulb-appsync` with the static claim `{"aud":"lightbulb-appsync"}`. AppSync
configures the Clerk issuer plus the anchored client-ID regex
`^lightbulb-appsync$`, which AWS validates against `aud` or `azp`. The regular
HTTP API continues to use the normal Clerk session token. Namespace
`onSubscribe` handlers use a strongly consistent DynamoDB `BatchGetItem`:

- `rooms` checks both `META.deliveryKey` and
  `MEMBER#human#<ctx.identity.sub>`;
- `users` checks `STREAM#<channelKey>.userId == ctx.identity.sub` and
  `active == true`.

Backend and stream-projector roles publish with IAM. Clients have subscribe-only
authorization; typing is an authenticated, rate-limited HTTP command that the
backend republishes.

Room and user `onPublish` handlers strongly read current metadata and reject any
IAM publish to a stale room key or inactive user key. Backend room/user publisher
methods accept product IDs, not channel keys, and resolve the current active key
immediately before each publish. The projector also treats inactive user mappings
as successful no-ops. The existing Clerk `organizationMembership.deleted` webhook
enumerates that human's discovered rooms, strongly rechecks each membership,
removes it, rotates every affected room delivery key, and writes the same durable
rotation/removal notices before marking both user-stream mappings inactive. A later
rejoin gets a new user key. Consequently already-open old user and room
subscriptions receive no future publications.

An `onSubscribe` decision does not revoke an already-open subscription. Therefore
every room has an opaque `deliveryKey`. Any human membership removal rotates that
key and writes one durable `LIVE` notification per affected human in the same
membership transaction. The projector publishes `room.subscription.rotated` to
remaining members and `room.access.removed` to the removed member through their
private user channels. It strong-reads current `META` before any room publish and
never publishes to a historical key. Clients also revalidate active-room metadata
over HTTP once per minute, so a permanently failed ephemeral rotation notice
cannot strand an authorized client. The removed subscriber's old room channel
becomes permanently silent. This is a security boundary, not optional cleanup.

Every event has:

```ts
type LiveEventType =
  | "room.message.created"
  | "room.typing.changed"
  | "room.run.frame"
  | "room.metadata.changed"
  | "room.subscription.rotated"
  | "room.access.removed"
  | "runtime.operation.changed"
  | "runtime.approval.requested"
  | "runtime.approval.resolved"
  | "runtime.question.requested"
  | "runtime.question.resolved"
  | "runtime.progress.changed"
  | "runtime.resource.invalidated"
  | "paperclip.event";

type LiveEvent<T> = {
  schemaVersion: 1;
  eventId: string;
  type: LiveEventType;
  occurredAt: string;
  roomId?: string;
  messageSeq?: number;
  roomRevision?: number;
  payload: T;
};

type RuntimeOperationChanged = {
  operationId: string;
  state: "queued" | "running" | "succeeded" | "failed";
  updatedAt: string;
  result: unknown | null;
  error: {
    code: string;
    message: string;
    retryable: boolean;
  } | null;
};
```

The exact founder-release event registry is:

| `type` | Channel | Payload | Durable/order | Client action |
| --- | --- | --- | --- | --- |
| `room.message.created` | room | `CanonicalRoomMessage` | durable; `messageSeq` | insert by sequence; reconcile a gap |
| `room.typing.changed` | room | `{roomId,userId,displayName,expiresAt}` | ephemeral | replace typing lease |
| `room.run.frame` | room | `{roomId,agentId,runId,frame}` | ephemeral | route to existing run reducer by `(roomId,agentId,runId)` |
| `room.metadata.changed` | room | `{roomId,roomRevision}` | reconcile hint | refetch room metadata |
| `room.subscription.rotated` | user | `{roomId,deliveryKey,roomRevision}` | durable `LIVE` projection | replace subscription, then reconcile |
| `room.access.removed` | user | `{roomId,roomRevision}` | durable `LIVE` projection | close room and remove cached data |
| `runtime.operation.changed` | user | `{operationId,state,updatedAt,result,error}` | operation row is durable | reconcile operation by ID |
| `runtime.approval.requested` | user | existing `ExecApprovalRequest` projection | container is authoritative | enqueue card; reload through HTTP |
| `runtime.approval.resolved` | user | `{id}` | container is authoritative | remove card |
| `runtime.question.requested` | user | existing `QuestionRecord` | container is authoritative | enqueue card; reload through HTTP |
| `runtime.question.resolved` | user | existing `QuestionResolvedEvent` | container is authoritative | remove card |
| `runtime.progress.changed` | user | `{sessionKey,revision}` | progress card is durable in OpenClaw | revalidate progress HTTP/RPC read |
| `runtime.resource.invalidated` | user | `{resource,resourceId?,revision?}` where resource is `agents`, `config`, `models`, `skills`, `tools`, `health`, or `onboarding` | reconcile hint | invalidate matching SWR keys |
| `paperclip.event` | user | `{eventName,payload}` where `eventName` is exactly `teams.activity.logged`, `teams.agent.status`, `teams.heartbeat.run.queued`, `teams.heartbeat.run.status`, `teams.heartbeat.run.event`, `teams.heartbeat.run.log`, or `teams.stream.resumed` | Paperclip is authoritative | run the existing Paperclip invalidation map |

`room.run.frame.frame` is the existing `ChatIncomingMessage` discriminated union
from `packages/shared/src/gateway/types.ts`, moved to `packages/shared/src/live/`
without changing its variant fields except the transport normalization
`agent_id -> agentId` and `session_id -> sessionId`. All other public fields are
camelCase. The backend rejects unknown event types and the shared client validates
the discriminated payload before dispatching it.

The dependency-free client follows AWS's Event API protocol exactly: connect with
the OIDC authorization object Base64URL-encoded in the `header-*` subprotocol plus
`aws-appsync-event-ws`; send `connection_init`; wait for `connection_ack`; send one
`subscribe` per desired channel with a fresh Clerk template token; mark a channel
live only after `subscribe_success`; parse every stringified item in a `data`
message's `event` array; and send `unsubscribe` on replacement. It closes after the
server-provided `connectionTimeoutMs` elapses without `ka`, then reconnects with
full-jitter exponential backoff capped at 30 seconds. One client instance owns this
single loop; consumers never create their own socket or reconnect timer. The
client has no publish method.

Clients deduplicate by `eventId`, sort durable messages by `messageSeq`, and fetch
`messages?afterSeq=<lastSeen>` after initial subscription, reconnect, key rotation,
or any gap. AppSync stores no history.

## 9. Durable projection and agent dispatch

A single `messaging-projector` Lambda consumes the messaging-table stream for live
projection only. For each new canonical `MSG` image it strong-reads the current
room `META`, publishes `room.message.created` to the current room channel, and
validates AppSync's response body (`successful[0]` exists and `failed` is empty),
not merely HTTP status. For each new `LIVE` image it publishes the frozen event to
that user's private channel with the same response validation.

A direct EventBridge Pipe consumes the same DynamoDB Stream, filters `INSERT`
records whose sort key begins `WORK#`, and sends them to
`openclaw-work.fifo`. This is a point-to-point managed connector, not an event bus.
The backend transaction stores a DynamoDB `payload` map plus the final
`messageGroupId` and `messageDeduplicationId` on each outbox item because Pipes
dynamic parameters can reference source JSON paths but not values produced by
input transformation. The Pipe has no input template (an object placeholder
renders as Java-map text, not JSON) and forwards the raw stream record; the
worker reads `dynamodb.NewImage.payload.M`, uses the SDK `TypeDeserializer`,
and obtains exactly one of:

```json
{"kind":"room_dispatch","roomId":"...","agentId":"...","throughSeq":42}
{"kind":"runtime_operation","ownerId":"...","operationId":"..."}
```

The stream-to-SQS pipe provides AWS-managed at-least-once delivery and ordering;
it has a retry policy shorter than DynamoDB Streams' 24-hour retention and a
standard DLQ. The AppSync projector and work pipe are independent consumers, so
an ephemeral UI publish failure never blocks durable agent work.

For each new canonical message the committed outbox rows contain:

1. one `room_dispatch` row per target agent; and
2. the precomputed FIFO IDs below.

The room SQS pointer contains only `kind`, `roomId`, `agentId`, and `throughSeq`.
Its `MessageGroupId` is SHA-256 of `room|ownerId|roomId|agentId`, so Friday and
Gauge run concurrently while one agent's room session remains serialized. Its
`MessageDeduplicationId` is SHA-256 of `message|messageId|agentId`. A runtime
operation uses SHA-256 of `runtime|ownerId` for its group and SHA-256 of
`operation|operationId` for deduplication. Hashing is required because raw product
identifiers can exceed SQS's 128-character ID limits. The worker treats room work
as a wake signal:

1. read `DELIVERY#AGENT#<agentId>.lastSeq`;
2. query canonical messages through `throughSeq` in sequence order;
3. skip non-targeted messages while still advancing the cursor;
4. resolve/wake the owner's OpenClaw container;
5. ensure the existing backend-to-gateway connection is attached and mark the
   server-owned room session active so native run/tool/chunk events have a live
   backend consumer;
6. call the Lightbulb plugin once for each targeted message using the canonical
   `messageId` as `eventId`; and
7. conditionally advance the agent cursor only after OpenClaw accepts that message.

Duplicate stream records and SQS delivery normally collapse through the canonical
message, per-agent cursor, and OpenClaw persistent ingress guard. Immediately before
each plugin call, the worker strongly rechecks that the target agent is still a room
member; removal makes queued work a successful skip while advancing its cursor. A
process crash in the post-dispatch/pre-commit window can execute an agent turn again,
so canonical agent output and side-effecting tools require stable idempotency keys. A
failed agent does not block another agent because they use different FIFO groups.

The AppSync projector uses batches of at most ten with a zero-second batching
window, partial batch responses, bisect-on-error, a 30-second timeout, bounded
retries, and an S3 on-failure destination that retains the complete stream record.
A failed record and iterator-age alarm require replay through the documented
projector replay command. DynamoDB remains canonical, so even a missed live event
is recovered by HTTP reconciliation. The agent queue and Pipe DLQ have alarms for
oldest age, visible messages, and failed invocations. No room-commands queue or
room-fanout queue is added: the HTTP transaction is already the command commit,
and AppSync is already the fanout service.

Noncanonical live events—OpenClaw chunks, tool activity, run state, Paperclip
invalidations—are published directly by the backend with IAM. Existing OpenClaw
text frames already arrive in roughly 150-ms coalesced chunks; preserve that
cadence and never publish one AppSync event per model token.

## 10. Security and agent-to-agent privacy

- The browser never supplies an owner ID, author ID, OpenClaw session key,
  container address, agent fanout list, or AppSync publish credential.
- The container never writes DynamoDB or AppSync directly.
- Human room membership is checked on every HTTP operation and every AppSync
  subscription, with key rotation for live revocation.
- The hook token resolves one owner; agent and room membership are rechecked for
  every native outbound message.
- A gateway run/frame event is eligible for room publication only when it arrives
  on that room owner's bound backend-to-OpenClaw connection, its session key parses
  to the expected Lightbulb agent/room shape, a strong `META.ownerId` read matches,
  and that agent is still a current room member. Only the frozen room-safe frame
  variants reach the room channel; approvals, questions, private progress, unknown
  events, and parse failures go to the current user's private channel or are dropped.
- Agent-authored room messages skip their source agent but otherwise follow the
  same server-resolved listener rules as human messages. This lets Friday see and
  answer Gauge without inventing a second peer transcript. For every agent sender,
  the plugin supplies OpenClaw's native pair-loop facts—one workspace scope shared
  by all local accounts, room ID, sender agent, receiving agent, and canonical
  message ID—so upstream's retry-deduplicated cooldown stops pathological
  ping-pong. A plugin `409 not_admitted` is a terminal refusal: the backend advances
  the delivery cursor instead of retrying the message after the cooldown. Before a
  native `room_event` reaches the full agent turn, the plugin asks the explicitly
  configured utility model for a tool-free `IGNORE`/`DISPATCH` relevance decision
  over bounded recent context and the agent's Paperclip role. A missing utility
  model, malformed output, or a failed decision drops the optional ambient turn
  instead of falling through to the primary model. Explicit DMs and mentions bypass
  this gate. An approved turn is promoted to a non-command `user_request` so its
  normal final is durably posted once. This is a cost optimization, while the Bedrock/Metronome admission
  ledger remains the spend fence; do not add a second Lightbulb turn-budget state
  machine without production evidence that both controls are insufficient.
- Explicitly set `tools.sessions.visibility: "agent"` and
  `tools.agentToAgent.enabled: false`; pinned 9.3 otherwise defaults to global
  cross-agent session visibility.

For the founder MVP, agents in one company container share an OS/runtime trust
domain. Public teammate speech uses the room. Cross-agent private session access is
disabled and not promised until OpenClaw exposes a room-scoped policy seam or
mutually untrusted agents move to separate runtimes. This avoids pretending that
application routing is sandbox isolation.

## 11. Container lifecycle

AppSync has no application disconnect callback, and an open UI subscription is not
proof that compute is needed. Delete browser-socket-driven park/unpark behavior.

- Any authenticated message or runtime command updates `lastInteractiveAt` and
  transparently wakes the runtime.
- Active OpenClaw runs and Paperclip work remain stop vetoes.
- A small periodic backend sweep applies the existing trial-idle policy from
  durable timestamps; conditional container state updates make duplicate sweeps
  safe during rolling deploys.
- Founder/paid always-on policy remains independent of UI connectivity.

Tauri token delivery is triggered by Clerk token availability/refresh and native
node state, never by AppSync connectivity.

### Scale envelope

AppSync fanout, DynamoDB storage, the stream projector, and FIFO message groups
scale across rooms without keeping browser connections in the backend. The founder
MVP deliberately keeps the existing backend ECS service at `desiredCount: 1` and
processes distinct room-agent queue groups concurrently behind a bounded
semaphore. This avoids pretending the current process-local OpenClaw connection
pool and node/operator relay callbacks are horizontally shareable.

Raise the backend task count only after a load test reaches this single-task
ceiling. The first scale step is to move `openclaw-work.fifo` consumption into its
own ECS worker service while leaving HTTP stateless; the retained node/operator
relay then gets explicit routing or its own service. No tenant-specific queues or
new event bus are needed for that split.

## 12. Paperclip contract

- A room or thread can link to one or more goal, project, issue, approval, run,
  artifact, or work-product IDs.
- Creating work from chat is explicit and previews destination and assignee.
- Paperclip routines can ask assigned agents to post progress/results through the
  normal Lightbulb channel; they do not write the room table directly.
- Private room text is never copied into a wider Paperclip object without an
  explicit audience-expanding action.
- `paperclip-exec-creds` remains the credential boundary for agent exec children.

## 13. Parallel PR packets and merge order

The following self-contained implementation plans share this contract:

1. `2026-09-09-native-multiplayer-aws-foundation.md`
2. `2026-09-09-native-multiplayer-openclaw-channel.md`
3. `2026-09-09-native-multiplayer-backend.md`
4. `2026-09-09-native-multiplayer-frontend.md`
5. `2026-09-09-native-multiplayer-rollout.md`

Packets 1–4 can be developed in parallel. The merge sequence is infrastructure
(A), OpenClaw plugin/image (B), immutable image promotion (E), backend/config and
legacy deletion (C), then frontend and client-socket deletion (D). The rollout
packet performs one production dispatch, rerolls the founder container, and runs
the complete founder acceptance suite.

## 14. Founder-release non-goals

- external A2A federation;
- replacing the machine relay with AWS IoT Core;
- end-to-end encryption between members;
- file upload, reactions, edit/delete, search, or mobile feature parity;
- automatic Paperclip task creation for every conversation;
- a custom agent action DSL;
- per-agent OS isolation inside one company container; and
- DynamoDB write sharding before a measured hot-room threshold.

## Sources

### OpenClaw 9.3

- [Ambient room events](https://docs.openclaw.ai/channels/ambient-room-events)
- [Broadcast groups](https://docs.openclaw.ai/channels/broadcast-groups)
- [A2A channel](https://docs.openclaw.ai/channels/a2a)
- [Channel routing and session keys](https://docs.openclaw.ai/channels/channel-routing)
- Pinned source:
  `extensions/a2a/src/inbound.ts`, `extensions/a2a/src/channel.ts`,
  `extensions/a2a/src/gateway.ts`, and `src/channels/inbound-event/context.ts` at
  fork tag `v2026.9.3-isol8.2`.

### AWS

- [What is AWS AppSync Events?](https://docs.aws.amazon.com/appsync/latest/eventapi/event-api-welcome.html)
- [AppSync Event API authorization](https://docs.aws.amazon.com/appsync/latest/eventapi/configure-event-api-auth.html)
- [AppSync event handlers](https://docs.aws.amazon.com/appsync/latest/eventapi/event-handlers-overview.html)
- [AppSync DynamoDB integrations](https://docs.aws.amazon.com/appsync/latest/eventapi/data-source-integrations.html)
- [AppSync DynamoDB `BatchGetItem`](https://docs.aws.amazon.com/appsync/latest/eventapi/dynamodb-batchgetitem.html)
- [Publishing AppSync events over HTTP](https://docs.aws.amazon.com/appsync/latest/eventapi/publish-http.html)
- [AppSync protocol/path restrictions](https://docs.aws.amazon.com/appsync/latest/eventapi/event-api-websocket-protocol.html)
- [AppSync quotas](https://docs.aws.amazon.com/general/latest/gr/appsync.html)
- [DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
- [DynamoDB read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html)
- [DynamoDB Streams with Lambda](https://docs.aws.amazon.com/lambda/latest/dg/with-ddb.html)
- [EventBridge Pipes](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-pipes.html)
- [DynamoDB Streams as an EventBridge Pipes source](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-pipes-dynamodb.html)
- [EventBridge Pipes input transformation](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-pipes-input-transformation.html)
- [SQS FIFO delivery](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-understanding-logic.html)
- [SQS KMS permissions](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-key-management.html)
- [API Gateway WebSocket quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-execution-service-websocket-limits-table.html)

### Clerk

- [JWT templates](https://clerk.com/docs/guides/sessions/jwt-templates)
- [Session token generation](https://clerk.com/docs/reference/backend/sessions/get-token)
- [Backend API 2026-05-12 schema](https://raw.githubusercontent.com/clerk/openapi-specs/refs/heads/main/bapi/2026-05-12.yml)
