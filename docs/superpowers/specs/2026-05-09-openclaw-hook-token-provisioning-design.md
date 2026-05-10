# OpenClaw Hook Token Provisioning + Public Ingress

**Status:** design — pending review
**Date:** 2026-05-09
**Scope:** Provisioning piece only. UI panel and Gmail Pub/Sub follow-up specs separate.

## Context

OpenClaw's Gateway exposes an optional HTTP webhook ingress at `/hooks/*`, gated by a single shared-secret bearer token configured via `hooks.token` in `openclaw.json`. When enabled, it accepts `POST /hooks/wake` (enqueue a system event), `POST /hooks/agent` (run an isolated agent turn), and `POST /hooks/<name>` (custom mappings declared under `hooks.mappings`).

Today Isol8 does not provision this ingress for users. `apps/backend/core/containers/config.py:~654` writes only `hooks.internal` (in-process Gateway hooks like `command-logger`). No `OPENCLAW_HOOK_TOKEN` env is injected into the per-user task definition, and there is no public path that lands at a user's container `/hooks/*` endpoint.

This blocks any agent that wants to be triggered by an outside service — Zapier, Stripe webhooks, GitHub events, custom integrations, and the Gmail Pub/Sub flow many marketplace agents depend on. Specifically, the cofounder's agents (Atlas, Thread, Pitch, Vera, Echo, Ora, Pulse, Scout, Tally, Lens, Ember) all reference `${OPENCLAW_HOOK_TOKEN}` in their `openclaw.json` files; that env var is currently undefined in every container, so any hook-driven flow silently fails.

## Goals

1. Every user gets a working `/hooks` ingress automatically at provision — no manual setup.
2. Outside services can reach a user's `/hooks` endpoints via a stable public URL with bearer-token auth.
3. Existing users get backfilled.
4. Minimum UI surface so users can retrieve their URL + token without API spelunking.

## Non-Goals

- Mappings CRUD UI, test button, hook-event browsing, code-snippet templates. Those are a separate spec ("hooks control panel").
- Gmail Pub/Sub integration, OAuth flows, gog auth state rendering, watch-renewal cron. Those are a separate spec ("Gmail managed channel").
- Per-mapping or per-integration tokens. Single token per user, same as upstream.
- Token rotation automation. Manual-rotate only, surfaced in the follow-up panel.

## Design Decisions

### One token per user

A single `OPENCLAW_HOOK_TOKEN` per container. Matches upstream OpenClaw's native model exactly. If it leaks, the user rotates it manually (mechanism in follow-up spec). Per-integration tokens were considered and rejected as over-engineering for v1.

### Public ingress via FastAPI proxy on the existing backend

Public URL: `https://api.isol8.co/api/v1/hooks/u/{owner_id}/{hook_path}` (every isol8 router mounts under `/api/v1/`). The `{hook_path}` segment is the OpenClaw hook name — `wake`, `agent`, or any custom mapping registered in `hooks.mappings`. New router `apps/backend/routers/hooks_proxy.py`, modeled on the existing `paperclip_proxy.py` pattern. Reuses the existing ALB + ACM cert; no CDK network/DNS changes.

**Why a proxy at all — direct-to-container isn't an option today.** Per-user containers run on ECS Fargate in private subnets with no public IPs. They're not internet-routable. The only public entrypoints we have into the VPC are:

1. WebSocket API Gateway → VPC Link → NLB → ALB → backend (chat traffic)
2. ALB rules → backend FastAPI service (REST at `api.isol8.co`)
3. ALB rule → Paperclip BFF per tenant

No ALB rule targets per-user OpenClaw containers, and adding one per user doesn't scale (ALB has a 100-target-group-per-LB cap; we'd hit it immediately). Any "direct" alternative still requires *some* Isol8-owned component to resolve `owner_id → container IP`, because ALB target groups can't dispatch on a path parameter.

