# Multi-Instance Backend Architecture (Scaling + Availability)

**Date:** 2026-05-30 (rev. 2026-05-31)
**Status:** Design — rev 2 (incorporates expert review + concurrent-multi-user feasibility analysis)
**Author:** prez2307 (with Claude)
**Grounding:** file/line references verified against `origin/main` (`71744d65`, after Piece 3 merged via #789); OpenClaw pinned `2026.5.12`.

---

## Summary (read this first)

Today the Lightbulb AI backend runs as **one process** (`desiredCount: 1`). Everything about a live
session — which browser tabs/devices belong to an owner, and the persistent WebSocket to each
owner's OpenClaw container — lives in that one process's **memory**. That's correct at one instance
but blocks running a second: a second copy wouldn't know about connections the first holds, and the
two would each open a duplicate connection to the same container.

**We are staying at one instance for now.** This spec exists so that *when* we need to scale, it's a
small, safe step rather than a rewrite — and so we don't accumulate single-instance assumptions that
make a future move painful.

**Design principle — N-agnostic, no mode switch.** The target is **one code path that is correct at
`desiredCount=1` and at `>1` alike.** There is **no flag you must enable to scale** and **no
guardrail that refuses N>1.** At one instance the single task trivially owns every lease (zero
contention) and the DynamoDB connection lookup returns exactly the connections that task registered —
**behavior-identical to today.** At N>1 the same code coordinates correctly. Flipping `desiredCount`
is safe at any time; it "just works."

Two building blocks make this true:

1. **Owner-keyed connection routing** — connection lookup comes from the DynamoDB `ws-connections`
   table (keyed by `owner_id`), so any backend can deliver to all of an owner's tabs/devices.
2. **Single-owner container connection** — a short DynamoDB *lease* (per `owner_id`) ensures exactly
   one backend holds a given owner's container WebSocket at a time; other backends forward outbound
   RPCs to the owner.

(Availability — clean restarts/deploys — was a third piece, **Piece 3, now shipped (#789)**.)

This is independent of the dead-air UX fix (**#780**), which ships separately and rides the same
fan-out path for free.

---

## 1. Problem & root cause

**Root cause:** per-task in-process state.
- Fan-out is in-process: `GatewayConnectionPool._forward_to_frontends` iterates an in-memory
  `_frontend_connections` set (keyed by `owner_id`), demuxed per member via `_conn_member_map`. The
  Teams broker fans out via its own in-memory `_subscribers` set. Neither consults DynamoDB at
  delivery time.
- Each task opens its **own** persistent WS to an owner's container (the pool is a per-task singleton
  keyed by `owner_id`).
- The ingress can't pin an owner to a task: client → API Gateway WS → `ws-message` Lambda → **HTTP
  POST to ALB → per-request load-balanced** to any task (`api-stack.ts`); ALB cookie stickiness does
  not apply to the Lambda→ALB hop, and the backend service is not individually task-addressable.

**The registry primitive is half-built (from #518).** `ConnectionService.query_by_user_id`
(`connection_service.py:190`) returns connection IDs via a `by-user-id` GSI (`api-stack.ts:264`), and
the API Gateway Management API push is already **global** (any task → any connectionId). But it was
never wired into the fan-out path, and — see §3.0 — it's keyed by the *member's* `userId`, not by
`owner_id`, so it is **not org-correct yet.**

---

## 2. Goal & Non-Goals

### Goal
A backend that is **correct and behavior-identical at `desiredCount=1`** today, and can be scaled to
**N ≥ 2 instances** (now or later) **without code changes or a mode flip** — any instance can serve
any owner, and the loss of any single instance does not cause an outage.

### Non-Goals (explicit ceiling)
- **Scaling now.** We stay at `desiredCount=1`. This spec builds the *capability*, not the scale-up.
- **Zero-downtime / zero-lost-events.** A chat mid-turn at the instant of a restart can still lose
  in-flight stream events; the user's next action recovers. Exactly-once delivery would need a
  persistent event log — out of scope.
- **Cross-region / multi-AZ active-active beyond standard ECS.** Out of scope.
- **The dead-air `run_active` UX fix (#780).** Separate; ships independently; inherits this work.
- **First-frame delivery during GSI propagation lag (accepted residual, negligible).**
  Fan-out resolves recipients from the eventually-consistent `by-owner-id` GSI (GSIs can't be read
  strongly-consistently). In practice this lag never bites: a connection's row is written by the
  `$connect` Lambda **before** API Gateway completes the connection, and every fan-out is gated behind
  a `chat.send` round-trip + LLM turn — so propagation (typically single-digit ms on an on-demand
  table) has long since completed before any frame is delivered. The cache is also busted on every
  `$connect`/`$disconnect` for the owner, so a tab that sends a message is picked up immediately. The
  only theoretical residual is a brand-new tab whose **very first action is a single one-shot frame**
  within the sub-second window — accepted, ~zero probability, self-heals on the next event. We
  deliberately do **not** build machinery to close it (no in-process freshness gate, no merge of the
  local set into delivery): DynamoDB stays the single source of truth, and the gate would add more
  complexity than the residual is worth (decision 2026-06-01, after the freshness-gate experiment was
  removed as over-engineering — see §3.1).

---

## 3. Target architecture

> **Why DynamoDB (and not Redis/etcd/ZooKeeper)?** DynamoDB is what AWS recommends for *both* halves
> here, and it's zero new infrastructure (we already run it for all metadata):
> - **Connection registry / fan-out:** the canonical AWS serverless-WebSocket pattern is exactly
>   "store `connectionId`s in DynamoDB, look them up, push via the API Gateway Management API."
> - **The lease:** AWS publishes a DynamoDB lock client and documents the conditional-write lease
>   pattern as the standard distributed-lock primitive on AWS. DynamoDB conditional writes on the
>   **primary key are strongly consistent** — exactly what a lease needs.
> - **Alternatives weighed:** ElastiCache/Redis (Redlock) buys sub-ms latency we don't need for a
>   ~10s-renew lease, and adds a cluster to run/patch/fail-over plus Redlock's correctness debates;
>   ZooKeeper/etcd are the gold standard for leader election but heavy to operate with no managed AWS
>   offering for this use — overkill. DynamoDB is the idiomatic, lowest-operational-cost choice.
> - **Consistency split (important):** the **lease uses the table primary key** (strongly consistent
>   conditional writes). **Fan-out uses a GSI** (eventually consistent — fine, with the
>   lazy-registration fallback in §3.1). Don't put the lease on a GSI.

### 3.0 Make the connection registry owner-correct (prerequisite)

**Verified gap:** the `ws-connections` table's GSI `by-user-id` is keyed on **`userId`**
(`api-stack.ts:264-266`, KEYS_ONLY); `store_connection` writes `connectionId`, `userId`, `orgId`,
`connectedAt`, `connectionType` — **no `ownerId` attribute** (`connection_service.py:85-89`);
`query_by_user_id` queries by `userId` (`:208-210`). But the in-process fan-out is keyed by
`owner_id = org_id or user_id` and demuxed per member (`websocket_chat.py:166,188`). Consequence:

- **Personal user:** `owner_id == userId` → `query_by_user_id(owner)` works.
- **Org:** `owner_id == orgId`, but no row has `userId == orgId` → `query_by_user_id(orgId)` returns
  **nothing**. **DDB org fan-out is broken today** (masked only because the live path still uses the
  in-process map).

**Fix (the org-correct registry):**
- Add an explicit **`ownerId`** attribute to each connection row (= `orgId or userId`), written in
  `store_connection` (and by the `ws-connect` Lambda).
- Add a **`by-owner-id` GSI** partitioned on `ownerId`, **projecting `userId`** (the member) **and
  `connectionType`** (`chat` | `node` | operator-relay) so the owning task can fan out to the right
  connections and demux per member.
- `query_by_owner(owner_id)` returns `[(connectionId, member_userId, connectionType)]`.
- **Filter by `connectionType` at fan-out (Codex P1).** Chat events (`chunk`/`done`/status) go ONLY
  to `connectionType == "chat"` rows. `node` (desktop node-host) and operator-relay sockets are
  transparent adapter pipes — broadcasting chat frames to them is bleed-through that today's
  `websocket_chat.py` deliberately avoids. The owner-keyed fan-out MUST preserve that exclusion
  (filter to chat rows, equivalently a frontend-chat-only view of the registry).

This is correct hygiene even at N=1 and is the foundation both blocks below rely on. It also covers
the concurrent-multi-user constraint (see §6).

**Migration posture — no backfill, reconnect IS the migration (decision 2026-06-01).** Browser
WebSockets terminate at API Gateway and survive backend rollovers, so when this ships there can be
live `ws-connections` rows written by the *old* `store_connection` — they have `userId`/`orgId` but
**no `ownerId`**, so they won't appear in the `by-owner-id` GSI. An earlier revision of this spec
called for a one-time `ownerId` backfill sweep plus a lazy `ownerId` self-heal on the first
`/ws/message`. **We do neither.** The deployed footprint is one prod container and two dev
containers; a passively-waiting pre-deploy tab simply does not appear in the GSI until its socket
reconnects (a deploy cycles the backend, and clients reconnect shortly after regardless), at which
point both the `ws-connect` Lambda and `store_connection` write `ownerId` on the fresh row. That
bounded, self-correcting gap is acceptable at this scale and keeps the system from carrying
migration/self-heal machinery it would never need again. Rollout is therefore a plain deploy: every
new connect is owner-correct from its first frame, and no code path reads a legacy fallback.

### 3.1 Owner-keyed DynamoDB fan-out

Replace the in-memory recipient set with `query_by_owner(owner_id)` against `ws-connections`, then
push via the existing global Management API; demux per member with the projected `userId`.

- **Source of truth:** the `ws-connections` table (written on `$connect`/`$disconnect`). The
  delivery path reads **only** DynamoDB — the in-process connection set is never consulted to decide
  recipients.
- **Hot-path cache (plain TTL, no freshness gate).** The recipient set for an owner is stable across
  a streaming run, so cache `query_by_owner` per `owner_id` with a short TTL (~5s) and **bust the
  cache on every `$connect`/`$disconnect` for that owner** (`invalidate_recipients_cache` from
  `add`/`remove_frontend_connection`). We do **not** query DynamoDB per chunk. We deliberately do
  **not** cross-check the cached set against the in-process set to detect "partial" GSI results — an
  earlier revision did (a stale-vs-pending freshness gate keyed on per-connection registration
  timestamps), and it was **removed as over-engineering** (decision 2026-06-01): it guarded a GSI lag
  that does not bite us, and its own failure mode (a never-unregistered id permanently disabling the
  cache) was worse than what it prevented. Caching an empty/partial result for ≤TTL is acceptable
  because the cache is busted on connect and the lag window has closed before any frame ships (below).
- **Why GSI lag is moot here.** The `by-owner-id` GSI is eventually consistent, but a connection's
  row is written by the `$connect` Lambda *before* API Gateway completes the connection, and every
  fan-out is gated behind a `chat.send` round-trip + LLM turn. So by the time any frame is delivered,
  propagation (single-digit ms on an on-demand table) finished long ago. The lazy
  `add_frontend_connection` on `/ws/message` only matters for cache invalidation now (not as a
  delivery source), so it does not need the `connectionType` gate — delivery is filtered to
  `connectionType == "chat"` at the DDB read regardless of what is in the in-process set.
- **Failure handling (DynamoDB is the only source):** if `query_by_owner` errors/times out, log and
  drop the event for that push — there is **no** in-process fallback (per the Non-Goals, DynamoDB is
  the single source of truth for fan-out). A transient query failure costs at most the current frame;
  the next event re-queries. (Known residual: a *terminal* `done`/`error` frame has no successor to
  re-query on, so a blip there can leave the UI streaming — addressed by a client-side watchdog, not
  by weakening this invariant.)
- **Stale-entry self-heal (already present):** a Management API push to a dead connection returns
  410/GoneException; discard it (`connection_pool.py:634`) and best-effort delete the row.
- **Teams broker is NOT migrated.** It keeps its subscription-based `_subscribers` filtering — node
  desktop sockets never subscribe to Teams events, so blanket owner fan-out would mis-deliver. Only
  the gateway pool's `_forward_to_frontends` moves to the DDB path.

**Result:** any backend delivers to all of an owner's tabs/devices, demuxed to the right member,
regardless of which backend each connection landed on.

### 3.2 Single-owner container connection (DynamoDB lease)

Exactly one backend holds an owner's container WebSocket at a time.

- **New `container-connection-leases` table:** PK **`owner_id`**; attributes `holder_task_id`,
  `owner_addr` (this task's private `ip:port` for internal forwarding), **`expires_at_epoch_s`**
  (epoch seconds — see TTL note), `version` (monotonic, for CAS renew/steal).
- **Acquire:** strongly-consistent conditional write on the PK —
  *"claim if `attribute_not_exists(owner_id)` OR `expires_at_epoch_s < :now`."* Atomic; one winner.
  The winner is the **owner**: the sole task that opens the container WS, runs the reader loop, and
  calls `sessions.subscribe`.
- **Renew (heartbeat):** every ~10s, conditional update *"set `expires_at_epoch_s = :now+30`, bump
  `version`, IF `version = :held` **AND `expires_at_epoch_s > :now`**."* If it fails, ownership was
  lost — do **not** blind-renew. An expired-but-unstolen lease (version unchanged because no
  successor acquired yet) must **fall through to a fresh acquire**, never a renew, so a paused owner
  can't resurrect an elapsed lease.
- **Fencing (REQUIRED — a lease is not a lock):** the owner tracks its held `version`; **before every
  container interaction** (reader-loop iteration, `send_rpc`, `mark_run_active`) it must verify it
  **still holds a non-expired lease — both `version = :held` AND `expires_at_epoch_s > now` AND
  `holder_task_id == me`.** On any mismatch (stolen *or* elapsed), **immediately close the WS, cancel
  the reader, stop fan-out.** Version-match alone is insufficient: after a GC pause/stall **≥ TTL**
  the lease can have *expired without yet being stolen* (version still matches), and a second task
  could acquire concurrently — so the not-expired check is what actually prevents two simultaneous
  owners. (Codex P2.) **The fence read MUST be strongly consistent — `ConsistentRead=True` (or a
  conditional write/transaction), never a default eventually-consistent `GetItem` (Codex P1):**
  otherwise a GC-paused owner could read a *stale pre-steal* lease image, pass version/expiry/holder,
  and keep using the old WS while the successor already owns the lease.
- **`expires_at_epoch_s` is NOT DynamoDB native TTL.** Lease expiry is enforced by the
  **ConditionExpression** above (strongly consistent). Native DDB TTL is best-effort (can lag hours)
  and is used **only as a GC backstop** to delete abandoned rows — never as the correctness gate.
- **Release on shutdown — order matters (Codex P2):** on graceful shutdown, drain in-flight RPCs,
  then **close the container WS and cancel the reader FIRST, and only then delete the lease row**
  (conditional on `version`). Releasing the lease *before* the old subscription is gone would let a
  successor acquire and open a *second* container connection while the draining reader is still
  attached — reintroducing the duplicate subscription the lease exists to prevent. With
  close-then-release, the row stays non-acquirable until the old WS is actually closed, and the
  successor still acquires within the bounded drain window (seconds), not a full TTL. (Piece 3's
  bounded shutdown provides the drain window.)
- **Outbound RPC from a non-owner** (a message that landed on a non-owner via per-request LB): read
  the lease, forward to `owner_addr` over a backend-internal **`POST /internal/rpc-forward`** (which
  **must require service-to-service auth + owner-scope check — see §6; do NOT rely on the security
  group, the route is reachable from the public API path**); the owner executes it on its container
  WS and returns the result. **`mark_run_active` is owner-only** —
  a non-owner must include its parameters in the forward payload so the *owner* records the run (else
  parked-vs-dead detection breaks at N>1). The original `req_id` threads through all hops for
  end-to-end idempotency. Contract in §6.
- **Inbound events** stream into the owner's reader loop and fan out via §3.1 to all of the owner's
  connections on any task.
- **Task-to-task connectivity (Codex P1 — resolves Open Decision A).** `owner_addr` is a backend
  task's private `ip:port`, but the backend service SG today only allows inbound `:8000` from the ALB
  (`service-stack.ts:163-168`) — peer tasks can't reach it, so at N>1 every forward would time out.
  Add an explicit **backend-service-SG → itself ingress rule** on the internal forward port. (This is
  *connectivity*; it does not replace the app-layer auth in §6 — the SG opens the path between
  backend tasks, the service token authenticates the caller. Sticky/Cloud-Map routing remains
  rejected per §4.)
- **Failover:** owner dies → lease expires (≤ TTL) → the next task needing `owner_id` acquires it and
  lazily re-opens the WS + re-subscribes. Parked chats reconstruct run-active state by re-reading
  `sessions.list` (the same `hasActiveRun` signal #780 uses — they compose).
- **`_active_runs` / parked-watchdog** live on the owner only — behavior unchanged, just scoped to
  the owning task.

### 3.3 One ownership chokepoint (keeps the code N-agnostic + future-proof)

Route **all** container-WS opens through a single function (e.g. `get_or_create_owned_connection(owner_id)`).
At N=1 it always returns "me" (the lone task wins the lease trivially). At N>1 the same function is
the only place ownership is decided. This keeps a single code path for any N and prevents
single-instance assumptions from scattering across the codebase.

---

## 4. Why not affinity (sticky owner→task routing)? — decision recorded

Affinity (consistent-hash `owner_id → task`, route the client straight to the owning task) is the
textbook-clean *steady-state* model and was seriously evaluated (code + AWS-docs feasibility). It is
**technically feasible** (the VPC-attached `ws-message` Lambda can `DiscoverInstances` + hash + POST
to a task IP) and satisfies all constraints **in steady state** — but it is **rejected as the primary
design** because:

- **Rebalancing breaks it.** On every rolling deploy (Piece 3 deliberately overlaps old+new tasks
  ~60s) and every autoscale, the hash ring shifts. The old task still holds the container WS →
  **duplicate subscriptions / double event delivery**, and **node-host/operator-relay/desktop
  connections (pinned per-`connectionId` to the task that received their `$connect`) get stranded**
  on the decommissioned task → relay drops, reconnect storms. That's a regression on "no regressions
  + desktop must keep working."
- **It needs a lease anyway.** Making affinity safe across rebalances requires an ownership-handoff
  protocol — i.e., a lease. So affinity ends up as **affinity + lease** (strictly more complex than
  the lease alone) for a **~1–5ms latency win** that is noise next to OpenClaw's 10–100ms per-RPC
  cost.

Conclusion: the **lease is the fundamental primitive**; affinity is at most a future latency
optimization layered on top, only if data ever shows the forward hop matters (it won't). Not built.

---

## 5. Relationship to issue #780 (dead-air UX fix)

#780 adds a `run_active` signal that rides the **same fan-out path** as `chunk`/`done`
(`_forward_to_frontends`) plus an additive `sessions.changed` handler. Because this design migrates
that exact path to owner-keyed routing + owner-held subscription, #780 inherits multi-instance
correctness for free — and its `sessions.list`/`hasActiveRun` re-read is exactly what §3.2 uses to
reconstruct run-active state after a failover. #780 can ship before or after this; no rework either
way.

---

## 6. Constraints this design must satisfy (and how it does)

- **C1 — two org members concurrent (e.g. both founders, web and/or desktop).** Same org → same
  `owner_id` → same lease holder → one task holds the org's container WS and registers both members'
  connections. The `by-owner-id` GSI returns all the org's connections, each tagged with its member
  `userId`; fan-out demuxes per member. Each member's agent messages reach only that member. ✅
- **C2 — one user on web + desktop simultaneously.** `owner_id == userId`; both the browser operator
  connection and the desktop node-host connection resolve to the same owner → same lease holder;
  separate per-connection registries don't collide. ✅
- **C3 — no regression to current flows.** Web chat, desktop/node-host, exec approvals, Teams events,
  operator relay all continue. The Teams broker is explicitly unchanged (§3.1). At N=1 they co-locate
  trivially on the single task; at N>1 the node-host and operator-relay connection types are made
  correct by the **same lease/ownership model as chat, generalized to streaming frames — see §6.1**
  (owner holds the relay upstream; non-owners forward inbound frames; outbound returns via the global
  Management API). No guardrail, no deferral. ✅
- **C4 — identical at N=1, scalable to N>1 with no flag.** At one instance the lone task wins every
  lease and `query_by_owner` returns its own registered connections — bit-for-bit today's behavior.
  The same code is correct at N>1. ✅

**`/internal/rpc-forward` contract:** request `{ method, params, req_id, owner_id, mark_run_active?: {session_key, agent_id}, sessionKey? }`;
response `{ type: "res", req_id, result }` or `{ type: "error", req_id, code, message }`; HTTP 200 /
400 (malformed) / 401 (bad/absent service auth) / 403 (caller not scoped to `owner_id`) / 503 (owner
draining) / 408 (timeout) / 500 (owner exception); HTTP timeout 35s (> the 30s RPC timeout); the
non-owner awaits the response before replying to the frontend (not fire-and-forget).

**Auth on this route is mandatory — the security group is NOT sufficient (Codex P1).** `api-stack.ts`
defines a `$default` HTTP_PROXY route forwarding *any* public HTTP API path to the ALB → the single
backend FastAPI app. So a route added to that app is reachable **from the public internet** via API
Gateway; the SG only blocks *direct* task IP access, not the public path. Therefore `/internal/rpc-forward`
**must** authenticate at the application layer with a **dedicated task-to-task credential** — a
distinct `kind`/audience or a separate shared secret that is **only ever held by backend tasks and
NEVER minted for or distributed to agents.** **Do NOT reuse `core/services/service_token.py` as-is
(Codex P1):** it mints long-TTL `paperclip_service` tokens that the WS authorizer already accepts for
Paperclip agents, so verifying those here would let any agent token-holder hit this public-reachable
path and drive an owner's container. Reject unauthenticated/wrong-audience calls (401), and verify
the caller is scoped to the claimed `owner_id` (403 otherwise) so it can't drive *another* owner's
container. Also exclude `/internal/*` from the public API path at the gateway as defense-in-depth.
Without this, an internet client (or a leaked agent token) could invoke arbitrary container RPCs for
any owner with no user token.

**Paperclip:** orthogonal at the account level. Paperclip is a separate per-tenant REST BFF with its
own per-user accounts/sessions; that account model does not affect the lease. Its WebSocket
touchpoint — the operator-relay connection type — is, however, subject to the §6.1 open item below.

### 6.1 Relay connection routing at N>1 (node-host + operator-relay)

The desktop **node-host** connection and the Paperclip **operator-relay** are transparent
bidirectional pipes the backend relays between an external client and the container (opened today
from `$connect` via `_kick_off_node_connect` / `_kick_off_operator_connect` in `websocket_chat.py`,
with `$default` frames routed by per-connection in-process upstream maps). They use the **same
lease/ownership model as the chat path**, generalized from a unary RPC to streaming frames — so they
are N>1-correct under this design, **no guardrail and no deferral** (which is required: per product
constraint the system must just work at one instance *or* more):

- **Owner holds the relay upstream, opened on `$connect` (not lazily — Codex P1).** The WS upstream
  to the container for a relay is opened **only by the lease owner**, through the same §3.3 ownership
  chokepoint as the chat container WS. **The relay `$connect` itself must be forwarded to the owner
  to open/register the upstream immediately** — these handshakes are *server-initiated*: the
  container pushes a `connect.challenge` *down* before the client sends its signed first frame (see
  `_kick_off_node_connect` / `_kick_off_operator_connect`). Opening the upstream lazily on the first
  forwarded `$default` frame would **deadlock** — the client waits for a challenge that never comes,
  so no first frame is ever sent. So a relay `$connect` landing on a non-owner forwards an explicit
  open request to the owner (over `/internal/relay-forward`); the owner opens the upstream and the
  challenge flows back to the client via the global Management API. A non-owner never opens its own
  upstream — that's what prevents a second container connection.
- **Inbound (client→container) frames forward to the owner.** A relay `$default` frame landing on a
  non-owner is forwarded to `owner_addr` over an authenticated internal **streaming** endpoint
  (`/internal/relay-forward` — same dedicated **task-to-task** credential + owner-scope check as §6,
  *not* an agent token); the owner writes it to its upstream. This is the streaming analogue of
  `/internal/rpc-forward`.
- **Outbound (container→client) frames need no forward.** The owner reads from the upstream and pushes
  to the client `connectionId` via the **global Management API** (any task → any connectionId), so the
  return path needs no per-task affinity.
- **Net:** under the lease, exactly one task holds each relay upstream, inbound frames always reach
  it, and outbound always reaches the client — no second upstream, no stranded frames, no guardrail.
  This is why the summary's "safe at any N" holds for **all** connection types, not just chat.

> We explicitly **decline an N>1 guardrail** (Codex suggested one): it is unnecessary because relay
> routing is *specified here*, not deferred — and a guardrail would violate the product requirement
> that the system just work at one instance or more.

---

## 7. Risks & mitigations

- **Split-brain (two owners):** prevented by strongly-consistent conditional acquire + fencing on
  **`version` AND `expires_at_epoch_s > now` AND `holder_task_id`** checked before every container op,
  plus renew refusing to extend an already-expired row (§3.2). A GC-paused owner that resumes after a
  steal *or after its lease merely elapsed* fails its next fenced check (version moved or
  now-expired) and tears down before touching the container — so even a pause ≥ TTL cannot
  double-deliver. Idempotent event handling remains a belt-and-suspenders backstop.
- **DynamoDB native-TTL misuse:** explicitly avoided — expiry is conditional-expression-enforced;
  native TTL is GC-only (§3.2).
- **Fan-out coherence (cross-task `$connect`):** short cache TTL + lazy `/ws/message` registration +
  GSI eventual consistency handling (§3.1); a recipient-query failure logs and drops the current
  frame (no in-process fallback — DynamoDB is the only source).
- **Forwarding hop latency:** in-VPC HTTP, low single-digit ms; negligible vs OpenClaw RPC cost.
- **DDB write hotspots (lease renew):** PK is `owner_id` (well-distributed); renew is a no-op unless
  near expiry; on-demand billing.
- **Rolling-deploy × lease handoff:** the draining task **releases its lease on shutdown** (§3.2) so
  the new task acquires cleanly in <1s — no flap. (This is why §3.2's release pairs with Piece 3's
  bounded shutdown.)

---

## 8. Testing strategy

- **Unit (pytest):** `query_by_owner` targets the right members (personal AND org); lease
  acquire/renew/expire/steal under contention; **fencing** (lose lease mid-run → WS closes); release
  on shutdown; `/internal/rpc-forward` round-trip + `req_id` idempotency; `mark_run_active` owner
  gating; fan-out failure fallback.
- **Integration (N>1 simulation):** two backend instances against one mock container — verify a
  single container subscription (one lease holder), cross-task delivery (client on task B receives
  events from owner on task A), per-member demux for a 2-member org, and failover (kill owner → other
  task re-acquires + re-opens, no duplicate events).
- **N=1 identity:** full suite at `desiredCount=1` is bit-for-bit unchanged.
- **Infra:** CDK assertions for the `by-owner-id` GSI, the leases table (+ native-TTL-as-GC).
- **Manual (dev):** two members of one org chatting simultaneously (web + desktop); single user web +
  desktop; kill a task at N=2 and confirm sessions continue. (Frontend CI is Lint & Build only — run
  vitest locally.)

---

## 9. File-change summary (orienting, not exhaustive)

**Backend:**
- `core/services/connection_service.py` — add `ownerId` to `store_connection`; add `query_by_owner`
  (uses `by-owner-id` GSI, projects member `userId`); keep `query_by_user_id` for compatibility.
- `core/gateway/connection_pool.py` — `_forward_to_frontends` via `query_by_owner` + cache + failure
  fallback; the lease (acquire/renew/**fence**/release); owner-only reader/subscribe; non-owner RPC
  forward; one ownership chokepoint (§3.3).
- `routers/websocket_chat.py` — write `ownerId` on connect; `POST /internal/rpc-forward` (chat) +
  `/internal/relay-forward` (streaming node-host/operator frames, §6.1); `mark_run_active`
  owner-gating + forward payload; keep lazy `add_frontend_connection`; route relay-upstream opens
  through the §3.3 ownership chokepoint (owner-held).
- `core/services/lease_repo.py` *(new)* — acquire/renew/release via strongly-consistent conditional
  writes + `version` CAS.
- (Teams broker unchanged.)

**Infra (CDK):**
- `api-stack.ts` (or `database-stack.ts`) — `by-owner-id` GSI on `ws-connections` (project `userId` +
  `connectionType`); `container-connection-leases` table (PK `owner_id`, native TTL on
  `expires_at_epoch_s` as GC only).
- The `ws-connect` Lambda — write `ownerId` on the connection row.
- `service-stack.ts` — backend-service-SG **self-ingress** rule on the internal forward port (so
  peer backend tasks can reach `/internal/rpc-forward` at N>1; §3.2).
- **No** `desiredCount` change (stays 1). **No** N-gating flag, **no** guardrail.

**Frontend:** none required.

---

## 10. Status & sequencing

- **Piece 3 (clean handoff / availability): DONE** — merged via #789 (`71744d65`).
- **This rev (rev 2):** target design for owner-keyed routing + single-owner lease, N-agnostic, no
  guardrail. We stay at `desiredCount=1`; this builds the capability so a future scale-up is a config
  change, not a rewrite.
- **#780 (run_active dead-air UX fix):** separate, ships independently.
- Implementation will follow as its own plan (registry-correctness + fan-out, then the lease), built
  and verified at N=1.

### Deferred to the implementation plan (captured from Codex review, not re-spun here)

The design + correctness invariants above are settled. These finer, build-time completeness items
are recorded so the implementation plan addresses them (where they get code-level review), rather
than iterating the design doc further:
- **Relay `$disconnect` routing (symmetric to the `$connect` forward, §6.1):** when a node-host/
  operator relay client disconnects, the owner must tear down/clean the corresponding relay upstream;
  a `$disconnect` landing on a non-owner forwards the close to the owner just like `$connect`.
- **`connectionType` write + projection end-to-end:** ensure `connectionType` is persisted on the
  `ws-connections` row (it is today) and projected on the `by-owner-id` GSI, so the chat fan-out
  filter (§3.1) and the existing node/operator exclusion are preserved exactly.
- **Strongly-consistent reads everywhere ownership is decided** (the fence read uses
  `ConsistentRead=True`, §3.2) — carry the same discipline to any acquire/lease-read path the
  implementation adds.
- **One NON-chat delivery path still reads the in-process set — must move to owner-keyed DDB
  fan-out before N>1.** Piece 1 migrated the *chat* path (`_forward_to_frontends` → `query_by_owner`)
  and `GatewayConnectionPool.broadcast_to_member` (the org branch now routes through
  `_forward_to_frontends(message, target_member_id=…)`, so node-status member pings are owner-keyed
  too). The remaining holdout is `GatewayConnection._emit_status_change` (GATEWAY_DOWN/health status
  pushes), which still iterates the per-task `_frontend_connections`. Correct at N=1 (single task
  holds all of an owner's connections); at N>1 a status push raised on task B would not reach a tab on
  task A — the exact cross-task gap this design eliminates for chat. It is deferred rather than
  migrated with the others because it is a **sync** method called from 5 sites (connect / reader-loop
  exit / reopen), so routing it through the async `_forward_to_frontends` is a sync→async refactor of
  its callers, and it carries lower-stakes UI status pings tied to Piece 2's single-owner connection
  (where the lease decides which instance holds the GatewayConnection). Migrate it to `query_by_owner`
  (chat-filtered) before flipping `desiredCount>1`; the in-process set then reduces to the
  grace/idle-teardown signal (`_grace_close`) or a DDB count.

(Standing decision, not an open item: **no N>1 guardrail** — declined per the product requirement
that the system work at one instance *or* more; relay routing is specified in §6.1 so "safe at any N"
holds. Codex re-flags this each round; it is intentional.)
