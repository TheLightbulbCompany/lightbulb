# Native multiplayer frontend implementation plan

> **For agentic workers:** use `superpowers:subagent-driven-development` for this
> packet. Reuse the reviewed UI in PR #2404, but replace its obsolete native-room
> contracts with the frozen Lightbulb HTTP/AppSync contract.

**Goal:** Ship the Slack-like room experience on web, preserve mobile access, move
all browser/mobile commands to HTTP, and move all live delivery to AppSync Events
without rewriting the existing chat/run/tool renderers.

**Architecture:** A small dependency-free client implements AWS's documented
AppSync Events WebSocket protocol over the web/React Native native `WebSocket`.
It subscribes to one private user channel and the active room channel using a
Clerk template token. Shared HTTP clients own durable messages and runtime RPC;
room state subscribes first and reconciles by canonical sequence over HTTP.

**Tech stack:** React 19, Next.js 16, Expo/React Native, SWR, Clerk, native
`WebSocket`, Vitest/Jest, existing Lightbulb API client.

**Spec:**
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`

## Ownership and prerequisites

This packet owns:

```text
packages/shared/src/live/
packages/shared/src/messaging/
packages/shared/src/gateway/useGatewayConnection.ts (delete)
packages/shared/src/hooks/useGatewayRpc.ts (replace/rename)
packages/shared/src/hooks/useAgents.ts
packages/shared/src/index.ts
apps/frontend/src/hooks/useLiveEvents.tsx
apps/frontend/src/hooks/useGateway.tsx (delete)
apps/frontend/src/lib/teamsSubscription.ts (delete)
apps/frontend/src/lib/api.ts
apps/frontend/src/hooks/useAgentChat.ts
apps/frontend/src/components/messaging/
apps/frontend/src/components/teams/TeamsEventsProvider.tsx
apps/frontend/src/hooks/useExecApprovals.tsx
apps/frontend/src/hooks/useQuestions.tsx
apps/frontend/src/hooks/useProgressCard.ts
apps/frontend/src/components/agents/useRosterEntries.ts
apps/frontend/src/components/notifications/RunNotifications.tsx
apps/frontend/src/components/workspace/useBrowserToolAutoOpen.ts
apps/frontend/src/components/workspace/WorkspaceShell.tsx
apps/mobile/src/providers/live-events.tsx
apps/mobile/src/providers/gateway.tsx (delete)
apps/mobile/src/app/(app)/agent/[agentId].tsx
focused client tests for those paths
```

It must not edit backend, CDK, OpenClaw plugin/image, or deployment workflows.

PR [#2404](https://github.com/TheLightbulbCompany/lightbulb/pull/2404) at reviewed
head `0d40b74067896e221436a5704dae408590786dda` contains reusable controlled
presentation components:

```text
apps/frontend/src/components/messaging/RoomDirectory.tsx
apps/frontend/src/components/messaging/RoomPanel.tsx
apps/frontend/src/components/messaging/RoomComposer.tsx
apps/frontend/src/components/messaging/messaging.css
```

Reuse the four presentation components, their accessibility/uncertain-send tests,
and the user+organization workspace cache isolation. Do **not** reuse
`packages/shared/src/messaging/presence.ts`: it derives human presence from the
`native_human` socket inventory that this plan deletes. The MVP has ephemeral
typing only and does not claim online presence. Remove the PR's old
OpenClaw-canonical design/goal documents during rebase; do not consume its
`sessions.room.*` wire assumptions or treat synthetic fixture data as backend
evidence.

## Frozen client contract

The client subscribes to:

```text
/users/<opaque key returned by GET /messaging/live-config>
/rooms/<active room UUID>/<deliveryKey returned by room detail GET>
```

The client never publishes to AppSync. Every durable command uses HTTP. Every
canonical message is ordered by `messageSeq`, not arrival time.

Public JSON is camelCase. `RoomKind` is exactly `channel | dm | group-dm`;
threads are nested message views, and there is no public/private room discovery in
the founder release. Import the exact event registry and payload types from
architecture spec section 8 rather than declaring packet-local event strings.

## Task 1: Add the dependency-free AppSync Events client

- [ ] Add no npm dependency. Use the native `WebSocket`, `JSON`, `btoa`, timers,
  and the existing Clerk token getter on both web and React Native.
- [ ] Create:

  ```text
  packages/shared/src/live/types.ts
  packages/shared/src/live/appsyncEvents.ts
  packages/shared/src/live/appsyncEvents.test.ts
  packages/shared/src/live/useLiveEvents.ts
  packages/shared/src/live/useLiveEvents.test.tsx
  ```

- [ ] Define the frozen `LiveEvent` discriminated union and move the existing
  `ChatIncomingMessage` payload union under `packages/shared/src/live/`, changing
  only `agent_id` to `agentId` and `session_id` to `sessionId` so current
  stream/tool/run reducers do not need a second rewrite.
- [ ] `AppSyncEventsClient` accepts the exact HTTP and realtime endpoints returned
  by `/messaging/live-config`, a function that calls Clerk
  `getToken({template:"lightbulb-appsync"})`, and event/status callbacks. It exposes
  only desired-channel replacement and close; there is no publish method.
- [ ] For every connection, fetch a fresh non-null token; Base64URL-encode
  `{"Authorization":token,"host":new URL(httpEndpoint).host}` and open the
  realtime endpoint with subprotocols
  `["header-<encoded>","aws-appsync-event-ws"]`. Send
  `{"type":"connection_init"}` and accept no application data before a valid
  `connection_ack`.
- [ ] After the ACK, subscribe to each desired path with a unique UUID and a fresh
  `{"Authorization":token}` authorization object. Mark a subscription live only
  after `subscribe_success`; surface `subscribe_error` without exposing the token
  or server payload. Parse each string in `data.event` as one validated
  `LiveEvent` and deduplicate by `eventId`.
- [ ] Maintain one user subscription and zero/one active-room subscription. Close
  replaced subscriptions on sign-out, owner switch, room switch, key rotation,
  and unmount. Do not subscribe to every room or add a workspace channel.
- [ ] Store `connectionTimeoutMs` from the ACK, reset one watchdog on every `ka`,
  and close/reconnect if it expires. On network close, use full-jitter exponential
  backoff starting at 500 ms and capped at 30 seconds; reset after a successful
  ACK. Recreate desired subscriptions with fresh Clerk tokens, then invoke HTTP
  reconciliation. Do not send client pings or add a periodic token-refresh timer.
- [ ] Ignore stale events from superseded socket generations and stale
  subscription IDs. Send `unsubscribe` before replacing a live subscription when
  the socket permits it, but correctness never depends on its acknowledgement.
- [ ] Expose `liveConnected`, `lastEventAt`, `onEvent`, and `onChatMessage`.
  HTTP availability remains independent; do not recreate `isParked`, `send`,
  `sendChat`, pending RPC maps, application pings, or backend connection IDs.

Tests use one fake `WebSocket` and fake timers. Cover exact subprotocol encoding,
ACK gating, subscribe authorization/acknowledgement, one user/one room subscription,
data-array parsing, duplicate/malformed events, keepalive expiry, jittered reconnect,
fresh token on reconnect, stale-generation rejection, owner/room/key turnover,
unsubscribe, reconciliation, sign-out, and token/error redaction. Tests make no
network calls.

Commit as `frontend: add AppSync Events protocol client`.

## Task 2: Add shared messaging and runtime HTTP clients

Create:

```text
packages/shared/src/messaging/types.ts
packages/shared/src/messaging/client.ts
packages/shared/src/messaging/client.test.ts
packages/shared/src/runtime/client.ts
packages/shared/src/runtime/client.test.ts
```

- [ ] Build both clients on the existing `ApiMethods`/authenticated fetch in
  `packages/shared/src/api/client.ts`; do not add another fetch wrapper or auth
  cache.
- [ ] Implement typed methods for live config, room list/get/create/update,
  membership, history, send, read cursor, typing, abort, steer, approval/question
  resolution, runtime RPC, and operation status. Import/reproduce exactly the
  section-6 `LiveConfig`, room/member, message page, cursor, typing, abort/steer,
  runtime-operation, success/status, and error contracts; add no frontend aliases.
- [ ] Preserve `GatewayRpcError`'s code/retryable/retryAfter/details semantics as
  `RuntimeRpcError` and update imports. Do not flatten backend error bodies.
- [ ] Change `packages/shared/src/hooks/useGatewayRpc.ts` to
  `useRuntimeRpc.ts`. Preserve SWR keys, cold-start retry, event aliases,
  module-wide push-throttling, and `No container` behavior; replace `sendReq` with
  the shared HTTP runtime client and replace `isParked` with normal SWR state.
- [ ] Update `useAgents.ts` to handle `agents.create` returning an operation ID:
  listen for the matching private-user operation event and reconcile through
  `GET /runtime/operations/{id}` after reconnect.
- [ ] Export the new clients/types/hooks from `packages/shared/src/index.ts`.

Tests cover bearer auth, endpoint/body shapes, idempotency IDs, structured errors,
async operation completion, event-driven invalidation, reconnect polling, and
forbidden server-owned fields.

Commit as `frontend: move messaging and runtime commands to HTTP`.

## Task 3: Replace the web Gateway provider

- [ ] Add `LiveEventsProvider` in
  `apps/frontend/src/hooks/useLiveEvents.tsx`; update the root provider import and
  all consumers to use it, then delete `useGateway.tsx`. Keep compatibility event
  callbacks only at the reducer boundary, not a compatibility socket object.
- [ ] Fetch `/messaging/live-config` after Clerk authentication and owner
  resolution. Treat HTTP as ready even if AppSync is reconnecting; render a small
  "Reconnecting live updates" state without disabling sends.
- [ ] Delete browser idle park/unpark, application ping, old gateway-socket retry,
  and browser-connection-driven container wake logic. Sending a cost-bearing
  command wakes the runtime through the backend; the one AppSync protocol client
  owns only its required transport reconnect.
- [ ] Preserve onboarding/status SWR invalidation from private user events.
- [ ] Decouple Tauri `send_auth_token` from browser live connection state. Trigger
  it from signed-in user/token availability, desktop permission revision, Clerk
  token refresh, and native `node:status`; the Rust node socket remains unchanged.
- [ ] Replace `gatewayPark`, `useGateway.connectRace`, and
  `useGateway.statusChanged` tests with live-subscription/status/Tauri tests. Add a
  test proving Tauri receives auth when AppSync is disconnected.

Commit as `frontend: replace browser gateway socket with live events`.

## Task 4: Connect the reviewed room UI to canonical state

- [ ] Rebase PR #2404's messaging components and cache isolation onto current
  `main`; retain their controlled-prop boundary and tests.
- [ ] Change PR #2404's room choices from `public | private | dm | group` to the
  frozen membership-scoped `channel | dm | group-dm`. Remove public discovery/
  join controls and keep threads as nested message collections.
- [ ] Add `apps/frontend/src/hooks/useRooms.ts` and its focused test. Use SWR for
  room directory/metadata and a reducer for active-room canonical messages.
- [ ] On active-room entry: fetch metadata/delivery key, connect the room
  subscription, then fetch history after the last canonical sequence. This order
  closes the fetch/subscribe race. Repeat after reconnect or key rotation.
- [ ] While a room is active, revalidate its metadata over authenticated HTTP
  every 60 seconds. If its `roomRevision` or `deliveryKey` changes, close the old
  subscription, subscribe to the returned key, and reconcile by sequence. A 403
  closes the room and evicts its messages immediately. This timer is the
  revocation fallback for a missed private-user rotation event; it is not a
  second live transport or a polling replacement for AppSync.
- [ ] On a live message:
  1. ignore a duplicate `eventId`/`messageId`;
  2. insert by `messageSeq`;
  3. if `messageSeq > lastSeq + 1`, fetch `afterSeq=lastSeq`; and
  4. never invent missing canonical messages from streaming chunks.
- [ ] On send, generate one `clientMessageId`, retain draft/request evidence, and
  optimistically render it. Replace it with the HTTP response's canonical message.
  If the request outcome is uncertain, keep the same ID/body and reconcile before
  offering retry; never mint a new ID automatically.
- [ ] Change `RoomPanel` so the main and thread composers are enabled whenever
  authenticated HTTP is available, including while `liveConnected` is false.
  The reconnect banner is informational; it cannot gate HTTP send or abort.
- [ ] Render multiple humans and agents from canonical author fields. Agents may
  type/stream simultaneously; key run/tool state by `(roomId, agentId, runId)` so
  Friday and Gauge cannot overwrite one another.
- [ ] Display and insert the server-owned `RoomMember.mentionHandle` for agent
  mentions while submitting the canonical UUID in structured `mentions`. Never
  derive identity from display text or assume the friendly name is the OpenClaw
  agent ID.
- [ ] Connect room directory, create room, participant selection, listen-mode
  controls, room panel, thread/reply context, composer, unread cursor, typing, and
  access-removed state to the HTTP contract.
- [ ] Make ordinary conversation available without a Paperclip issue. Add explicit
  "Link to work" affordances using stable Paperclip object IDs already available
  in Teams; do not auto-create work from each room/message.

Tests cover two humans, Friday+Gauge, concurrent runs, peer agent replies visible to
the other agent's session through canonical history, out-of-order/duplicate events,
gap recovery, reconnect, the 60-second metadata revocation fallback, room-key
rotation, access removal, optimistic idempotency,
uncertain send, HTTP send during AppSync reconnect, reply/thread isolation,
keyboard/focus, and narrow/mobile layout.

Commit as `frontend: connect multiplayer rooms to canonical messaging`.

## Task 5: Preserve current stream/tool experiences on AppSync

Update these consumers to use the new provider without changing their payload
reducers:

```text
apps/frontend/src/hooks/useAgentChat.ts
apps/frontend/src/components/agents/useRosterEntries.ts
apps/frontend/src/components/notifications/RunNotifications.tsx
apps/frontend/src/components/workspace/useBrowserToolAutoOpen.ts
apps/frontend/src/components/teams/TeamsEventsProvider.tsx
apps/frontend/src/hooks/useExecApprovals.tsx
apps/frontend/src/hooks/useQuestions.tsx
apps/frontend/src/hooks/useProgressCard.ts
```

- [ ] Replace raw `agent_chat` with canonical room message HTTP.
- [ ] Replace raw `agent_abort` and `agent_steer` with dedicated HTTP endpoints.
- [ ] Replace `/btw` raw socket send with an explicitly attributed room message;
  do not keep a hidden second transcript.
- [ ] Route chat chunks/thinking/tool/run events by room+agent+run and preserve
  existing coalesced rendering.
- [ ] Route approvals, questions, progress, health/config, and Teams invalidations
  from the private user channel. Replace `teams.subscribe/unsubscribe` and delete
  reference counting.
- [ ] Keep browser VNC transport and Paperclip proxy UI transport untouched.

Update the existing focused tests named in the transport inventory:
`useAgentChat.{demux,queue,reconnect,sessionRouting,steer,cancel,streamPaint,
streamResync,subStreams,historyRevalidate,wakeRace}`, approval/question/progress,
roster, notifications, browser auto-open, and Teams provider. Replace only socket
fixtures with HTTP/AppSync fixtures; retain behavior assertions.

Commit as `frontend: route agent activity through room events`.

## Task 6: Move mobile off the custom browser socket

- [ ] Add `apps/mobile/src/providers/live-events.tsx` with the same shared live
  provider using Clerk Expo `getToken` and AppState-driven subscription teardown/
  restart. Switch the app root to it and delete `providers/gateway.tsx`.
- [ ] Update `apps/mobile/src/app/(app)/agent/[agentId].tsx` to load the agent DM
  room, send canonical messages over HTTP, subscribe to room events, and abort via
  HTTP. This is transport parity, not the full desktop room-directory UI.
- [ ] On foreground, subscribe first and reconcile history by sequence. On
  background, close AppSync subscriptions; do not keep a socket solely for
  presence.
- [ ] Use the same `lightbulb-appsync` Clerk JWT template with the shared native
  protocol client on Expo; the normal authenticated HTTP client continues using
  the ordinary Clerk session token. No native module or dev-client rebuild is
  required for the event transport.
- [ ] Update existing mobile chat suites for agent switch, attachments composer
  shell, history retry, server truth, stop run, and stream resync.

Commit as `mobile: replace custom chat socket with AppSync events`.

## Task 7: Delete the client socket implementation and finish CI

After Tasks 1–6 pass against backend fakes:

- [ ] Delete `packages/shared/src/gateway/useGatewayConnection.ts`, browser/mobile
  gateway providers, `teamsSubscription.ts`, `NEXT_PUBLIC_WS_URL`,
  `EXPO_PUBLIC_WS_URL`, `deriveWebSocketUrl` uses, pings, park/reconnect helpers,
  raw sends, and tests that only cover that protocol.
- [ ] Move gateway stream payload types that the new client still needs under
  `shared/src/live`; do not maintain duplicate type definitions.
- [ ] Preserve desktop Rust node files, VNC code, and Paperclip proxy code.
- [ ] Push and rely on Frontend CI and Mobile CI. The PR description must name PR
  #2404's reused files, deleted browser socket paths, HTTP endpoints, AppSync
  channel count, and Tauri decoupling.

The frontend PR merges after the AWS, promoted plugin image, and backend contracts
exist. The rollout packet owns the single production dispatch and founder
acceptance.