Rejected alternatives:
- **Direct ALB host rule + Lambda authorizer (`hooks.isol8.co`):** builds a Lambda-based equivalent of the FastAPI proxy. Same hop count and latency, more infra, less observability than reusing the backend.
- **Per-user ALB target group:** doesn't scale past 100 users; CDK churn on every provision.
- **Per-user subdomain (`{slug}.hooks.isol8.co`):** wildcard cert + wildcard DNS + ALB SNI routing or Lambda@Edge slug-resolver; still passes through Isol8 infra; requires a stable per-owner slug we don't have today.
- **Internet-facing container ports:** would require moving containers to public subnets with per-container public IPs. Massive change to the security model — containers are in private subnets deliberately.

The latency cost of going through the backend is ~10-30ms — well below what external webhook sources care about — and matches existing patterns (chat goes API Gateway → backend → container; Paperclip goes ALB → `paperclip_proxy.py` → tenant container).

### Edge token validation in the proxy

The proxy validates the bearer token against DDB before forwarding to the container. This duplicates OpenClaw's internal `hooks.token` check, which is the point: early reject means no container call for unauthorized requests, less load, simpler rate limiting, and we get a clean audit row at the edge.

### Audit trail in DDB

New table `hook-events` with TTL on `expires_at`. Stores `{owner_id, event_id, hook_path, timestamp, status_code, source_ip, user_agent, request_size, response_size}`. 30-day retention. Used by the follow-up panel; populated from day one so we have history when the panel ships.

### In-process rate limit

60 requests/minute per `owner_id`, in-process token bucket. Fine for the current single-task backend deployment. If/when we scale to N backend tasks, we move to a DDB-backed counter — flagged in this doc as a known-future limitation, not v1 work.

### Token storage on the existing `containers` row

`hook_token` lives on the `containers` row in DDB, plaintext, alongside the existing `gateway_token` field (`apps/backend/core/containers/ecs_manager.py:1112,1296`). Same security posture as `gateway_token` today — when we tighten that storage (e.g., move to Secrets Manager), both fields move together. No rationale to treat hook tokens differently.

## Architecture

```
External service (Zapier / Stripe / etc.)
        |
        |  POST https://api.isol8.co/api/v1/hooks/u/{owner_id}/{hook_path}
        |  Authorization: Bearer <hook_token>
        v
+----------------------------------------------------+
|  FastAPI backend (existing ECS service)            |
|  routers/hooks_proxy.py                            |
|    1. Validate bearer token vs DDB containers row  |
|    2. EcsManager.resolve_running_container(owner)  |
|       -> (container, ip)  (or 503 if not running)  |
|    3. Rate-limit check (60/min)                    |
|    4. Rewrite X-Forwarded-* with true caller IP    |
|    5. Forward request via httpx                    |
|    6. Write audit row to hook-events               |
+----------------------------------------------------+
        |
        |  POST http://<container-ip>:18789/hooks/{hook_path}
        |  Authorization: Bearer <hook_token>  (same token, forwarded)
        v
+--------------------------------------------+
|  Per-user OpenClaw container (ECS Fargate) |
|  Gateway validates hooks.token, dispatches |
|  to /hooks/wake, /hooks/agent, or mapping  |
+--------------------------------------------+
```

## Components

### Token generation + storage

In `apps/backend/core/containers/ecs_manager.py`, alongside the existing `gateway_token` generation at line 1296:

```python
hook_token = secrets.token_urlsafe(32)
```

Stored on the `containers` row at provision: `hook_token`, `hook_token_rotated_at`. The existing `containers` repo gets a setter; new fields are additive.

### Container env injection

In `ecs_manager.py:212`, where `environment_for_task` is layered onto the task definition, append:

```python
environment_for_task.append(
    {"name": "OPENCLAW_HOOK_TOKEN", "value": hook_token}
)
```

This plumbing already exists (used today for `CODEX_HOME` in the chatgpt_oauth flow); we add one entry. No CDK changes — the ECS task-def already accepts arbitrary env entries.

### Config rendering

In `apps/backend/core/containers/config.py:~654`, alongside the existing `hooks.internal` block, render:

```python
"hooks": {
    "internal": { ... existing entries ... },
    "enabled": True,
    "token": "${OPENCLAW_HOOK_TOKEN}",
    "path": "/hooks",
    "maxBodyBytes": 1048576,                # 1 MB — Stripe/GitHub routinely exceed the upstream 64KB pre-auth default
    "allowedSessionKeyPrefixes": ["isol8-", "hook:"],
    "allowRequestSessionKey": False,
}
```

`allowedAgentIds` is intentionally omitted — let any agent be addressable by hooks at v1. Tightening is a follow-up if abuse appears.

`maxBodyBytes: 1MB` matters: upstream's `WEBHOOK_BODY_READ_DEFAULTS` (`webhook-request-guards.ts:24`) caps pre-auth bodies at 64KB and post-auth at 1MB. Stripe webhook payloads with expanded objects, GitHub event payloads, and Zapier batched events all exceed 64KB regularly. Without this override, OpenClaw rejects pre-auth and the proxy returns 413 to a perfectly valid webhook.

`allowedSessionKeyPrefixes` MUST include `"hook:"`. OpenClaw v2026.5.7's `gateway/hooks.ts:75-95` enforces a runtime invariant: when `defaultSessionKey` is unset (it is in our config), `allowedSessionKeyPrefixes` must include `"hook:"` or the gateway throws at boot with `"hooks.allowedSessionKeyPrefixes must include 'hook:' when hooks.defaultSessionKey is unset"`. Omitting it means every newly-provisioned container refuses to start.

The trusted-proxy infrastructure on the OpenClaw side is already in place — no additional `gateway` block work is required. Today's `config.py:544-557` already renders:

```python
"gateway": {
    "mode": "local",
    "bind": "lan",                                       # binds on container ENI, not loopback
    "auth": {"mode": "token", "token": gateway_token},   # token auth, intentionally NOT trusted-proxy mode
    "trustedProxies": ["10.0.0.0/8", "127.0.0.1", "::1"],# covers our backend subnets
    "controlUi": {"enabled": False},
    "http": { ... },
},
```

This was set up for the existing chat/RPC traffic from the backend, but it's exactly the upstream-recommended posture for running OpenClaw behind a trusted reverse proxy (see `~/Desktop/openclaw/docs/gateway/trusted-proxy-auth.md`). The hook ingress inherits it for free:

- `bind: "lan"` means OpenClaw's HTTP server already listens on the container's VPC ENI; the backend's outbound HTTP from a sibling subnet reaches it.
- `trustedProxies: ["10.0.0.0/8", ...]` already trusts our backend's source subnet, so `X-Forwarded-For` headers from `hooks_proxy.py` will be honored in OpenClaw's audit logs.
- `auth.mode: "token"` is unchanged. `gateway.auth.mode: "trusted-proxy"` is a different upstream feature for delegating user-identity auth to an OAuth proxy (Pomerium / Caddy / oauth2-proxy). The existing inline comment immediately above the rendered `gateway.auth` block in `config.py` explains why we don't use it: trusted-proxy mode blocks loopback, which would break the in-container agent calling its own gateway. Hook calls authenticate via bearer token in `hooks.token`, independent of `auth.mode`.

Net delta to `config.py`: just the `hooks` block above. No `gateway` changes.

### Public ingress proxy

New file: `apps/backend/routers/hooks_proxy.py`. Pattern modeled on `routers/paperclip_proxy.py`:

