# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isol8 is an AI agent platform powered by [OpenClaw](https://github.com/openclaw/openclaw). Users subscribe, get a per-user ECS Fargate container running the OpenClaw Docker image, and interact with their agents via a WebSocket-based chat UI. The platform uses a Next.js 16 frontend (Vercel), FastAPI backend (ECS Fargate), Clerk for authentication, **DynamoDB** for metadata (users, containers, billing, api-keys, usage counters, pending updates, channel links, admin actions, marketplace listings/purchases/payouts/takedowns, plus the Teams BFF tables), AWS Bedrock + ChatGPT OAuth + BYO API key for LLM inference, Stripe for billing (subscription + marketplace purchases), EFS for per-user agent workspaces, and a per-tenant Paperclip BFF for the Teams Inbox. Infrastructure is managed via **AWS CDK** in `apps/infra/` (single source of IaC truth). The desktop app is **Tauri** (not Electron) — see `project_desktop_app_tauri` memory.

Every freshly provisioned owner gets two seed agents inside their Paperclip company: **Friday** (the per-owner CEO / orchestrator) and **Atlas** (the in-house agent-recruiter). Both are seeded by `paperclip_provisioning.provision_org` — see the "Provisioning Flow" section below. Custom agents beyond Friday/Atlas can be **published to the Isol8 marketplace** for other users to deploy — see the "Marketplace" section.

The OpenClaw container image Isol8 runs is an **extended image** built from the **Isol8 OpenClaw fork** (`ghcr.io/isol8ai/agent`, a **Debian**-based image — NOT Alpine) with additional Linux skill binaries and baked externalized plugins (Slack/Discord/Bedrock). Its Dockerfile lives at `apps/infra/openclaw/` and is published to ECR `isol8/openclaw-extended` by `.github/workflows/build-openclaw-image.yml`. The pinned base + per-env extended tags are tracked in `openclaw-version.json` at the repo root, where the fields mean distinct things:
- `upstream` — the fork base the Dockerfile `FROM`s (e.g. `ghcr.io/isol8ai/agent:2026.7.2-isol8.2`). A **private** GHCR package, pulled only by the authenticated build workflow. Keep it in lockstep with the Dockerfile `FROM` line.
- `image`/`tag`/`full` — the **legacy public** upstream image (`ghcr.io/openclaw/openclaw`), kept only as the bootstrap/unknown-env fallback in `container-stack.ts` (pulled by per-user ECS tasks with **no** registry credentials, so it must stay anonymously pullable). `tag` also feeds the `OPENCLAW_UPSTREAM_VERSION` env fallback.
- `extendedImage` + per-env `dev.tag`/`prod.tag` — the ECR extended image actually deployed; `dev.tag`/`prod.tag` are the per-environment promotion gate (flip dev, validate, then prod). They drive `meta.lastTouchedVersion` via `deployed_upstream_version()`.

Bundled agent-facing skills live at `apps/infra/openclaw/skills/` (`paperclip`, `isol8`). The `isol8` skill is the fleet's knowledge of the platform itself — when user-facing surfaces, marketplace/billing flows, or Paperclip seeding change, update its SKILL.md (anchors listed in its maintenance header). Per-owner dynamic state (plan/provider) is NOT in the skill; it lives in `~/.openclaw/isol8-facts.json`, written by `core/services/owner_facts.py`.

The Isol8 repo does not vendor OpenClaw source. The base image is built from the **Isol8 fork `Isol8AI/agent`** (a GitHub fork of [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)). The fork's `main` tracks upstream — it was realigned to `openclaw/openclaw:main`, so `git pull upstream main` merges cleanly. Isol8's patches (e.g. the MCP non-blocking-materialization + shared-runtime fixes) do **not** live on `main`; they sit on `isol8/*` branches plus `v<version>-isol8.<n>` tags, which the fork's `docker-release.yml` builds and publishes to GHCR `ghcr.io/isol8ai/agent`. The writable fork workspace is checked out at `~/Desktop/agent` (origin = `Isol8AI/agent`, upstream = `openclaw/openclaw`). A separate **read-only** vanilla-upstream checkout sits at `~/Desktop/openclaw` (sibling of `~/Desktop/isol8.nosync`). Treat both as outside this repo — do not commit either into it.

Each user gets their own isolated OpenClaw container, with a persistent WebSocket connection pool on the backend proxying RPC calls and streaming events back to the frontend.

## Critical Rules

**NEVER modify production servers directly.** All changes to backend, frontend, or infrastructure MUST:
1. Be version controlled in the appropriate repository
2. Go through CI/CD pipelines for deployment

This includes environment variables -- use AWS CDK, GitHub secrets, and AWS Secrets Manager.

**Always watch your runs after you make a push to a repository.**

**NEVER overwrite any environment or docker files, or git repos without permission.**

**If the tests are not passing, your change is likely the culprit, even if the error is not in a file you touched. These are all dependent systems.**

**NEVER use `sleep` or `tail` commands in bash.** Use proper tool-based approaches:
- Instead of `sleep`: use `gh run watch` for CI/CD, or tool-based polling
- Instead of `tail`: use the Read tool to read files, or run commands without piping through tail

**Planning docs are disposable.** Plans/specs under `docs/superpowers/` are working
documents for in-flight work. When the feature ships (or the approach is abandoned),
DELETE the doc in the same PR or shortly after — git history preserves it, and the
PR description is the durable record. The only plans/specs that stay are ones living
code or CLAUDE.md explicitly references. Never let shipped plans accumulate: a
2026-07 purge deleted 250 of 260 such files (196k lines) — don't rebuild that pile.
The same applies to one-shot backend scripts: once a backfill has run and the
data it wrote is produced at creation time, DELETE the script and its tests —
git history is the operational record (it carries the run date and PR context
an archive directory does not). Re-runnable ops tools stay in
`apps/backend/scripts/`.

