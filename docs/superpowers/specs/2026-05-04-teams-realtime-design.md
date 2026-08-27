# Teams Realtime Updates — Design

**Status:** Spec
**Owner:** prasiddha
**Roadmap:** [Teams UI parity roadmap](./2026-05-04-teams-ui-parity-roadmap.md) — sub-project #1
**Started:** 2026-05-04

## Goal

Make `/teams/*` panels feel live. When the upstream Paperclip server emits an event (run status changed, new activity logged, agent status flipped, etc.), the affected panel's data should refresh automatically without the user clicking refresh, navigating, or waiting for a poll tick.

## Non-goals

- Bidirectional commands over WS. Mutations stay on REST. Events are server-push only.
- Backfilling missed events after a disconnect. Paperclip's WS is pure live-tail; on reconnect we re-fetch via REST instead of replaying.
- Agent-key (Bearer) auth on the events channel. Only browser-board (cookie) auth — agents don't access /teams.
- Cross-tenant fanout, global events, plugin events. We pass through `plugin.*` types but don't yet wire any UI to them.
- Optimistic updates / inline cache mutation. Each event triggers a refetch, not an in-place merge.

## Background

Native `/teams` shipped in PR #509 with REST-only data fetching (SWR). Panels feel stale because nothing refreshes between explicit user actions. The audit at sub-project #0 (this roadmap's introduction) flagged "no realtime updates" as the most cross-cutting gap.

Upstream Paperclip already exposes a per-company WebSocket at `/api/companies/{companyId}/events/ws` carrying 9 event types (`heartbeat.run.{queued,status,event,log}`, `agent.status`, `activity.logged`, `plugin.{ui.updated,worker.crashed,worker.restarted}`). Their own UI uses it; we used to ride it transparently when `/teams` was a reverse proxy. After the rebuild we lost that channel.

## Architecture

```
Browser                  API Gateway WS              FastAPI backend            Paperclip server
  │                       (existing infra)         (long-lived ECS task)       (Hono + ws library)
  │                            │                          │                          │
  │ wss://ws-dev.flickmeon.ai  │                          │                          │
  ├──── connect (Clerk JWT) ──>│                          │                          │
  │                            │ Lambda authorizer ✓       │                          │
  │                            │ DDB: write {connectionId, │                          │
  │                            │  userId} (existing)       │                          │
  │ {type:"teams.subscribe"}   │                          │                          │
  ├───────────────────────────>│ POST /ws/message ────────>│ TeamsEventBroker          │
  │                            │                          │  .subscribe(userId,       │
  │                            │                          │             connId)       │
  │                            │                          ├─ open WS to Paperclip ──>│
  │                            │                          │  (Cookie: per-user        │
  │                            │                          │   Better-Auth session)    │
  │                            │                          │                          │
  │                            │                          │  ←── activity.logged ────┤
  │                            │                          │  ←── agent.status ──────┤
  │                            │                          │  ←── heartbeat.run.* ───┤
  │                            │                          │                          │
  │                            │ Management API push       │ broker fans out:          │
  │ {type:"event",             │<─────────────────────────┤  for each conn in         │
  │  event:"teams.<x>",        │                          │  user_subscribers[uid]:   │
  │  payload:{...}}            │                          │   send_message(conn, ev) │
  │<───────────────────────────│                          │                          │
```

### Why API Gateway WS (not a direct ALB→FastAPI WS)

We already operate one WS path end-to-end: Lambda authorizer (Clerk JWT), VPC Link → NLB → ALB → FastAPI, DDB connection table, Management API for fanout. Reusing it for events keeps one auth path, one connection table, one reconnect story. A second WS endpoint via ALB would need new infra (listener rules, sticky sessions vs Management-API-equivalent push) and double our operational surface.

### Why one backing Paperclip WS per **user**, not per browser tab

If a user opens three `/teams` tabs they each open a browser WS to API Gateway, but we only need one upstream WS to Paperclip — the same events fan out to all three browser conn-IDs. Per-tab backing connections would triple Paperclip load for no UX win and complicate the lifecycle (tear down which one when?). The broker keys backing connections by `user_id` and the per-user lock pattern from `core/gateway/connection_pool.py:1052` ports directly.

