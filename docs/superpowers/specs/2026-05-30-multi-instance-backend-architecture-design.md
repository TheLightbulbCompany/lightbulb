# Multi-Instance Backend Architecture (Scaling + Availability)

**Date:** 2026-05-30
**Status:** Design — pending user review
**Author:** prez2307 (with Claude)
**Grounding:** all file/line references verified against `origin/main` (`b61fd303`); OpenClaw pinned `2026.5.12`.

---

## Summary (read this first)

Today the Isol8 backend can only run as **one process** (`desiredCount: 1`). Everything about a
live session — which browser tabs belong to a user, the persistent WebSocket to each user's
OpenClaw container — is held in that one process's **memory**. Two consequences:

- **Can't scale** (primary): we cannot run a second backend for load; a second copy wouldn't know
  about connections the first copy holds.
- **No availability** (secondary): when that one process restarts — **every deploy, plus any
  crash** — live chat degrades for **1–5 minutes** (verified, not estimated).

This spec makes the backend **multi-instance-correct** in three pieces, after which we run N copies
with autoscaling and both problems are fixed:

1. **Routing** — move connection lookup out of process memory into the DynamoDB table we already
   have, so any backend can deliver to any user's tabs.
2. **Ownership** — exactly one backend holds a given user's container WebSocket at a time (a short
   DynamoDB lease), so N backends don't open duplicate connections.
3. **Clean handoff** — graceful shutdown + rolling deploy config so a restart/scale event is a
   non-event.

Availability falls out of (1)+(2): if any backend can serve any user and ownership re-homes when a
task dies, losing one task is no longer an outage. (3) makes the transition clean.

**This is independent of the dead-air UX fix (issue #780)**, which ships after and rides the
migrated routing for free.

---

## 1. Problem & root cause

**Root cause:** per-task in-process state.
- Fan-out is in-process: `GatewayConnectionPool._forward_to_frontends` iterates an in-memory
  `_frontend_connections` set (`connection_pool.py:602`, set defined `:1379`). The Teams broker
  fans out via an in-memory `_subscribers` set. Neither consults DynamoDB.
- Each task opens its **own** persistent WS to a user's container (the pool is a per-task
  singleton keyed by `owner_id`).
- The ingress can't pin a user to a task: client → API Gateway WS → `ws-message` Lambda → **HTTP
  POST to ALB → per-request load-balanced** to any task (`api-stack.ts`); ALB cookie stickiness
  does not apply to the Lambda→ALB hop.

**Verified failure mode on a single-task restart** (crash *or every deploy*):
- 0–2s: all in-process state lost; in-flight RPCs reject; in-flight chat goes "parked"; container
  WS, Teams subscribers, operator/node upstreams all dropped.
- The browser WS terminates **at API Gateway, not the backend**, so it stays open and the frontend
  often doesn't even detect the swap → Teams events stay dead until a ~30s grace disconnect.
- Detection + new-task-healthy + reconnect ≈ **32–111s**; a chat mid-turn can sit "thinking" up to
  **300s** before the red "Restart Gateway" button. Realistic blast radius: **1–5 minutes, on every
  deploy.**

**The primitive we need is half-built.** `ConnectionService.query_by_user_id`
(`connection_service.py:190`) returns all of a user's connection IDs via a `by-user-id` GSI
(`api-stack.ts:263`), and the API Gateway Management API push is already global (any task → any
connection ID). These were added in #518 for a fan-out that was never wired up.

---

## 2. Goal & Non-Goals

### Goal
Run **N ≥ 2 backend instances** (with autoscaling) correctly: any instance can serve any user, and
the loss of any single instance does not cause an outage.

### Non-Goals (explicit ceiling)
- **Zero-downtime / zero-lost-events.** A chat that is mid-turn at the instant of a restart can
  still lose in-flight stream events; the user's next action recovers. True exactly-once delivery
  would need a persistent event log — out of scope.