### Monorepo Setup

This is a **Turborepo monorepo** using **pnpm** for JS and **uv** for Python.

```bash
pnpm install                          # All JS deps (from repo root)
cd apps/backend && uv sync            # Python deps
```

### Individual Services

**Database:** DynamoDB tables provisioned by CDK (`apps/infra/lib/stacks/database-stack.ts`). No relational DB — items are plain dicts, accessed via repositories in `apps/backend/core/repositories/`.

```bash
turbo run dev                              # everything, in parallel
turbo run dev --filter=@isol8/frontend     # or @isol8/backend
cd apps/backend  && uv run uvicorn main:app --reload --port 8000
cd apps/frontend && pnpm run dev           # localhost:3000; also `build`, `lint`
```

### Running Tests

```bash
turbo run test                        # All tests (parallel, cached)
turbo run test --filter=@isol8/backend    # Backend only
turbo run test --filter=@isol8/frontend   # Frontend only
turbo run lint                        # All linting
```

Backend: `cd apps/backend && uv run pytest tests/ -v`
Frontend unit: `cd apps/frontend && pnpm test`
Frontend E2E: `cd apps/frontend && pnpm run test:e2e`

### Container Re-provisioning (Dev)

When you change `openclaw.json` config, existing containers keep the old config on EFS. Re-provision:

```javascript
// Browser console (any Isol8 page with Clerk loaded):
// PATCH -- rewrites openclaw.json + redeploys (preserves container)
await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {method:'PATCH', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())

// Full reprovision -- DELETE then POST
await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {method:'DELETE', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {method:'POST', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
```

Debug endpoints return 403 in production.

### Clean-Slate Dev Reset

Full dev wipe (ECS, EFS, DynamoDB, Paperclip Aurora, Stripe, Clerk) moved to the
`dev-clean-slate-reset` skill (`.claude/skills/dev-clean-slate-reset/SKILL.md`).
Requires AWS SSO (`aws sso login --profile isol8-admin`).

### Prod Org Reprovision

Wipe an owner's container runtime + Paperclip side while preserving billing,
subscription, Clerk org, and marketplace rows — moved to the
`prod-org-reprovision` skill (`.claude/skills/prod-org-reprovision/SKILL.md`).

### Manual dev testing accounts

DO NOT use `isol8-e2e-testing@mailsac.com` for manual dev testing. That Clerk
account is reserved for the Playwright E2E journey test (run on-demand).
The E2E journey test cancels + recreates a Starter subscription on that
account on each run, which will clobber any manual testing state.

For manual dev testing, create your own Clerk account (or use an existing
personal email) at https://dev.isol8.co/sign-up. The dev Clerk instance is
the same one used for prod previews, so test accounts created in dev are
real accounts -- don't use a production email.

### CI/CD Monitoring

```bash
gh run watch <run-id> --repo Isol8AI/isol8 --exit-status
gh run view <run-id> --repo Isol8AI/isol8 --json status,jobs
```

**Deployment methods:**
- **Frontend:** Vercel auto-deploy on push to main (configured via Vercel dashboard)
- **Backend:** GitHub Actions (`backend.yml`) → ECR Docker build → ECS Fargate service deploy
- **Infrastructure (CDK):** GitHub Actions (`deploy.yml`) → `cdk synth` → `cdk deploy` per environment
- **Extended OpenClaw image:** GitHub Actions (`build-openclaw-image.yml`) → build on Dockerfile/`openclaw-version.json` changes → push to ECR `isol8/openclaw-extended`
- **Desktop app:** GitHub Actions (`desktop-build.yml`) → signed DMG via Tauri. Standalone workflow (NOT part of `deploy.yml`), path-filtered on `apps/desktop/**`, runs on a **self-hosted macOS runner** (`runs-on: [self-hosted, macos-desktop]`). Version is `0.4.${{ github.run_number }}`.
- **E2E journey test:** GitHub Actions (`e2e-dev.yml`) runs the Playwright journey test against dev **on-demand only** (not on every deploy); keep regularly-needed coverage hermetic/per-PR

---

## Architecture

### High-Level Data Flow

```
Client --> wss://ws-dev.isol8.co --> API Gateway --> Lambda Auth --> VPC Link V1 --> NLB --> ALB
  --> FastAPI /ws/message --> Cloud Map DiscoverInstances --> per-user OpenClaw container (ECS Fargate)
  <-- FastAPI pushes streaming chunks via Management API
                                                    |
                                        EFS /mnt/efs/users/{user_id}/
                                        agents/{agent_uuid}/
```

### Production Infrastructure

Turborepo monorepo (`Isol8AI/isol8`): `apps/{frontend,backend,desktop,infra,mobile}`,
`packages/` (shared TS). The non-obvious entries at the root:

- `paperclip/` — **GITIGNORED**, a read-only clone of *vanilla* upstream
  (`paperclipai/paperclip`). Reference only. **NOT what we deploy** — see the
  fork note under AWS CDK Stacks.
- `openclaw-version.json` / `paperclip-version.json` — the image pins. Both
  carry a `notes` field explaining what each field means and what may not be
  changed without breaking the fleet. Read it before bumping.

### AWS Configuration

| Setting | Value |
|---------|-------|
| Account ID | `877352799272` |
| Deploy Region | `us-east-1` |
| SSO Start URL | `https://isol8.awsapps.com/start` |
| CLI Profile | `isol8-admin` |

---

## Backend (FastAPI + DynamoDB)