```python
@router.api_route(
    "/hooks/u/{owner_id}/{hook_path:path}",
    methods=["POST"],
)
async def proxy_hook(owner_id: str, hook_path: str, request: Request):
    # 1. Extract bearer token from Authorization header (return 401 if absent/malformed)
    # 2. Look up containers row for owner_id; return 404 if not found
    # 3. hmac.compare_digest(token, container.hook_token) — return 401 if mismatch
    # 4. Rate-limit check (60/min per owner_id) — return 429 + Retry-After if exceeded
    # 5. EcsManager.resolve_running_container(owner_id) -> (container, ip)
    #    If ip is None (task not running, scaling, recovering) -> return 503
    #    with Retry-After: 30 (matches the 409 "container_not_running" pattern
    #    used by routers/admin.py:441 for the admin-side equivalent)
    # 6. Build forwarded headers — strip any inbound X-Forwarded-*,
    #    set X-Forwarded-For to the *real* caller IP (from the inbound
    #    request's first-hop address; ALB places it as the rightmost
    #    entry of the inbound XFF chain), X-Forwarded-Proto: https,
    #    X-Forwarded-Host: api.isol8.co. Forward Authorization verbatim.
    # 7. httpx.AsyncClient.post to http://{ip}:18789/hooks/{hook_path}
    #    with the rebuilt headers + original body. Stream body to avoid
    #    buffering large Stripe/GitHub payloads.
    # 8. Write audit row to hook-events (record original caller IP, status,
    #    sizes). Audit row is best-effort — log-and-continue on DDB error
    #    so a transient DDB failure doesn't drop a real webhook.
    # 9. Return container's response with same status code.
```

**No Clerk auth dependency.** Unlike every other isol8 router, this endpoint does **not** depend on `get_current_user`. External webhook callers don't have Clerk tokens. The router's *only* auth is the timing-safe bearer-token compare against `containers.hook_token` (step 3, using `hmac.compare_digest`). Document this explicitly in the router's module docstring.

X-Forwarded-* headers are rewritten, not pass-through. We do not trust X-Forwarded-* values from the public caller (those could be spoofed). The ALB strips and re-asserts the chain at the edge; our proxy then re-asserts it again with values we control. OpenClaw on the container side trusts these headers because our backend's subnet is in `gateway.trustedProxies`.

**No idempotency at the proxy level.** External senders (Stripe, GitHub) retry on transient errors, and we forward every retry. Agents must handle idempotency themselves (most webhook senders include a unique event ID — `Stripe-Signature`, `X-GitHub-Delivery`, etc.). The repo's existing `webhook-event-dedup` table (`database-stack.ts:161`) is for a separate Clerk/Stripe webhook surface and is not consumed by this proxy. We don't add a second dedup mechanism in v1.

Mounted under `/api/v1/hooks` in `main.py`, giving the public URL `https://api.isol8.co/api/v1/hooks/u/{owner_id}/{hook_path}`.

No CORS configuration is needed — external webhook callers (server-to-server) don't send `Origin` headers, so CORS is a no-op for them. The browser-facing settings page reads credentials via the standard `/api/v1/hooks/credentials` endpoint, which uses the existing CORS rules already configured for `/api/v1/*`.

### Audit table + IAM wiring

New DDB table `hook-events` in `apps/infra/lib/stacks/database-stack.ts`. Match the conventions of every other table in that file (customer-managed KMS, point-in-time recovery, removal policy):

```typescript
public readonly hookEventsTable: dynamodb.Table;

// in constructor:
this.hookEventsTable = new dynamodb.Table(this, "HookEvents", {
  tableName: `${prefix}-hook-events`,
  partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
  sortKey:      { name: "event_id", type: dynamodb.AttributeType.STRING },  // ULID — time-ordered
  billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
  encryption:   dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: this.tableKey,
  pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
  timeToLiveAttribute: "expires_at",
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
```

No GSI — `event_id` is a ULID, so a query against the base table by `owner_id` returns events in chronological order without an index.

**IAM wiring (this is what every other table does — easy to miss):**

1. Pass the table through `service-stack.ts` props alongside the existing tables (`UsersTable`, `ContainersTable`, etc.).
2. Inside `service-stack.ts`, add `props.database.hookEventsTable.grantReadWriteData(this.taskRole)` next to the existing grants at `service-stack.ts:288-316`. Without this, the proxy gets `AccessDeniedException` on every audit write.
3. Also grant `props.database.tableKey.grantEncryptDecrypt(this.taskRole)` if not already broad enough (the existing tables share one KMS key, so this is typically already granted; verify by reading the surrounding code).