- **Cross-region / multi-AZ active-active beyond standard ECS.** Out of scope.
- **The dead-air `run_active` UX fix (#780).** Separate, ships after, inherits this work.

---

## 3. Target architecture

### Piece 1 — DynamoDB-backed connection routing (fan-out)

Replace the per-task in-memory recipient set with a lookup against the `ws-connections` table.

- **Source of truth:** the `ws-connections` table, already written on `$connect`
  (`ws-connect` Lambda + `connection_service.store_connection`, `websocket_chat.py:151`) and on
  `$disconnect`. We add nothing to the write path.
- **`_forward_to_frontends`** (and the Teams broker fan-out) resolve recipients via
  `query_by_user_id(owner_id)` instead of the in-memory set, then push via the existing Management
  API. Per-member targeting (org) is preserved by filtering on the `userId`/member the row carries.
- **Hot-path cache:** the recipient set for a user is stable across a streaming run, so cache the
  `query_by_user_id` result per `owner_id` with a short TTL (e.g. 5s) + invalidate on
  `$connect`/`$disconnect`, so we do **not** query DynamoDB per chunk. (The in-memory set becomes
  this cache, sourced from DynamoDB instead of from "connections that happened to land on me.")
- **Stale-entry self-heal (already present):** a Management API push to a dead connection returns
  410/GoneException; we discard it (`connection_pool.py:634`) and best-effort delete the row.

**Result:** any backend can deliver to all of a user's tabs, regardless of which backend each tab
connected through.

### Piece 2 — Single-owner container connection (DynamoDB lease)

Ensure exactly one backend holds a user's container WebSocket at a time.

- **New `container-connection-leases` table:** PK `owner_id`; attributes `task_id`,
  `owner_addr` (this task's private IP:port for internal forwarding), `expires_at` (TTL), `version`
  (for conditional renew/steal).
- **Acquire:** when work for `owner_id` first needs the container connection, a task acquires the
  lease via a conditional write (acquire if absent or expired). The lease holder = the **owner**:
  it is the sole task that opens the container WS, runs the reader loop, and calls
  `sessions.subscribe`.
- **Renew:** the owner renews the lease (bump `expires_at`) on a heartbeat well inside the TTL
  (e.g. renew every 10s, TTL 30s). Batched/no-op if not near expiry to avoid write churn.
- **Outbound RPC from a non-owner** (a user message that landed on a non-owner task via per-request
  LB): the non-owner reads the lease, then forwards the RPC to the owner over a backend-internal
  HTTP endpoint **`POST /internal/rpc-forward`** at `owner_addr`. The owner executes it on the
  container WS and returns the result. *(See Open Decision A — task-to-task addressing.)*
- **Inbound events** stream into the owner's reader loop and fan out via Piece 1 (DDB-routed
  Management API) to all the user's tabs on any task.
- **Failover:** owner dies → lease expires (≤ TTL) → the next task that needs `owner_id` acquires
  it and lazily re-opens the container WS + re-subscribes. Parked chats reconstruct run-active state
  by re-reading `sessions.list` (this is the same `hasActiveRun` signal #780 uses — they compose).
- **`_active_runs` / parked-watchdog** (the reader-loop "parked vs dead" logic, `connection_pool.py:173`)
  lives on the owner only — unchanged in behavior, just scoped to the owning task.

### Piece 3 — Clean handoff (graceful shutdown + deploy/health config)

Make a restart/scale event a non-event. This is the availability piece and is **independently
shippable** (correct at N=1 today, and required for clean scale-in/out at N>1).

**Code (graceful shutdown):**
- Wrap the lifespan shutdown in a bounded `asyncio.timeout` (e.g. 20s, < the ECS SIGTERM→SIGKILL
  window) so no cleanup step can hang into a hard kill (`main.py:329-349`).
- A **shutdown-aware middleware**: set a `_shutting_down` flag at shutdown start; return 503 to
  *new* `/ws/message`/RPC requests while letting in-flight ones finish.
- Drain in-flight RPCs (settle/timeout `_pending_rpcs`) **before** closing the container WS
  (`connection_pool.py:1326-1367`), with a bounded wait.
- Drain the per-user container reader loop before closing its WS; release the lease.
- Bound the Teams broker close (`asyncio.timeout` per `client.close()`) (`teams_event_broker.py:266-282`).
- Graceful-cancel the scheduled worker and gauge loops (finish current iteration, then exit).

**Config (CDK — turns deploys from outage into rolling handoff):**
- Set `minHealthyPercent`/`maxHealthyPercent` so ECS **starts the new task before stopping the old**
  during a deploy (and at N>1, scale-in respects min healthy) (`service-stack.ts:921`).
- Set ALB target group `deregistrationDelay` explicitly (≈20s, ≤ SIGTERM grace) so a draining task
  finishes in-flight work before the LB stops routing (`network-stack.ts:86-100`).
- Enable the ECS deployment circuit breaker (auto-rollback on failed deploy).
- **Enhance the health check** (`main.py:686-693`): today it only pings DynamoDB. It should also
  reflect that the process can serve gateway traffic (event loop responsive; pool initialized) so
  ALB doesn't route to a not-ready task.

**Code (recovery safety):**
- **Container-resolution graceful errors:** if `ecs.resolve_running_container()` fails, return a
  structured "container starting" error instead of throwing/hanging (`connection_pool.py:1507-1559`).
- **RPC idempotency:** client-supplied `idempotencyKey` on mutating non-chat RPCs
  (`agents.create/update/delete`, etc.); backend short-circuits a duplicate (bounded LRU/DDB) so a
  client retry after a hang can't double-execute. (Chat already has upstream idempotency.)

---

## 4. Migration & rollout (safe, incremental, flag-gated)

The key safety property: **every piece is correct at `desiredCount=1`**, so we land and verify each
at N=1 *before* flipping to N>1.

1. **Piece 3 (HA hygiene)** first — config + graceful shutdown. Immediately turns deploys into clean
   rolling handoffs at N=1. Lowest risk, highest-frequency win. Independently shippable.
2. **Piece 1 (DDB routing)** behind a flag (`GATEWAY_FANOUT_FROM_DDB`). At N=1 the DDB query returns
   the same connections the in-memory set held — behavior-identical, so we validate it safely. Keep
   the in-memory set as the cache.
3. **Piece 2 (ownership lease)** behind a flag (`GATEWAY_OWNERSHIP_LEASE`). At N=1 one task trivially
   owns every lease — behavior-identical. Validate lease acquire/renew/expire/forward at N=1.
4. **Flip `desiredCount` 1→2**, soak, then add autoscaling. Both problems fixed.

Each flag falls back to today's in-process behavior, so rollback is an env-var flip at every step.

---

## 5. Relationship to issue #780 (dead-air UX fix)

#780 adds a `run_active` signal that rides the **same fan-out path** as `chunk`/`done`
(`_forward_to_frontends`) plus an additive `sessions.changed` handler. Because this spec migrates
that exact path to DDB routing + owner-held subscription, #780 inherits multi-instance correctness
for free — and its `sessions.list`/`hasActiveRun` re-read is exactly what Piece 2 uses to
reconstruct run-active state after an ownership failover. Ship #780 after this; no rework.

---

## 6. Open decisions (resolve in the plan)

- **A. Task-to-task addressing for `/internal/rpc-forward`.** Two options: (a) store the owner's
  private `IP:port` in the lease row and POST directly (needs a security-group rule allowing
  backend-task → backend-task on an internal port; no Cloud Map) — *preferred for simplicity*; or
  (b) register the backend service in Cloud Map and resolve per-task. Decide in planning; (a) looks
  lighter. Sticky routing (pin user→task at the LB) is **rejected/blocked** — backend isn't in Cloud
  Map and the Lambda→ALB hop can't address a task.
- **B. Lease TTL / renew cadence.** Start 30s TTL / 10s renew; tune under a 2–4 task soak to avoid a
  write hotspot (batch renewal; on-demand billing).
- **C. Idempotency store.** In-memory LRU per task is insufficient at N>1 (a retry may hit a
  different task) → use a small DDB-backed idempotency table for mutating RPCs.

---

## 7. Risks & mitigations

- **Lease thrash / split-brain** (two tasks think they own a user): conditional-write with
  `version`; only the holder whose conditional renew succeeds keeps ownership; loser closes its
  connection. TTL bounds a dead owner.
- **Forwarding hop latency** (non-owner → owner per user message): in-VPC HTTP, low ms; acceptable.
  Mitigate by caching the lease lookup briefly.
- **DDB write hotspots** (lease renew + connection rows): on-demand billing, batched renew, KEYS_ONLY
  GSI already minimal.
- **Migration regressions:** flag-gated, each piece behavior-identical at N=1, env-var rollback.
- **Health-check flapping** from the enhanced check: keep it cheap and liveness-focused; don't tie
  it to external upstreams (avoid the Teams-broker-hang trap).

---

## 8. Testing strategy

- **Unit (backend, pytest):** `query_by_user_id`-based fan-out targets the right members; lease
  acquire/renew/expire/steal under contention; `/internal/rpc-forward` round-trip; graceful-shutdown
  drains RPCs before WS close and is bounded; shutdown 503 middleware; idempotency short-circuit.
- **Integration (N>1 simulation):** two backend instances against one mock container — verify single
  connection (one lease holder), cross-task delivery (client on task B receives events from owner on
  task A), and failover (kill owner → other task re-acquires + re-opens).
- **Infra:** CDK assertions for `minHealthyPercent`/`maxHealthyPercent`, `deregistrationDelay`,
  circuit breaker, the leases table + TTL.
- **Manual (dev):** deploy with the rolling config and confirm no chat interruption; kill a task at
  N=2 and confirm sessions continue. (Frontend CI is Lint & Build only — run vitest locally.)

---

## 9. File-change summary (orienting, not exhaustive)

**Backend:**
- `core/gateway/connection_pool.py` — `_forward_to_frontends` via `query_by_user_id` + cache; lease
  acquire/renew/release; owner-only reader/subscribe; non-owner RPC forward; graceful-shutdown drain;
  container-resolution graceful error.
- `core/services/connection_service.py` — already has `query_by_user_id`; add cache hooks if needed.
- `core/services/teams_event_broker.py` — DDB-routed fan-out; bounded shutdown.
- `routers/websocket_chat.py` — `POST /internal/rpc-forward`; shutdown-aware 503; RPC idempotency.
- `main.py` — bounded lifespan shutdown; enhanced health check; graceful worker/gauge cancel.

**Infra (CDK):**
- `lib/stacks/service-stack.ts` — `minHealthyPercent`/`maxHealthyPercent`, circuit breaker; later
  `desiredCount` + autoscaling; backend-task→task SG rule (Open Decision A).
- `lib/stacks/network-stack.ts` — `deregistrationDelay`.
- `lib/stacks/database-stack.ts` (or api-stack) — `container-connection-leases` table (+TTL),
  `rpc-idempotency` table.

**Frontend:** none required for this spec (reconnect/resync hardening is the small client-side part
of Piece 3 and can be a follow-up task in the plan).