### Why pass-through event shapes

Paperclip's payload `{id, companyId, type, createdAt, payload}` is stable, public, and shared between Paperclip's own UI and any plugin author's UI. Translating at our broker would put us on the hook for every upstream payload change forever. We prefix the event name with `teams.` (so `event: "teams.activity.logged"`) to avoid colliding with OpenClaw events on the same `{type:"event", event, payload}` channel, but the payload object itself is verbatim.

### Why central invalidation in `TeamsEventsProvider`

Paperclip's own UI uses this exact pattern: a single `LiveUpdatesProvider` mounted at the root maps event types to React Query invalidations, and individual pages use `useQuery` with no awareness of the realtime layer. We mirror it with SWR's `useSWRConfig().mutate(key)`. The alternative — every panel registers what events it cares about — couples panel code to the event bus and means a new event type touches N+1 files. Central invalidation keeps panels free of realtime concerns.

## Components

### Backend

#### New: `core/services/paperclip_event_client.py`

Owns one WebSocket connection to Paperclip's `/api/companies/{companyId}/events/ws` for one user. Responsibilities:

- Open the WS with `Cookie: <session>` header from `paperclip_user_session.get_user_session_cookie(user_id)`.
- Resolve `companyId` via `paperclip_repo.get(user_id)` (the row already exists by the time a /teams subscribe fires — guaranteed by the lazy-provision flow from PR #514).
- Reconnect with capped exponential backoff (1s → 30s, jitter ±20%). Mirrors the OpenClaw gateway client.
- Emit each parsed event to a `Callable[[dict], Awaitable[None]]` callback supplied at construction.
- On reconnect, emit a synthetic `{type: "stream.resumed"}` so the broker can tell subscribers to refetch (see "Reconnect semantics" below).
- Lifecycle: explicit `start()` and `close()`. Owner is responsible for calling `close()` when the last subscriber leaves.
- Health: a single `is_connected` property the broker can check.

**Public surface** (small):
```python
class PaperclipEventClient:
    def __init__(self, user_id: str, company_id: str, on_event: Callable[[dict], Awaitable[None]]): ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
```

#### New: `core/services/teams_event_broker.py`

Process-wide singleton. Subscriber bookkeeping + per-user backing-connection orchestration. Responsibilities:

- `subscribe(user_id, connection_id)`: register conn-ID under user; if no `PaperclipEventClient` for this user, create + start one; idempotent across repeated subscribes from the same conn-ID.
- `unsubscribe(user_id, connection_id)`: remove conn-ID; if subscriber set is now empty, schedule a 30-second grace cancellation that closes the backing client (mirrors `connection_pool.py`'s grace pattern). If a new subscriber arrives within 30s, cancel the teardown.
- `_handle_event(user_id, event)`: receives every event from a backing client. Looks up subscribers for that `user_id` from a GSI on `ws-connections` (see schema change below), wraps the event as `{type: "event", event: f"teams.{event['type']}", payload: event['payload'], id: event['id'], createdAt: event['createdAt']}`, and pushes to each conn-ID via `management_api_client.send_message`. Pushes that return False (stale conn) trigger a `connection_service.delete(conn_id)` cleanup.
- Per-user `asyncio.Lock` to serialize subscribe/unsubscribe (no double-open, no cleanup-while-subscribing race).
- On stream-resumed events, broker emits `{type: "event", event: "teams.stream.resumed"}` — frontend uses this to invalidate all keys.
- Lifecycle: started in `main.py` lifespan handler alongside the gateway pool; stopped on shutdown (close all backing clients gracefully).

#### Modify: `routers/websocket_chat.py`

Extend the `/ws/message` dispatcher to handle two new message types:

- `teams.subscribe`: call `broker.subscribe(auth.user_id, connection_id)`. No response payload.
- `teams.unsubscribe`: call `broker.unsubscribe(auth.user_id, connection_id)`. No response payload.

Both are idempotent. Errors are logged but not surfaced to the client (best-effort subscribe; if the backing connection later fails, the resumed/degraded events surface that).

#### Modify: `core/services/connection_service.py`

Add `query_by_user_id(user_id) -> list[str]` that uses the new GSI to return all current connection IDs for a user. Used by the broker fanout.

#### Schema change: `apps/infra/lib/stacks/api-stack.ts`

Add a GSI to the `ws-connections` table:

```ts
connectionsTable.addGlobalSecondaryIndex({
  indexName: "by-user-id",
  partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.KEYS_ONLY,
});
```

`KEYS_ONLY` projection is sufficient — the broker only needs `connectionId` from the lookup; full row data lives at the base table if anyone wants it.

The existing connection-write path (`websocket_chat.py` `$connect` handler) already writes `userId` to the row, so no migration is needed for new connections. Old in-flight connections (none in dev/prod after deploy since the table TTL is short) will simply not appear in the GSI until they reconnect — acceptable.

#### Lifespan integration: `main.py`

Add `start_teams_event_broker()` / `stop_teams_event_broker()` symmetrically to the existing gateway pool startup. Order: after gateway pool, before update-service worker.

### Frontend

#### Modify: `src/app/teams/layout.tsx` and `src/components/teams/TeamsLayout.tsx`

Wrap `TeamsLayout`'s body in `<GatewayProvider>` (currently mounted only on `/chat`). The provider lazily opens the WS and reconnects automatically — sharing the existing OpenClaw chat infra.

#### New: `src/components/teams/TeamsEventsProvider.tsx`

Mounted inside `TeamsLayout`, inside `GatewayProvider`. Responsibilities:

- On mount: subscribe to `useGateway().onEvent(...)` for events whose `event` field starts with `teams.`. When connected (`isConnected === true`), send `{type: "teams.subscribe"}`. On disconnect, send is a no-op (provider auto-resubscribes on reconnect — see below).
- For each received event, look up the SWR cache key(s) to invalidate (table below) and call `useSWRConfig().mutate(key)`. SWR refetches automatically.
- On reconnect (`isConnected` flips false → true after a previous true): re-send `teams.subscribe` and invalidate ALL realtime-affected keys (cheap; covers the no-backfill gap).
- On unmount: send `teams.unsubscribe` (best-effort — if WS is closing, ignore).

**Event → invalidation map** (lives inside `TeamsEventsProvider`):

| Event type | SWR keys to mutate |
|---|---|
| `teams.activity.logged` | `/teams/dashboard`, `/teams/activity`, `/teams/inbox`, `/teams/issues` (list) |
| `teams.agent.status` | `/teams/dashboard`, `/teams/agents` |
| `teams.heartbeat.run.queued` | `/teams/dashboard`, `/teams/inbox`, `/teams/agents/{agentId}/runs` (when displayed) |
| `teams.heartbeat.run.status` | same as `queued` |
| `teams.heartbeat.run.event` | the specific run-detail key only — most expensive to invalidate, only fires when run-detail is open |
| `teams.heartbeat.run.log` | same as `run.event` (log appended; same refetch model) |
| `teams.plugin.*` | (no UI bound in v1; ignore) |
| `teams.stream.resumed` | invalidate every distinct key listed in this table (full refresh) |

The map is the only place panels' SWR keys are referenced from realtime code. Adding a new panel that wants live updates means: add a row, no other changes.

## Data flow (event lifecycle)

1. User opens `/teams/dashboard`. `TeamsLayout` mounts inside the existing Clerk-gated route. `GatewayProvider` lazily opens WS to API Gateway. Lambda authorizer validates Clerk JWT → DDB row written `{connectionId, userId}`.
2. `TeamsEventsProvider` mounts inside the layout, sees `useGateway().isConnected === true`, calls `gateway.send({type: "teams.subscribe"})`.
3. API Gateway invokes `/ws/message` on the backend with the message body. Handler routes on `type` and calls `broker.subscribe(auth.user_id, connection_id)`.
4. Broker: per-user lock acquired. No existing client for this user → create `PaperclipEventClient(user_id, company_id, on_event=broker._handle_event)`, call `start()`. Subscriber set: `{conn_id_A}`.
5. `PaperclipEventClient.start()` calls `paperclip_user_session.get_user_session_cookie(user_id)`, opens WS to `wss://paperclip.internal/api/companies/{companyId}/events/ws` with `Cookie:` header. Paperclip authorizes via `companyMemberships` → upgrade succeeds.
6. User clicks "comment" on an issue (REST mutation, unrelated to this design). Paperclip emits `activity.logged` over the WS.
7. Client parses, calls `broker._handle_event(user_id, event)`. Broker queries the new GSI for `user_id` → gets `[conn_id_A]`. For each, `management_api_client.send_message(conn_id_A, {type:"event", event:"teams.activity.logged", payload, id, createdAt})`.
8. Browser WS receives. `useGateway` dispatches via `onEvent`. `TeamsEventsProvider`'s handler sees `event.startsWith("teams.")`, looks up `teams.activity.logged` → invalidates `["/teams/dashboard", "/teams/activity", "/teams/inbox", "/teams/issues"]`. SWR refetches each → panels re-render.
9. User opens a second tab on `/teams`. New browser WS conn, new DDB row, broker.subscribe adds `conn_id_B`. Existing backing client reused. Both tabs receive subsequent events.
10. User closes both tabs. Browser WS disconnects → API Gateway invokes `$disconnect` → existing handler deletes DDB rows → those rows happen to be the broker's subscribers, but the broker doesn't see disconnects directly. The next outbound event push to a stale conn-ID returns False from Management API, broker treats that as "remove subscriber"; when subscriber set is empty, 30s grace period starts; if nobody resubscribes in 30s, backing client closes.

## Reconnect semantics (the no-backfill problem)

Paperclip's WS has no replay cursor. Three reconnect cases need explicit handling:

1. **Browser WS reconnects (API Gateway → backend)**: existing `useGateway` reconnect logic fires. `TeamsEventsProvider` detects the false→true transition, sends `teams.subscribe` again, and invalidates every key in its map (so any state change while disconnected gets refetched). The backing Paperclip WS may have stayed up the whole time — irrelevant to the browser, the refetch covers it.

2. **Backing Paperclip WS reconnects (backend → Paperclip)**: client's reconnect loop fires after backoff. On successful reopen, client emits a synthetic `{type: "stream.resumed"}`. Broker fans this out as `event: "teams.stream.resumed"` to all subscribed conn-IDs. Frontend handler invalidates all keys (same as case 1).

3. **Cold path: backend process restart (ECS task replacement)**: the backing client is lost; all browser WS connections are lost too (API Gateway sends `$disconnect`). Browser reconnects → case 1 fires.

In all three, the user observes "panels refresh automatically about 1-2 seconds after the network blip resolves." No explicit "you missed events" UI is needed for v1.

## Error handling & degradation

| Failure | Behavior |
|---|---|
| Paperclip WS upgrade returns 403 (e.g. session expired) | Client: log + retry once after fresh `get_user_session_cookie` call. If still 403, give up; broker emits `teams.stream.degraded` to subscribers; backend logs an error metric. |
| Paperclip WS connection drops mid-stream | Reconnect with backoff (1s → 30s, ±20% jitter, max 30 attempts ≈ 15 min). After max attempts, broker emits `teams.stream.degraded`, marks client as failed; subscribers must explicitly resubscribe (e.g. user navigates away and back). |
| Management API `send_message` returns False (conn gone) | Broker calls `connection_service.delete(conn_id)`, removes from subscriber set. |
| Management API raises `ManagementApiClientError` | Log + skip that conn; don't tear down siblings. |
| Unknown event `type` from Paperclip | Log at warning, ignore. Forward-compat: when Paperclip adds a new event type, our broker still works; the frontend ignores unmapped events too. |
| Multiple `teams.subscribe` from the same conn-ID | Idempotent — subscriber set is an `dict[user_id, set[conn_id]]`. |
| Subscribe before WS `$connect` writes the DDB row (race) | Already impossible in practice: `/ws/message` is invoked AFTER `$connect` succeeded. |

`teams.stream.degraded` is a frontend-only signal: `TeamsEventsProvider` surfaces a small "Live updates paused — refresh to retry" banner. **v1 deferred** — log + ignore in v1, add the banner in a follow-up if real users hit it.

## Security

- **Tenancy boundary**: Paperclip itself enforces company-membership in `live-events-ws.ts:146`. The session cookie we pass is per-user, so Paperclip authorizes the upgrade against THAT user's memberships. We can't accidentally fan out cross-tenant.
- **Browser auth**: same Clerk JWT path as chat. No new attack surface.
- **Backing connection privilege**: identical to what the user has for REST `/teams/*` (per-user Better-Auth session) — no privilege elevation.
- **PII / payload contents**: events carry IDs and timestamps, occasionally tool result snippets in `heartbeat.run.event`. Same payload the user already sees on the panel after a refresh. Logging the broker should redact `payload.content` if logged at INFO; only IDs + types at INFO, full payloads only at DEBUG.

## Testing strategy

### Backend

- **Unit:** `paperclip_event_client` against a fake WS server (`websockets.serve`). Cases: open with cookie header, parse event, reconnect on close, exponential backoff bounds, `close()` cancels the loop cleanly.
- **Unit:** `teams_event_broker` with mock `PaperclipEventClient` and mock `management_api_client`. Cases: subscribe/unsubscribe lifecycle, multi-conn fanout, grace-period teardown, race-safe lock, stale-conn cleanup on `send_message` False, no double-open of backing client.
- **Integration:** `routers/websocket_chat.py` end-to-end with the new `teams.subscribe` / `teams.unsubscribe` types via TestClient + WebSocket, mocking only the Paperclip-side WS.
- **No moto needed** for the GSI — the broker's GSI query goes through `connection_service`, which is already mocked at the unit level.

### Frontend

- **Unit:** `TeamsEventsProvider` with a mock `useGateway`. Cases:
  - On mount + connected, sends `teams.subscribe`.
  - On `event: teams.activity.logged`, invalidates the documented key set.
  - On `event: teams.stream.resumed`, invalidates the full key set.
  - On `isConnected` false→true transition, re-sends `teams.subscribe` and invalidates.
  - On unmount, sends `teams.unsubscribe`.
- **No E2E** for v1 — Playwright validation of realtime is disproportionate to the value vs unit/integration coverage.

### CDK

- Existing CDK snapshot tests catch the schema change; verify the GSI appears in the synth diff and the IAM policy for the backend service grants `dynamodb:Query` on the new index.

## Acceptance criteria

- Open `/teams/dashboard` in a browser. Trigger a Paperclip event from another source (e.g. CLI/REST mutation that creates an activity entry). The dashboard's relevant counter or list updates within ~1 second without manual refresh.
- Open `/teams/inbox` and `/teams/dashboard` in two tabs of the same user. A single triggered event refreshes both within ~1 second.
- Disconnect the user's network for 10 seconds, reconnect. Both tabs catch up to current state within ~2 seconds via the resume invalidation.
- Restart the backend ECS task while a `/teams` tab is open. After API Gateway re-establishes, the tab refreshes its data automatically (no user-visible action needed).
- Two users, two browsers, two distinct accounts: events from user A's actions never appear on user B's tabs (verified via Paperclip's company-membership gate, but smoke-tested anyway).

## Rollout

1. **Schema migration** ships first via the `deploy.yml` CDK workflow. GSI is additive — no downtime, no data loss. New connections start populating `userId` on the GSI immediately.
2. **Backend code** ships next via `backend.yml`. Without frontend, no one calls `teams.subscribe`, so the broker stays idle; safe to deploy.
3. **Frontend code** ships last via Vercel. Once it lands, `/teams` opens the subscribe channel and events start flowing.
4. **Verify on dev**: open `/teams/dashboard`, comment on an issue from a CLI tool that hits Paperclip directly, watch the dashboard refresh within ~500ms.
5. **No feature flag needed** — the channel is server-push only and gracefully degrades to "no live updates" if anything fails.

## Out of scope (deferred)

- Live agent run progress visualization (uses `heartbeat.run.event` log stream for a streaming transcript view). Scoped into sub-project #2 (Dashboard charts) and #3 (Inbox depth) where the data is actually rendered.
- `teams.stream.degraded` user-facing banner. Add only if reconnect failures hit real users.
- Caching the per-user Paperclip session cookie. Optional optimization for reconnect storms.
- Multi-region (not relevant — Lightbulb AI is single-region).
- Browser-pushed events (subscribe filters, "mark read" via the channel). Mutations stay on REST.
