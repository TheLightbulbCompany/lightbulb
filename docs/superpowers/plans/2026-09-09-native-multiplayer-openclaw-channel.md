# Native multiplayer OpenClaw channel implementation plan

> **For agentic workers:** use `superpowers:subagent-driven-development` for this
> packet. Work only in the Lightbulb repository. Do not patch the agent fork unless
> the pinned-9.3 qualification test proves a missing public SDK seam.

**Goal:** Ship a first-party `lightbulb` OpenClaw channel in the existing extended
image so each Lightbulb room maps to one native session per participating agent,
supports mentioned and ambient turns, and returns attributed agent messages through
the canonical Lightbulb API.

**Architecture:** One channel account is configured per local agent. The backend
posts an authenticated canonical room event to that account's private plugin route.
The plugin builds native OpenClaw channel context and dispatches through the 9.3
channel runtime. Automatic mentioned replies and native `message` tool sends both
post through the same hook-token-authenticated backend endpoint.

**Tech stack:** OpenClaw 2026.9.3 public plugin SDK, TypeScript/ESM, Node test runner,
existing Lightbulb extended-image build and runtime qualification.

**Spec:**
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`

## Ownership and prerequisites

This packet owns:

- `apps/infra/openclaw/plugins/lightbulb-channel/`
- the Lightbulb-channel COPY/qualification lines in
  `apps/infra/openclaw/Dockerfile`
- Lightbulb-channel checks in `.github/workflows/build-openclaw-image.yml`

It must not edit room persistence, browser transport, AWS messaging resources, or
`openclaw-version.json`. The rollout packet changes the deployed extended-image tag
after this image has been built and qualified.

Before coding, read these exact files at fork tag `v2026.9.3-isol8.2`:

```text
extensions/a2a/src/inbound.ts
extensions/a2a/src/channel.ts
extensions/a2a/src/gateway.ts
src/channels/inbound-event/context.ts
src/plugins/types.ts
```

The accepted public seams are
`resolveChannelInboundRouteEnvelope`,
`resolveStableChannelMessageIngress`,
`buildChannelInboundEventContext`, `channelRuntime.inbound.dispatch`,
`defineChannelPluginEntry`, `createChatChannelPlugin`,
`defineChannelMessageAdapter`, `buildChannelOutboundSessionRoute`,
`createIngressEffectOnce`, `readJsonWebhookBodyOrReject`, and
`registerPluginHttpRoute({auth:"gateway"})`.

## Frozen channel contract

For friendly agent Friday, config account ID is its canonical Paperclip/OpenClaw
UUID and `friday` is its server-owned mention handle. Its exact route is:

```text
POST /plugins/lightbulb-channel/inbound/<agentId>
Authorization: Bearer <gateway token>
```

Its room session key is:

```text
agent:<agentId>:lightbulb:channel:<roomId>
```

The request/response and native outbound bodies are exactly those in sections 5
and 6 of the architecture spec. No browser can reach the route without the
container gateway token.

## Task 1: Create the smallest complete channel plugin

Create:

```text
apps/infra/openclaw/plugins/lightbulb-channel/package.json
apps/infra/openclaw/plugins/lightbulb-channel/package-lock.json
apps/infra/openclaw/plugins/lightbulb-channel/openclaw.plugin.json
apps/infra/openclaw/plugins/lightbulb-channel/tsconfig.json
apps/infra/openclaw/plugins/lightbulb-channel/vitest.config.ts
apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts
apps/infra/openclaw/plugins/lightbulb-channel/src/index.test.ts
apps/infra/openclaw/plugins/lightbulb-channel/dist/index.js
```

Follow the existing `lightbulb-marketplace` plugin's TypeScript/build layout; do
not hand-maintain a second JavaScript source. Use no runtime npm dependency.
Import only the host's public `openclaw/plugin-sdk/*` entries and platform
`fetch`.

- [ ] Manifest ID and channel ID are both `lightbulb`; package version is
  `0.1.0`; peer OpenClaw range is `>=2026.9.3` and the development pin is exact
  `2026.9.3`.
- [ ] Declare `channels: ["lightbulb"]` and
  `channelConfigs.lightbulb` in `openclaw.plugin.json`, including its label,
  description, and strict JSON Schema. The schema accepts only top-level
  `enabled` and `accounts`; each dynamic account accepts only `enabled`. Reuse
  that same object through public `buildJsonChannelConfigSchema` so manifest
  validation and runtime validation cannot drift without adding Zod/TypeBox.
- [ ] Also declare the manifest-required top-level
  `configSchema: {type:"object", properties:{}, additionalProperties:false}` for
  `plugins.entries.lightbulb.config`. This is separate from the channel config
  schema and accepts no plugin-private settings.
- [ ] The default export uses `defineChannelPluginEntry`; construct the channel
  with `createChatChannelPlugin`. Do not use the bundled-only
  `defineBundledChannelEntry` helper copied by in-tree channels.
- [ ] Accept only lowercase OpenClaw agent/account IDs matching the host's agent
  ID validation and RFC-4122 UUID room IDs. Inbound event IDs are canonical
  message UUIDs. Outbound event IDs are bounded opaque host idempotency values,
  because prepared/durable queue IDs are not guaranteed to be RFC UUIDs.
- [ ] Resolve `ISOL8_API_URL`, `ISOL8_OWNER_ID`, and `OPENCLAW_HOOK_TOKEN` once per
  send. Trim one trailing slash; fail with a named configuration error when any
  value is absent. Never log an owner, token, Authorization header, or body.
- [ ] Define the channel as group-capable and normalize outbound target
  `lightbulb:<roomUuid>` to the room UUID.
- [ ] Resolve outbound sessions with `buildChannelOutboundSessionRoute`, channel
  `lightbulb`, the current account/agent ID, group peer kind `channel`, and exact
  recipient session routing.
- [ ] Register the native message adapter with text capability. Its send method
  posts the frozen outbound body to
  `/api/v1/internal/messaging/agent-messages/<ISOL8_OWNER_ID>`, derives `agentId`
  from `ctx.accountId`, derives `roomId` from `ctx.to`, maps `replyToId` and
  `threadId`, and requires backend `201` with exact
  `{messageId,roomId,createdAt}`. Validate all three fields and map the response to
  exact 9.3 `MessageReceipt` fields
  `{primaryPlatformMessageId:messageId, platformMessageIds:[messageId],
  parts:[{platformMessageId:messageId,kind:"text",index:0}],
  sentAt:Date.parse(createdAt)}`. An equal idempotent replay returns the same `201`
  body. The request body contains no mentions or run ID: the exact 9.3 adapter context
  exposes neither. Backend roster parsing resolves exact server-owned
  `@<mentionHandle>` tokens to canonical UUIDs, and canonical agent writes store
  `runId: null`.
- [ ] Declare
  `message.durableFinal.capabilities = {text:true, replyTo:true, thread:true,
  messageSendingHooks:true, reconcileUnknownSend:true}`. Set
  `automaticUnknownSendReconciliation: true` and
  `reconcileUnknownSendKinds: {text:true}`. Registration alone does not make 9.3
  inbound finals use the adapter; this capability/reconciliation contract forces
  normal one-text sends through the durable queue and supplies a stable queue ID.
- [ ] Derive `eventId` from non-empty `ctx.deliveryQueueId` first as
  `<deliveryQueueId>:<deliveryPartIndex ?? 0>`; otherwise use non-empty
  `ctx.preparedMessageId`. Fail closed if neither stable host value exists; never
  mint a random retry key or configure a channel text chunker.
- [ ] Implement `reconcileUnknownSend(ctx)` by querying the owner-scoped internal
  receipt endpoint for event ID `<ctx.queueId>:0`. A matching canonical receipt
  is `200` with exact `{messageId,roomId,createdAt}` and maps to the same normalized
  `MessageReceipt`; `404` returns `not_sent`, and a
  transient/invalid response returns `unresolved` without logging payloads. This
  is the same key the original adapter send used, so recovery cannot create a
  second canonical post.
- [ ] Do not add a structured action protocol, alternate transcript, timer,
  scheduler, room database, or A2A peer.

Add Vitest tests for malformed targets, missing environment, URL normalization,
server-bound owner/agent identity, hook auth, prepared and queue/part event IDs,
missing stable event ID, sent/not-sent/unresolved reconciliation, reply/thread
mapping, omitted mentions/run ID, and non-2xx backend errors. Commit only after the
image workflow proves `npm test` and `npm run build` regenerates `dist/index.js`
from `src/index.ts`.

Commit as `openclaw: add native Lightbulb channel adapter`.

## Task 2: Register per-agent private inbound routes

- [ ] In the channel account startup hook, register exactly one
  `auth: "gateway"`, `match: "exact"` HTTP route at
  `/plugins/lightbulb-channel/inbound/<accountId>`. Per-account paths avoid the
  duplicate fixed-route problem when Friday and Gauge run in one gateway.
- [ ] Register with `pluginId: "lightbulb"`, stable source
  `lightbulb-channel-gateway`, the current `accountId`, and
  `replaceExisting: true`, `throwOnFailure: true`; same-owner lifecycle refresh
  replaces its stale registration while an unrelated owner conflict fails rather
  than silently returning a no-op unregister function. Emit the standard
  starting/ready/stopped account status patches. No gateway-dispatch capability
  is declared because the handler calls the channel runtime, not a gateway RPC
  method.
- [ ] The handler closure, not the body, owns the target account/agent. If the
  body includes `targetAgentId`, require it to equal the closure's account ID.
- [ ] Call public `applyBasicWebhookRequestGuards({req, res,
  allowMethods:["POST"], requireJsonContentType:true})` before reading. It owns
  exact 405/`Allow: POST` and 415 behavior. Preserve the body helper's 413 response
  for size and 408 for timeout; schema/JSON validation is 400. Never collapse
  these failures into 202 or log the body.
- [ ] Parse only with `readJsonWebhookBodyOrReject` from
  `openclaw/plugin-sdk/webhook-ingress`, passing `maxBytes: 1_048_576`, a
  30-second timeout, and non-empty JSON. Validate the complete version-1 body
  before routing. Reject unknown fields at the trust boundary and separately cap
  canonical text at 100,000 UTF-8 bytes, history at 50 messages, participants at
  128, and the complete serialized request at 1 MiB.
- [ ] Resolve the native route with channel `lightbulb`, account ID equal to the
  agent, peer `{kind:"channel", id:room.id}`, and the routed agent equal to that
  account.
- [ ] Call `resolveStableChannelMessageIngress` with `groupPolicy: "open"`, the
  canonical event ID in `contextBinding.messageId`, group conversation ID,
  host-owned account identity, and the supplied kind in
  `contextBinding.inboundEventKind`. Reject any admission other than `dispatch`.
  The explicit open policy is safe
  only because the gateway route and Lightbulb's strong membership check are the
  boundary; omitting it makes 9.3 default every group ingress to disabled.
- [ ] Call `buildChannelInboundEventContext` with structured sender,
  conversation, route, reply, mentions, participant facts, reply/thread facts,
  and `message.inboundHistory`. Map every canonical history row to exact 9.3
  `HistoryEntry` fields `{sender: author.displayName, body: text, timestamp:
  Date.parse(createdAt), messageId}`; never assign canonical DTOs directly. Put
  the same supplied event kind in `message.inboundEventKind`; omission would make
  the builder default ambient work to `user_request`. Put room/roster facts in
  `ChannelStructuredContext`; never concatenate a fake system or transcript
  prefix into message text.
- [ ] Set mention facts from the server-resolved mention list. A
  `user_request` is command-authorized only for a human sender; agent and ambient
  events set `command: false` and
  `extra.CommandInterpretationSuppressed: true`, matching the pinned A2A security
  boundary, so they cannot execute slash or plugin commands.
- [ ] Create one account-scoped guard with `createIngressEffectOnce` from
  `openclaw/plugin-sdk/ingress-effect-once`: `pluginId: "lightbulb"`, namespace
  prefix `lightbulb-inbound-<accountId>`, eight-day TTL, 100,000 persisted entries,
  and a 5,000-entry memory cache. Run native dispatch as effect `dispatch` under
  the canonical event ID. A committed replay returns HTTP 202 without dispatch;
  a pre-effect failure releases for retry; persistent-state failure fails closed.
- [ ] Dispatch through `channelRuntime.inbound.dispatch`. Normal `user_request`
  final delivery uses a core-managed durable delivery targeting
  `lightbulb:<roomId>`, carrying the canonical reply/thread facts and requiring
  `{text:true, replyTo:true, thread:true, messageSendingHooks:true,
  reconcileUnknownSend:true}`. Its payload preparation admits only
  `info.kind === "final"`; suppress block/tool/non-final payloads. The required
  direct `deliver` fallback throws so a final can never bypass the durable message
  adapter and its stable queue ID. For
  `room_event`, suppress the automatic final entirely and rely on OpenClaw's native
  strict ambient behavior: only a native `message` tool send becomes visible.
- [ ] When `sender.kind == "agent"`, pass native `botLoopProtection` facts to
  dispatch: `scopeId="lightbulb:<ISOL8_OWNER_ID>"`, `conversationId=room.id`,
  `senderId=sender.id`, `receiverId=<accountId>`, `eventId=<canonical messageId>`,
  `defaultsConfig=cfg.channels?.defaults?.botLoopProtection`, and
  `defaultEnabled=true`. Human/system ingress passes no pair-loop facts. Keep
  one workspace scope for every local account so A->B and B->A hit the same
  unordered-pair guard. Keep upstream's default 20 events/60 seconds/60-second
  cooldown; do not implement a second Lightbulb loop counter.
- [ ] Return HTTP 202 only after native admission/dispatch succeeds. Map invalid
  input to 400, method to 405, content type to 415, body limit/timeout to 413/408,
  auth to the gateway layer, admission refusal to 409, and internal failure to
  500. A persistent replay is also 202. Do not claim a new turn was accepted if
  `dispatch.dispatched` is false.
- [ ] Unregister the route in the account shutdown `finally` block and wait on
  the provided abort signal, matching the A2A channel lifecycle.

Extend `src/index.test.ts` with two-account route uniqueness, deterministic session
keys, structured multi-human context, explicit mention classification, ambient
classification, history propagation, automatic mentioned reply, suppressed
ambient final, peer `message` send, self-echo omission input, native bot-pair
facts including symmetric Friday/Gauge scope, retry event-ID deduplication,
duplicate route registration failure, duplicate ingress, unknown agent, lifecycle
status, and shutdown cleanup checks. Prove a `user_request` final reaches the
registered adapter with a stable prepared/queue ID, a `room_event` final does not,
a native `message` tool send does, and the direct-delivery fallback fails closed.
Add a restart-persistence test and one explicit crash-window
test documenting at-least-once semantics: process death after dispatch but before
the persistent commit can execute the model again. Canonical outbound idempotency
and side-effecting tool idempotency—not this guard—contain that window.

Commit as `openclaw: dispatch Lightbulb rooms through native channel ingress`.

## Configuration contract handed to the backend packet

PR B must not emit plugin configuration before the image containing the plugin is
published and promoted. The backend PR owns `apps/backend/core/containers/config.py`
and implements this frozen contract after promotion PR E:

- [ ] Add `lightbulb` to `plugins.entries` with `enabled: true`.
- [ ] Build `channels.lightbulb.accounts` from the same validated/sorted agent
  roster used by `_build_agents_list`. Each account key is the local agent ID and
  contains only `enabled: true`.
- [ ] Add one deterministic binding per roster entry:

  ```json
  {
    "agentId": "<canonical-agent-uuid>",
    "match": {
      "channel": "lightbulb",
      "accountId": "<canonical-agent-uuid>",
      "peer": { "kind": "channel", "id": "*" }
    }
  }
  ```

- [ ] Preserve unrelated Slack/Discord/channel bindings; deduplicate on the full
  Lightbulb binding key.
- [ ] Set `messages.groupChat.unmentionedInbound: "room_event"` and
  `messages.groupChat.visibleReplies: "automatic"`. OpenClaw keeps ambient
  `room_event` final text strict even when normal addressed requests use automatic
  visible replies; this makes mentions reliable and ambient interjections
  deliberate.
- [ ] Keep `historyLimit: 50`. Do not enable global
  `tools.sessions.visibility: "all"`, broadcast config, WhatsApp, or A2A.
- [ ] Set `tools.sessions.visibility: "agent"` and
  `tools.agentToAgent.enabled: false`. Pinned 9.3 otherwise defaults to global
  cross-agent session access; teammate communication in this release goes through
  the canonical room, not private `sessions_send`.
- [ ] Set top-level `session.groupScope: "per-group"` and assert that inbound and
  outbound routing both resolve exactly
  `agent:<agentId>:lightbulb:channel:<roomId>`. Do not rely only on 9.3's current
  default.

Its focused config test covers
Friday-only, Friday+Gauge, sorted/deduplicated bindings, empty-roster fallback,
ambient config, preservation alongside Slack, and exact plugin enablement.
It also covers explicit group scope, inbound/outbound session-key equality, and
the explicit cross-agent session-tool denial.

## Task 3: Bake and qualify the plugin in the existing 9.3 image

- [ ] Add one root-owned Docker COPY from
  `plugins/lightbulb-channel` to
  `/app/dist/extensions/lightbulb-channel`, beside the existing marketplace and
  credential plugins.
- [ ] Update `apps/infra/openclaw/.dockerignore` only if its current allow rules
  exclude the new directory.
- [ ] In `.github/workflows/build-openclaw-image.yml`'s `image-proof-tests` job,
  run `npm ci`, `npm test`, `npm run build`, and a clean-diff assertion for the
  committed `dist/`. The built image need not carry Vitest or dev dependencies;
  its existing runtime qualification imports/starts the baked plugin instead.
- [ ] Add `lightbulb-channel` version `0.1.0` to the built-image package/version
  manifest assertion.
- [ ] Extend production-shape runtime qualification to assert:
  1. the plugin loads on exact OpenClaw 2026.9.3;
  2. Friday and Gauge accounts both start;
  3. both unique gateway-auth routes register;
  4. the native session key shape is exact; and
  5. config validation accepts a checked-in fixture matching the backend handoff
     contract above.
- [ ] Assert the effective production tool policy exposes the native `message`
  tool to the configured agents; ambient interjection is not qualified if the
  tool is merely present in the image but denied by effective config.
- [ ] Do not change the `FROM` fork tag, fork commit, or upstream plugin pins.
  This is an extended-image feature on `v2026.9.3-isol8.2`, not fork
  `v2026.9.3-isol8.3`.

Commit as `openclaw: bake and qualify Lightbulb channel`.

## Task 4: Finish the image PR

- [ ] Push and rely on the existing `image-proof-tests` pull-request job for the
  plugin tests, exact-9.3 SDK compile, config fixture validation, and image-proof
  changes. The expensive image build/push is intentionally skipped on pull
  requests and runs only after merge to `main`.
- [ ] In the PR description state the exact fork base
  `v2026.9.3-isol8.2` and explicitly state "no fork patch." Do not invent
  artifact digests or update dev/prod image pins in this PR.

The PR is ready to merge when source proof passes. Its merge triggers the full
Docker build, built-image qualification, SOCI conversion, immutable ECR push, and
digest evidence used by rollout PR E. If—and only if—the compile/runtime proof
fails because one named SDK symbol is not exported, stop this packet, record the
exact missing symbol and failing proof, then open the smallest generic upstreamable
fork seam as a separate PR. Do not copy OpenClaw internals into the Lightbulb
plugin.