New repository `apps/backend/core/repositories/hook_event_repo.py` follows the existing repo pattern (e.g., `usage_repo.py`).

No GSI on `containers.hook_token`. The proxy validates by `(owner_id, hook_token)` and we already know `owner_id` from the URL path, so a primary-key Get is sufficient. (The existing `containers.gateway_token` GSI exists for a different lookup pattern and is unrelated.)

### Backfill for existing users

**Why backfill needs more than a config patch.** New users get `OPENCLAW_HOOK_TOKEN` because `environment_for_task.append(...)` runs at task-definition registration time inside `ecs_manager.py`. Existing users' task-defs were registered before this change — they have no such env entry. Patching their `openclaw.json` on EFS makes OpenClaw read `"token": "${OPENCLAW_HOOK_TOKEN}"` and resolve `${VAR}` against a process environment where the var simply doesn't exist, leaving the gateway with a `null`/empty token. So backfill must re-register the task definition with the new env list and trigger an ECS service redeploy onto that new revision — config patching alone is not sufficient.

New file: `scripts/backfill-hook-tokens.py`. Scans the `containers` table; for each row missing `hook_token`:

1. Generate a token, write to the row.
2. Re-register the user's ECS task definition with the new `OPENCLAW_HOOK_TOKEN` env entry. The existing reprovision path in `apps/backend/routers/debug.py` (`DELETE /api/v1/debug/provision` + `POST /api/v1/debug/provision`) re-registers the task-def end-to-end, but it also tears down the EFS access point — too heavy for a backfill (loses agent state). Instead, the backfill calls a new admin-only helper `EcsManager.refresh_task_definition(owner_id)` which:
   - Reads the existing service config + EFS volume mount + secrets list,
   - Re-generates `environment_for_task` (including the new `OPENCLAW_HOOK_TOKEN`),
   - Calls `ecs.register_task_definition` with the rebuilt entry,
   - Calls `ecs.update_service` with the new task-def revision (forces ECS to roll the running task).
   This is essentially the second half of the existing provision flow factored out so backfill can call it without DELETE/POST churn.
3. After the redeployed task is `RUNNING` (poll via `describe_services`), call the existing config patcher (`PATCH /api/v1/admin/users/{user_id}/config` at `apps/backend/routers/admin.py:403-419`) to write the new `hooks` block into the user's `openclaw.json`. This step is what makes the gateway *use* the env var; step 2 only made it exist.

Idempotent and safe to re-run. Logs each user's outcome. Triggered manually post-deploy by ops.

**The new `EcsManager.refresh_task_definition` helper is the only new code surface for backfill** — the rest is orchestration in the script. Add a unit test that constructs a fake provisioned user with an old task-def (no `OPENCLAW_HOOK_TOKEN`), runs `refresh_task_definition`, and asserts the new task-def has the env entry while the EFS access point is unchanged.

### Minimum UI surface

The existing Settings page (`apps/frontend/src/app/settings/page.tsx`) is panel-driven, not card-driven: it has a `Panel = "profile" | "billing" | "channels"` type, a `NAV_SECTIONS` left-nav array, and a switch in the renderer. Adding a "Webhooks" surface requires a new panel, not a new card on an existing one:

1. Extend `Panel` to include `"webhooks"`.
2. Add a `NAV_SECTIONS` entry: `{ key: "webhooks", label: "Webhooks", icon: <link icon> }`.
3. Add a switch branch rendering a new `WebhooksPanel` component.
4. New file `apps/frontend/src/components/settings/WebhooksPanel.tsx` — read-only at v1, two rows:
   - **Public URL:** `https://api.isol8.co/api/v1/hooks/u/{owner_id}/` with a copy button.
   - **Hook token:** bullet-mask by default with "Show" toggle, copy button, "Last rotated: <date>" muted text below.

No rotate button, no add/edit mappings, no test button — those are the follow-up panel.