**Entry point:** `main.py` -- FastAPI app with lifespan handler that boots the gateway connection pool, starts container lifecycle management (`startup_containers` / `shutdown_containers`), starts the update-service scheduled worker, boots the Teams event broker singleton, and installs observability middleware. CORS middleware, custom OpenAPI schema with BearerAuth.

No SQLAlchemy, no ORM — DynamoDB items are plain dicts accessed via repositories.

> **File inventories are not kept here.** They rot faster than anyone updates
> them. Run `ls` on the directory you're touching. What this file documents is
> the *why* — invariants, single-writers, and things that look wrong until you
> know the reason.

### Core (`core/`)

Flat modules: `config.py` (Pydantic settings), `auth.py` (Clerk JWT via PyJWT;
`get_current_user` → `AuthContext`, JWKS cache w/ 1-hour TTL), `dynamodb.py`,
`encryption.py` (Fernet), `constants.py` (`SYSTEM_ACTOR_ID = "__system__"`),
`openclaw_version.py`, `secrets_manager.py`, `tenancy_codes.py`.

Subpackages: `billing/` (Bedrock AIPs, pricing, gate URL — see the Billing
section), `containers/`, `gateway/`, `repositories/`, `services/`, `crypto/`,
`middleware/`, `observability/`.

### Repositories (`core/repositories/`)

One repo per DynamoDB table; items are plain dicts. Table names follow
`isol8-{env}-{suffix}` (see `database-stack.ts`).

### Container Orchestration (`core/containers/`)

| File | Purpose |
|------|---------|
| `__init__.py` | Singletons: `get_ecs_manager()`, `get_workspace()`, `get_gateway_pool()`. `startup_containers()` / `shutdown_containers()` lifecycle. Usage-recording callback. |
| `ecs_manager.py` | Per-user ECS Fargate lifecycle. Container IP discovery is **Cloud Map** `DiscoverInstances` (`discover_ip`), **not** `describe_tasks`: every per-user service registers into ONE shared Cloud Map service via `serviceRegistries`, and the lookup is scoped to a tenant by filtering the `ECS_SERVICE_NAME` attribute. `HealthStatus="HEALTHY_OR_ELSE_ALL"` because the shared service has no `HealthCheckCustomConfig`, so ECS-registered instances report `UNKNOWN`. **Never cache a container IP across reconnects** — the service is long-lived but the task IP is ephemeral (changes on every deploy / OOM / health-restart / reprovision); a cached dead IP caused a prod outage where the pool showed "connected" and every RPC timed out. Per-user EFS access points. Service naming: `openclaw-{user_id}-{hash}`. |
| `config.py` | `write_openclaw_config()`: generates `openclaw.json` with Bedrock provider and memory plugin. |
| `workspace.py` | EFS file I/O at `/mnt/efs/users/{user_id}/`. |

### Gateway (`core/gateway/`)

| File | Purpose |
|------|---------|
| `connection_pool.py` | `GatewayConnectionPool`: persistent WebSocket connections to per-user OpenClaw containers. OpenClaw protocol v4 handshake, RPC proxy, event forwarding, idle scale-to-zero reaper, usage callback. |
| `node_connection.py` | Per-user node-host connection for desktop browser-proxy / exec-approval flows. |

### Services (`core/services/`)

`ls apps/backend/core/services/` for the current list. The ones whose behaviour
you cannot guess from the filename:

- `user_provision.py` — `ensure_user_ready` is the SINGLE canonical readiness
  entry point, called only from `GET /container/status`. Owner path seeds
  Paperclip synchronously, then dispatches the container provision as a
  background task.
- `provision_dispatch.py` — `dispatch_provision`, the single provisioning
  funnel. A fresh claim starts the SFN watchdog execution FIRST, then runs
  attempt 0 as a backend child process for speed. The machine watches the
  row (watch loop in `service-stack.ts`) and takes the ECS leg over on
  error or after its tick backstop — custody never depends on a client poll.
- `paperclip_provisioning.py` — owns `provision_org`, which seeds Friday + Atlas.
- `config_patcher.py` — EFS-safe JSON deep-merge under NFS-compatible
  `fcntl.lockf` (not `flock`).
- `clerk_admin.py` — Clerk REST wrapper for the admin surface. New Clerk calls
  **should** go through here, but two predate it and still hit `api.clerk.com`
  inline via httpx: `routers/billing.py` and `routers/desktop_auth.py`. A
  fleet-wide change to how we call Clerk has to touch all three.
- `node_proxy.py` — per-user node-host tracking. Lives in services/, not
  routers/, despite the name; `routers/websocket_chat.py` consumes it directly.
- `catalog_service.deploy_from_artifact` — the consumption-side entry point for
  marketplace deploys.