Backend: new endpoint `GET /api/v1/hooks/credentials` (in `routers/hooks_proxy.py` or a sibling file) that uses `Depends(get_current_user)` and returns `{public_url, token, rotated_at}` for the calling user. Frontend: new SWR hook `apps/frontend/src/hooks/useHookCredentials.ts` follows the existing pattern (e.g. `useBilling.ts`) — wraps `useApi().get('/hooks/credentials')` with SWR.

## Data Model

### `containers` table — new fields

| Field | Type | Notes |
|---|---|---|
| `hook_token` | String | URL-safe random, 32 bytes via `secrets.token_urlsafe(32)`. Plaintext, same as `gateway_token`. |
| `hook_token_rotated_at` | String (ISO timestamp) | Set at provision and on future rotate. |

### `hook-events` table — new

| Field | Type | Notes |
|---|---|---|
| `owner_id` | String | Partition key. |
| `event_id` | String | Sort key. ULID — chronologically ordered, no GSI needed. |
| `timestamp` | Number | Unix ms. Stored for convenience; not an index key. |
| `hook_path` | String | E.g. `wake`, `agent`, `atlas-build-start`. |
| `status_code` | Number | HTTP status returned to caller. |
| `source_ip` | String | Original caller IP, extracted from inbound XFF chain at the proxy. |
| `user_agent` | String | Truncated to 512 bytes. |
| `request_size` | Number | Bytes. |
| `response_size` | Number | Bytes. |
| `expires_at` | Number | TTL, Unix seconds, +30 days from `timestamp`. |

## Testing

- **Unit — proxy router:**
  - Token validation (timing-safe compare, 401 on bad/missing/malformed Authorization)
  - 404 when `owner_id` has no `containers` row
  - 503 + Retry-After when `resolve_running_container` returns no IP (stale / scaling / recovering task)
  - 429 + Retry-After when rate limit exceeded
  - X-Forwarded-* header rewrite (inbound spoofing attempt is replaced, not appended)
  - Audit-row shape, including the best-effort write-failure path (DDB error must not drop the proxied call)
- **Unit — backfill helper:** `EcsManager.refresh_task_definition` rebuilds env list including `OPENCLAW_HOOK_TOKEN`, leaves EFS access point untouched, calls `update_service` with the new task-def revision.
- **Integration:** end-to-end against LocalStack + a stubbed `/hooks` responder; verifies forwarding, header rewrite, audit-row write.
- **Manual smoke (post-deploy):** curl the cofounder's hook URL with a known good payload; verify the agent receives the wake event and the audit row appears in `hook-events`.

## Rollout

1. Land backend changes (token gen, env injection, config rendering, proxy router, audit table) — single PR.
2. Deploy to dev. Manually verify with a test user: provision new user → curl their hook URL → agent receives `wake` event.
3. Land minimum UI (settings panel + `/hooks/credentials` REST endpoint) — single PR. Deploy to dev. Verify.
4. Run backfill script in dev for existing test users. Verify each gets a working URL/token.
5. Promote to prod via existing CI/CD. Run backfill in prod after deployment lands.
6. Notify cofounder.

## Known Future Limitations

- **Rate limiter is in-process.** Fine for single-task backend; needs DDB-backed counter when we scale out. Not v1 work; flagged here so we don't forget.
- **No rotate UI in v1.** Backend supports rotation (regenerate token, redeploy task, patch config); the UI button lives in the follow-up panel spec. Until then, rotation is an admin-only operation.
- **No `allowedAgentIds` restriction.** Any agent in a user's container is addressable via `/hooks/agent`. Tighten if abuse appears.
- **No HMAC signing on the public URL.** Bearer-token-only auth. Stripe-style signed webhooks come with the per-mapping tokens deferred to v2.

## Open Questions

None. All design decisions resolved during brainstorming on 2026-05-09. Spec corrected against both codebases on 2026-05-09 after a code-review pass against `apps/backend/`, `apps/infra/`, and the upstream OpenClaw clone at `~/Desktop/openclaw/`.