- `agent_export.py` — seller-agent → tarball snapshotter. It tars
  `workspace-{id}/` (the seller's actual content) and **never** `agents/{id}/`
  (OpenClaw runtime state — `sessions/` logs, `agent/` auth tokens). Runtime
  state is excluded *by which root it points at*, not by a filter list; the
  filters only drop dotfiles and junk dirs. See `_resolve_agent_dir`'s docstring.

### Routers (`routers/`) — all mounted under `/api/v1`

~40 flat modules plus the `routers/teams/` subpackage (the Teams Inbox BFF —
agents, threads, messages, channels, members, attachments, search; composes the
`paperclip_*` services). `ls` both for the current set.

Worth knowing: `paperclip_proxy.py` is the per-tenant Paperclip proxy and has a
route-filter security model — see `docs/audit-2026-05-02-paperclip-routes.md`
before touching it. `debug.py` returns 403 in prod.

### Models (`models/`) and schemas (`schemas/`)

`models/` is intentionally empty — DynamoDB items are plain dicts, no ORM.
`schemas/` holds the Pydantic request/response models for the HTTP surface.

---

## Frontend (Next.js 16 App Router)

React 19, Tailwind CSS v4, Clerk auth, SWR, lucide-react icons.

### Pages (`src/app/`)

`find apps/frontend/src/app -name page.tsx` for the current routes. The two
that will mislead you otherwise:

- **`/chat`** is the whole app. Desktop renders the **dockable workspace shell**
  (`components/workspace/` — chat, teams, control, file-viewer panels); mobile
  renders `ChatLayout` via `MobileChatShell`. Teams and control are in-shell
  panels, **not routes** — the legacy `/teams/*` URL tree was removed and 404s.
- **`/admin`** only resolves on allowed admin hosts (`admin.isol8.co`,
  `admin.dev.isol8.co`, `admin.localhost:3000`) and 404s everywhere else.
  Allowlist lives in `apps/frontend/src/middleware.ts`.

### Source map (`src/`)

Per-file tables drift too fast to keep here — run `ls` on the directory you're
touching. The load-bearing structure:

- `components/workspace/` — the dockable desktop shell: `panelRegistry.ts` /
  `panelComponents.ts` map panel keys → components; `workspaceController.ts` +
  `layoutStorage.ts` own layout state. Start here for anything "where does X
  render on desktop".
- `components/chat/` — chat surface + onboarding (`AgentChatWindow`,
  `MessageList`, `ChatInput`, `ProvisioningStepper`, provider-setup steps).
  `ChatLayout.tsx` is the **mobile-only** shell.
- `components/teams/` — Teams Inbox (panels routed by `TeamsPanelRouter.tsx`
  via `PANEL_LOADERS`/`DETAIL_LOADERS`; shared types in `shared/types.ts`;
  ported-from-upstream-Paperclip modules carry a header saying so).
- `components/control/` — control panels (`ControlPanelRouter.tsx` routes;
  `ControlSidebar.NAV_ITEMS` is the live, sidebar-visible set).
- the rest of `components/` (`admin`, `agents`, `blog`, `channels`, `desktop`,
  `getstarted`, `landing`, `legal`, `marketplace`, `notifications`,
  `onboarding`, `providers`, `settings`, `support`, `ui`) — what their names
  say; `ui/` is shadcn/ui.
- `hooks/` — `useGateway.tsx` (WebSocket provider: reconnection, ping/pong,
  event pub-sub, RPC matching), `useAgentChat.ts` (chat state machine),
  `useGatewayRpc.ts` (SWR-wrapped RPC), `useTeamsApi.ts` (Teams BFF fetch),
  `useProvisioningState.ts` (stepper/blocked-state machine), plus ~25 more.
- `lib/api.ts` — `useApi()`: authenticated REST methods (`get`, `post`, `put`,
  `del`, `uploadFiles`). New data-fetching should go through this (or
  `useGatewayRpc` for WS RPC), not hand-rolled `fetch`.

### Middleware

`src/middleware.ts` -- Clerk middleware protects `/chat(.*)`, `/onboarding`, `/settings(.*)`. An admin-host gate (`decideAdminHostRouting`) also runs on every request: `/admin` paths on non-admin hosts return 404, and admin hosts straying off `/admin` redirect back to `/admin`.

---

## Agent Message Flow

```
Client          API Gateway WS      FastAPI              OpenClaw (ECS Fargate)
  |                  |                  |                       |
  | WS message       |                  |                       |
  | {type:"agent_chat|  $default route  |                       |
  |  agent_id,       |  POST /ws/message|                       |
  |  message}        |  (VPC Link/NLB)  |                       |
  |----------------->|----------------->|                       |
  |                  |                  | Cloud Map discover    |
  |                  |                  | -> user container IP  |
  |                  |                  | WebSocket RPC pool    |
  |                  |                  | chat.send {message}   |
  |                  |                  |---------------------->|
  |                  |                  |                       | Bedrock (IAM)
  |                  |                  |  agent events:        |
  |                  |                  |  stream: "assistant"  |
  |                  |  Management API  |  stream: "tool"       |
  | WS push          |  (HTTPS POST    |  chat: "final"        |
  | {type:"chunk"}   |   @connections)  |<----------------------|
  | {type:"tool_start"}                 |                       |
  | {type:"tool_end"}                   |                       |
  | {type:"done"}    |                  |                       |
  |<-----------------|<-----------------|                       |
```

**Event transformation** (`connection_pool.py:_transform_agent_event`):
- `stream: "assistant"` with text -> `{type: "chunk", content: text}`
- `stream: "tool"`, `phase: "start"` -> `{type: "tool_start", tool: name}`
- `stream: "tool"`, `phase: "result"` -> `{type: "tool_end", tool: name}`
- `chat` state `"final"` -> `{type: "done"}`
- `chat` state `"error"` -> `{type: "error", message: ...}`
- `chat` state `"aborted"` -> `{type: "error", message: "Agent run was cancelled"}`

---

## WebSocket Architecture

**Why WebSocket?** API Gateway HTTP API buffers SSE responses, breaking streaming. WebSocket API Gateway supports true real-time bidirectional communication.

**Key design decisions:**
1. VPC Link V1 (WebSocket API requires it, only supports NLB)
2. NLB -> ALB -> ECS Fargate (backend service) chain (Layer 4 -> Layer 7)
3. Management API for responses (HTTPS POST to `@connections/{id}`)
4. Lambda Authorizer validates Clerk JWT
5. DynamoDB stores connectionId -> user_id mapping

---

## Provisioning Flow

A first-time user moves from "just signed up" to "ready to chat" through one canonical path. Understanding it matters because **Paperclip wiring is only set up at provision time** — there is no PATCH that retrofits it onto a pre-existing container.

```
Clerk sign-up
   └─ frontend polls GET /container/status every ~3s
        └─ routers/container.py dispatches ensure_user_ready(auth)
             └─ core/services/user_provision.py
                  1. evaluate_provision_gate — subscription/credits/OAuth pre-checks
                  2. _ensure_paperclip_for_owner (SYNC, ~2-3s)
                       └─ paperclip_provisioning.provision_org
                            ├─ sign up Paperclip user (Better Auth)
                            ├─ create Paperclip company; bootstrap-admin invite/accept/approve
                            ├─ write PaperclipCompany DDB row (status="active")
                            ├─ seed Friday (CEO) with adapter_type="openclaw_gateway"
                            │     adapter_config.url = wss://ws-<env>.isol8.co
                            └─ seed Atlas (recruiter) — best-effort; Atlas failure does NOT undo Friday
                  3. asyncio.create_task(dispatch_provision(owner_id))
                       └─ ecs_manager.provision_user_container
                            ├─ per-user EFS access point + ECS service
                            └─ write_user_configs → write_openclaw_config(paperclip_agents=[…])
                                  └─ openclaw.json on EFS lists Friday/Atlas agent_ids in agents.list[]
```

Key invariants:

- **`GET /container/status` is the only caller of `ensure_user_ready`** (see `user_provision.py` module docstring). Clerk webhooks are pure record-keeping; they do not trigger provisioning.
- **Paperclip URL is NOT in `openclaw.json`** — the Paperclip↔OpenClaw binding is on the Paperclip seed-agent's `adapterConfig.url`. The container connects to Paperclip via the agent's adapter at runtime; the container's own config only knows the agent IDs and workspace paths.
- **`provision_org` is idempotent** — short-circuits on existing active rows. Member adds and duplicate signups return 409, which the caller treats as success (see `_ensure_paperclip_for_owner`).

### Legacy containers (pre-Paperclip provisioning)

Owners whose container was provisioned **before** Paperclip integration shipped have a container row but no `PaperclipCompany` row, and an `openclaw.json` that lists no agents. They get 409s from `/teams` and have no Friday/Atlas to chat with.

Two fixes:

1. **Full reprovision** — `DELETE /api/v1/debug/provision` then `POST /api/v1/debug/provision`. The new poll cycle invokes `ensure_user_ready` against an empty slate.
2. **Backfill script** — a one-shot `backfill_paperclip_for_legacy_users.py` (already run against prod, since deleted) paginated the `containers` table, found owners with no Paperclip row, and called `provision_org` for each. Recover it from git history only if a new legacy cohort appears.

**`PATCH /debug/provision` cannot wire Paperclip.** It only deep-merges `openclaw.json`. It cannot create a Paperclip company, cannot sign up a Paperclip user, cannot seed Friday/Atlas. The `config_patcher` is for config drift, not for provisioning gaps.

---

## Billing System

### Plan model

Post-2026-04-26 flat-fee cutover. Three paid shapes, each its own Stripe price. This section used to say "a single $50/mo subscription per owner (personal user OR org)" — that is wrong for orgs, which pay a base **plus** per-seat:

| shape | Stripe price(s) | amount | who |
|---|---|---|---|
| **Flat** | `STRIPE_FLAT_PRICE_ID` | $50/mo | personal owners |
| **Teams** | `STRIPE_TEAM_BASE_PRICE_ID` **+** `STRIPE_TEAM_SEAT_PRICE_ID` | `TEAM_BASE_PRICE_USD` $50 base **+** `TEAM_SEAT_PRICE_USD` $20/seat | orgs |
| **Ultra Pro** | `STRIPE_ULTRA_PRICE_ID` | `ULTRA_PRICE_USD` = $150/mo | large marketplace companies over the flat tier's agent budget |

**A Teams subscription is TWO line items, not one.** `create_team_seat_checkout` builds `[{base, quantity: 1}, {seat, quantity: seat_count}]`, so a 4-person org pays `$50 + 4 × $20 = $130/mo`. Both amounts are echoed to the frontend by `/billing/account` (`team_base_price_usd`, `team_seat_price_usd`) so `TeamBillingCard` renders "$50 + N × $20" without hardcoding either number.

Two constraints in that code worth not breaking:
- **Nothing may select the line items by position** — Stripe does not guarantee order. The webhook picks the seat item by price id (`subscription_item_on_price`), and `create_team_seat_checkout` refuses to start if the base and seat price ids are equal.
- **Seat quantity has a single writer**, `sync_team_seat_quantity`, driven off Clerk membership webhooks. Quantity is floored at 1 so a transient member-count read of 0 never creates a zero-quantity subscription. Pending invitations count as seats from the moment they're sent.

Every container is the same 1 vCPU / 4 GB box launched on **Fargate Spot** (~70% cheaper than on-demand; `ecs_manager` falls back to on-demand when Spot capacity is unavailable — at service create and via a zero-placement timeout in the provisioning poller). There is no longer a tier-based size ladder. Paid containers stay always-on while the subscription is active. Checkout bills the first month immediately — the 14-day Stripe trial was removed 2026-07-17 (evaluation happens on the no-card free tier below; the `/billing/trial-checkout` route path and `is_trial_blocked` names were kept for compatibility). Legacy `trialing` rows created before the cutover remain valid until they convert; for those, trial-clock state is owned by Stripe (do not compute trial-days-left from `trial_end` locally).

### Free tier (no card) — `trial_local`

A first-time personal signup with no billing row gets a `trial_local` row and a container **provisioned exactly like a paid Bedrock one, except with a SINGLE per-owner application-inference profile (the `sonnet` tier)**. The AIP is the attribution + pricing unit; model pinning is enforced by the **bedrock-gate** (a request may only name an AIP in the owner's own token row). `bedrock_trial` is a **config-path mode**, never a stored `provider_choice`.

**The enforcement model — read this before changing anything here.** Every Bedrock inference call flows through the **bedrock-gate** (`apps/infra/lambda/bedrock-gate/`, a streaming Lambda fronting bedrock-runtime). Containers authenticate to it with per-owner bearer tokens (`AWS_BEARER_TOKEN_BEDROCK`; hash-stored in `isol8-{env}-bedrock-gate-tokens`); **owner task roles carry NO `bedrock:Invoke`** (`build_owner_bedrock_policy` is a deny-only fence), so the `exec: full` shell path AccessDenies — the gate is the only door. The gate discovers its own URL via SSM (`/isol8/{env}/bedrock-gate-url`, read by `core/billing/bedrock_gate_url.py`; the literal value `direct` is the fleet break-glass mode, paired with `scripts/bedrock_gate_break_glass.py` which re-grants direct IAM — see `docs/runbooks/bedrock-gate-break-glass.md`).

- **The valve (primary)** is the gate's pre-flight **reservation**: admit only if `spent + holds + ~$0.30 estimate < $3.00`, place the hold, forward verbatim (byte-identical eventstream — load-bearing for Claude Code), settle Bedrock's own reported usage on completion. Over budget → 429 before a single token is generated. Overshoot is bounded by in-flight requests (cents), so the cap is a TRUE **$3.00** — no enforce-early margin (all three lockstep `CAP_MICROCENTS` copies: `trial_cap.py`, meter `index.py`, gate stack env — NEVER move one alone).
- **The auditor + backstop** is the invocation-log **meter Lambda** (`apps/infra/lambda/bedrock-usage-meter/`). It sees EVERY call from AWS's own records — including aborted/crashed streams the gate cannot settle and any break-glass direct traffic — and reconciles via the shared `requestId` dedup table (gate = cashier writes first; meter counts only what the gate missed, emitting `meter.reconciled_gateway_miss`). It remains the **sole Metronome writer** (paid billing never touches the gate). If the counter crosses the cap anyway it fires the backstop kill: deny the gate token + `desiredCount=0` (+ the now-vestigial IAM revoke, which still bites in break-glass direct mode).
- **The gates** (`gate_chat`, the Teams guards, `ecs_manager.start_user_service`) are read-only checks on that counter. **They are UX** — the limit card instead of a failed run. The historical "no pre-send reserve" rule applies to the BACKEND (off the inference path, where a reserve is unenforceable guesswork — one existed, caused a 15-defect review, and was deleted); the gate's reservation is different in kind: it lives on the only path in/out and settles against its own atomic counter.
- **The container config bounds** (`agents.defaults.timeoutSeconds`, `runRetries.max`, `subagents.maxConcurrent`, `tools.loopDetection`) shrink the blast radius of a runaway loop. They are **insurance, not the fence**. Note `timeoutSeconds` defaults to **48 HOURS** upstream and the agentic loop has no step/tool-call/token cap at all.

Trial containers scale to zero when idle and wake on the next `/container/status` poll (cap-gated). Trial owners are never ingested to Metronome — they have no Stripe customer, and Metronome is a ledger, not a valve (its threshold alerts poll on a ≥3-minute tick and it never blocks usage); paid credit balances are gated per-request by the gate against the `metronome-fast-gate` row.

**Load-bearing constraints (verified 2026-07-15/16; do not re-litigate without new facts):** OpenClaw sends **no `maxTokens`** on the adaptive path — a small injected cap silently truncates thinking, so the gate clamps only ABOVE model-max (128K Sonnet/Opus, tamper ceiling). Bedrock invocation-log **text delivery cannot be disabled** without blinding the meter entirely (the modality flags gate whole records; bodies — which have leaked plaintext Paperclip keys/JWTs — are bounded by 3-day log retention + DLQ redaction instead). A streaming Lambda **cannot be a fence by itself** (not reliably notified of client aborts; can't guarantee its own final write) — which is why the meter backstop + deny-flag kill stay, per the staff-review + AWS-docs research in `docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`.

### Paid provider paths

Three provider paths the user / org chooses between (stored on the per-owner `billing_accounts` record, not per-user):
1. **Bedrock managed** — Isol8-billed Bedrock inference, draws from per-owner Bedrock credits.
2. **ChatGPT OAuth** — available to personal owners AND org workspaces (see `routers/oauth.py` module docstring; the org admin who completes the device-code flow is the recorded `oauth_connector_user_id`, and only they can manage/re-select it — one ChatGPT account ↔ one container); user signs in to their own OpenAI account.
3. **BYO API key** — owner provides their own Anthropic / OpenAI key, encrypted via Fernet in `api_keys` table.

The provision gate (`core/services/provision_gate.py`) blocks chat when subscription is not active, OAuth tokens are missing, or Bedrock credits are exhausted. The frontend renders a structured "blocked" state instead of an indefinite spinner.

### Usage Flow

1. Agent chat completes -> `chat.final` event -> `sessions.list` RPC queries token counts
2. `record_usage()` writes to DynamoDB atomic counters (owner + per-member)
3. Budget check before each chat message (`check_budget()`)
4. Overage reported to Stripe Meters API when over included budget

### Container Update System

- **Track 1 (silent):** Config patches via EFS deep merge + OpenClaw file watcher (chokidar polling). Deep merge preserves OpenClaw's runtime additions (`meta`, `commands`, `identity`).
- **Track 2 (notification):** Image/resize updates queued in DynamoDB, user picks when via banner.
- **Admin endpoints:** `PATCH /container/config/{owner_id}`, `PATCH /container/config` (fleet)

### Config Patching Notes

- `CHOKIDAR_USEPOLLING=true` env var required for EFS NFS compatibility
- `fcntl.lockf()` for file locking (not `flock()` -- incompatible with NFS)
- `chown` to UID 1000 after write (OpenClaw runs as node)

---

## Marketplace

Sellers publish custom OpenClaw agents; buyers discover them and deploy a copy into their own container. Publishing and provisioning are **decoupled** — artifacts in S3 are immutable + versioned, so an old container deploying an old listing is safe.

### Status state machine

`draft → review → published → retired | taken_down`

- **draft** — seller's working row, artifact may or may not be uploaded yet.
- **review** — seller has submitted; awaits admin approval.
- **published** — buyer-visible. Publishing a new version atomically retires the previous published version (`marketplace_service.publish_v2` via `TransactWriteItems`).
- **retired** — no longer searchable; previously bought entitlements still resolve.
- **taken_down** — no longer searchable, and the takedown cascade REVOKES every buyer's license (`takedown_service` → `set_license_revoked`); previously bought entitlements do NOT survive a takedown.

### Seller publish pipeline

```
POST /api/v1/marketplace/listings                       (create draft row in DDB)
POST /listings/{id}/artifact-from-agent                 (snapshot EFS agent → S3)
   └─ agent_export.export_agent_from_efs
        ├─ _resolve_agent_dir — resolves workspace-{id}/, NEVER agents/{id}/
        │                       (that root is OpenClaw runtime state, not seller content)
        ├─ _build_tarball   — filters .git, __pycache__, node_modules, .DS_Store, dotfiles
        ├─ marketplace_safety.scan_tarball  — credential / PII pattern scan
        ├─ extract_agent_slice              — agent + plugins + selected MCPs/skills + cron jobs,
        │                                     scrubbed of secrets
        └─ marketplace_service._upload_artifact_to_s3
              s3://$MARKETPLACE_ARTIFACTS_BUCKET/listings/{id}/v{n}/
                ├─ workspace.tar.gz    (deterministic; mtimes zeroed)
                ├─ manifest.json       (file count, safety flags, agent_id)
                └─ openclaw-slice.json (slice to be deep-merged into buyer's openclaw.json)
POST /listings/{id}/submit                              (draft → review)
```

**Friday is unpublishable** — the `FRIDAY_PUBLISH_ERROR_MSG` guard in `agent_export.py` blocks the export at the router boundary. Publishing Friday would leak owner-keyed seeding content and hand a buyer a clone of someone else's CEO.

### Admin approval

```
GET  /api/v1/admin/marketplace/listings        (list review-state rows)
POST /admin/marketplace/listings/{id}/approve  (review → published, with version transition)
POST /admin/marketplace/listings/{id}/reject
```

### Buyer flow

```
GET  /api/v1/marketplace/listings/search?q=…   (60s in-process cache, tokenized scoring)
GET  /marketplace/listings/{slug}              (DDB row + manifest)
POST /marketplace/listings/{slug}/deploy
   ├─ entitlement check (free listing or owns purchase row)
   ├─ fetch workspace.tar.gz + openclaw-slice.json from S3
   └─ catalog_service.deploy_from_artifact
        ├─ generate new agent_id (agent_<uuid>)
        ├─ untar into EFS at workspaces/{new_agent_id}/
        ├─ deep-merge slice into buyer's openclaw.json
        └─ remap cron job IDs + agent references
```

### Paid listings

`marketplace_purchases.py` handles Stripe Checkout for paid listings + the `/webhooks/stripe-marketplace` webhook that flips the purchase row to `entitled=true`. `marketplace_payouts.py` manages seller payout accounts.

---

## Environment Variables

### Backend (.env)

**`apps/backend/core/config.py` is the source of truth** — the `Settings` class
lists the variables and their defaults. (A few are read via `os.getenv` outside
`Settings`: the `OPENCLAW_*` version overrides in `core/openclaw_version.py`.)
Only the ones with a non-obvious constraint are worth writing down here:

| Variable | Constraint |
|----------|-----------|
| `ENCRYPTION_KEY` | Fernet key for BYOK. Rotating it orphans every stored key. |
| `CLOUD_MAP_SERVICE_ARN` | Registration target — the shared Cloud Map service every per-user ECS service attaches to via `serviceRegistries`. |
| `CLOUD_MAP_NAMESPACE_NAME` + `CLOUD_MAP_SERVICE_NAME` | Discovery side — `DiscoverInstances` takes *names*, not the ARN, so both are needed on top of the ARN. Unset ⇒ `discover_ip` returns None and every container looks unreachable. |
| `STRIPE_TEAM_BASE_PRICE_ID` | Org flat base, always quantity 1. Display dollars mirrored by `TEAM_BASE_PRICE_USD` (50). **Must differ from the seat price id** or checkout refuses to start. |
| `STRIPE_TEAM_SEAT_PRICE_ID` | Org per-seat; quantity = live Clerk member count. Display dollars mirrored by `TEAM_SEAT_PRICE_USD` (20). |
| `MARKETPLACE_LISTINGS_TABLE` | Empty **disables the marketplace entirely**. |
| `AGENT_EMAIL_DOMAIN` | Empty disables agent-email minting entirely. |
| `CORS_ORIGINS` | Comma-separated. Note API Gateway CORS overrides FastAPI's on the WS path. |

Everything ECS/EFS/WS/table-name-shaped is set by CDK — don't hand-set them.

### Frontend (.env.local)

| Variable | Purpose |
|----------|---------|
| `NEXT_PUBLIC_API_URL` | Backend REST API URL **including `/api/v1`** |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key |
| `CLERK_SECRET_KEY` | Clerk secret key (server-side) |
| `GITHUB_RELEASES_TOKEN` | Fine-grained PAT (contents: read on Isol8AI/isol8) for the desktop download + auto-updater routes — the repo is private |

**API URL by environment:**
- Local: `http://localhost:8000/api/v1`
- Dev: `https://api-dev.isol8.co/api/v1`
- Staging: `https://api-staging.isol8.co/api/v1`
- Production: `https://api.isol8.co/api/v1`

---

## AWS CDK Stacks

Located in `apps/infra/lib/stacks/`. Entry point: `apps/infra/lib/app.ts`. Per-environment stage class: `isol8-stage.ts`. Stacks deliberately pass secret *names* (not `ISecret`) and KMS key *ARNs* (not objects) between stacks to avoid cross-stack KMS auto-grant cycles — see comments in `service-stack.ts`.

| Stack | Purpose |
|-------|---------|
| `network-stack.ts` | VPC (public/private subnets, NAT), ALB, NLB (for WebSocket VPC Link V1), security groups |
| `auth-stack.ts` | Secrets Manager secrets (Clerk, Stripe, encryption keys) + KMS key |
| `database-stack.ts` | Every DynamoDB table (`isol8-{env}-{suffix}`) + the marketplace artifacts S3 bucket |
| `container-stack.ts` | ECS cluster for per-user OpenClaw containers, EFS filesystem, Cloud Map namespace + the one shared Cloud Map service all per-user services register into, container task-definition scaffolding |
| `service-stack.ts` | Backend FastAPI service on ECS Fargate: task definition (Docker image from ECR), service, autoscaling, target-group wiring, DynamoDB/secrets IAM |
| `api-stack.ts` | WebSocket API Gateway, Lambda authorizer (Clerk JWT), VPC Link V1 wiring |
| `bedrock-gate-stack.ts` | The streaming Lambda fronting bedrock-runtime — the enforcement fence for all Bedrock inference. See the Billing section. |
| `bedrock-meter-stack.ts` | Invocation-log meter Lambda — the auditor + backstop kill, and the sole Metronome writer. |
| `paperclip-stack.ts` | Per-tenant Paperclip BFF — Aurora cluster, Fargate service, Cloud Map service discovery, ALB host route. Cross-stack KMS-cycle posture documented inline. See `apps/infra/paperclip/RUNBOOK.md`. |
| `ses-inbound-stack.ts` | Inbound agent email (SES → Lambda → backend). |
| `dns-stack.ts` | Route53 records + ACM certs (ALB + CloudFront) |
| `observability-stack.ts` | CloudWatch dashboards, alarms, log retention |
| `fis-stack.ts` | Fault Injection Simulator experiments. |

`apps/infra/openclaw/` is a sibling directory (not a stack) containing the extended OpenClaw Dockerfile + README. Built by `.github/workflows/build-openclaw-image.yml` and pushed to ECR `isol8/openclaw-extended`. The tag Isol8 runs is pinned in `openclaw-version.json`.

**Paperclip is a fork too — verify against the fork, not the in-repo clone.** Like OpenClaw, Paperclip has a vanilla-vs-fork split. The gitignored in-repo `paperclip/` clone tracks **vanilla upstream** (`paperclipai/paperclip`) and is a *read-only reference only*. What we actually deploy is the **Isol8 fork `Isol8AI/paperclip`**, checked out at **`~/dev/paperclip-fork`** (branch `isol8/2026.8-circuit-breaker`, released as `v2026.8-isol8.<n>`; `isol8/2026.7-circuit-breaker` is kept unmoved at the pre-rebase commit as the rollback ref). The fork carries our patches in **real source** — the openclaw-gateway protocol-v4 fix, the routine circuit breaker, and the agent-delete routines-FK fix. Its own CI builds `ghcr.io/isol8ai/paperclip` (private GHCR, pinned by **digest** in `paperclip-version.json`), which `apps/infra/paperclip-extended/Dockerfile` re-tags into ECR `isol8/paperclip-extended` (via `.github/workflows/build-paperclip-image.yml`) so per-tenant ECS tasks can pull it with execution-role creds. **When you need to confirm what the deployed Paperclip BFF actually returns (route projections, run/liveness fields, watchdog logic), grep `~/dev/paperclip-fork`, not the vanilla `paperclip/` clone** — they can diverge. Treat both as outside this repo — do not commit either into it.

---

## Desktop App

Tauri 2 app at `apps/desktop/`. Bundle ID: `co.isol8.desktop`. Product name: `Isol8`.

- Rust sidecar host at `apps/desktop/src-tauri/src/` (`lib.rs`, `main.rs`, `browser_sidecar.rs`, `exec_approvals.rs`, `node_client.rs`, `node_invoke.rs`, `tray.rs`)
- Points at `https://dev.isol8.co/chat` as the embedded web view
- Bundled binaries (`src-tauri/bin/`): `isol8-browser-service` (externalBin), per-arch `node` and `openclaw-host` copies for macOS (aarch64 + x86_64)
- Protocol handler: `isol8://` deep links (via `tauri-plugin-deep-link`)
- Single-instance lock (`tauri-plugin-single-instance`)
- Signed DMG for macOS (arm64), built by the standalone `.github/workflows/desktop-build.yml` on a self-hosted macOS runner (label `macos-desktop`) — dev + prod jobs, auto-publishing `desktop-dev-v0.4.*` / `desktop-v0.4.*` GitHub Releases consumed by the Tauri auto-updater
- Auth flow uses the backend's `/desktop-auth` device-code endpoints + the frontend's `/auth/desktop-callback` page
- Requirement driver: WebAuthn/passkey support (see `project_desktop_app_tauri` memory)
