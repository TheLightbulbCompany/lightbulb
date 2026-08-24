# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isol8 is an AI agent platform powered by [OpenClaw](https://github.com/openclaw/openclaw). Users subscribe, get a per-user ECS Fargate container running the OpenClaw Docker image, and interact with their agents via a WebSocket-based chat UI. The platform uses a Next.js 16 frontend (Vercel), FastAPI backend (ECS Fargate), Clerk for authentication, **DynamoDB** for metadata (users, containers, billing, usage counters, pending updates, channel links, admin actions, oauth tokens/state, integrations, webhook dedup, metronome fast-gate, bedrock AIP owners, agent-email, company slugs, paperclip-companies, marketplace listings/purchases/payouts/takedowns/sellers — see `database-stack.ts` for the full set; BYO LLM keys live in **Secrets Manager**, not DynamoDB), AWS Bedrock + ChatGPT OAuth + BYO API key for LLM inference, Stripe for billing (subscription + marketplace purchases; an Apple IAP rail is code-complete but disabled until `APPLE_IAP_BUNDLE_ID` is set — `routers/apple_iap.py` 503s), EFS for per-user agent workspaces, and a per-tenant Paperclip BFF (Aurora Serverless v2 Postgres) for the Teams Inbox. Infrastructure is managed via **AWS CDK** in `apps/infra/` (single source of IaC truth). The desktop app is **Tauri** (not Electron) — see `project_desktop_app_tauri` memory. There is also an **Expo iOS app** at `apps/mobile/` — see the "Mobile App" section.

Every freshly provisioned owner gets two seed agents inside their Paperclip company: **Friday** (the per-owner CEO / orchestrator) and **Atlas** (the in-house agent-recruiter). Both are seeded by `paperclip_provisioning.provision_org` — see the "Provisioning Flow" section below. Custom agents beyond Friday/Atlas can be **published to the Isol8 marketplace** for other users to deploy — see the "Marketplace" section.

The OpenClaw container image Isol8 runs is an **extended image** built from the **Isol8 OpenClaw fork** (`ghcr.io/isol8ai/agent`, a **Debian**-based image — NOT Alpine) with additional Linux skill binaries and baked plugins: the externalized upstream Slack/Discord/Bedrock plugins plus three in-repo plugins (`apps/infra/openclaw/plugins/{aws-exec-profile,isol8-marketplace,paperclip-exec-creds}`). Its Dockerfile lives at `apps/infra/openclaw/` and is published to ECR `isol8/openclaw-extended` by `.github/workflows/build-openclaw-image.yml`. The pinned base + per-env extended tags are tracked in `openclaw-version.json` at the repo root, where the fields mean distinct things:
- `upstream` — the fork base the Dockerfile `FROM`s (e.g. `ghcr.io/isol8ai/agent:2026.7.2-isol8.4`). A **private** GHCR package, pulled only by the authenticated build workflow. Keep it in lockstep with the Dockerfile `FROM` line.
- `image`/`tag`/`full` — the **legacy public** upstream image (`ghcr.io/openclaw/openclaw`), kept only as the bootstrap/unknown-env fallback in `container-stack.ts` (pulled by per-user ECS tasks with **no** registry credentials, so it must stay anonymously pullable). `tag` also feeds the `OPENCLAW_UPSTREAM_VERSION` env fallback.
- `extendedImage` + per-env `dev.tag`/`prod.tag` — the ECR extended image actually deployed; `dev.tag`/`prod.tag` are the per-environment promotion gate (flip dev, validate, then prod). They drive `meta.lastTouchedVersion` via `deployed_upstream_version()`.

Bundled agent-facing skills live at `apps/infra/openclaw/skills/` (`paperclip`, `isol8`). The `isol8` skill is the fleet's knowledge of the platform itself — when user-facing surfaces, marketplace/billing flows, or Paperclip seeding change, update its SKILL.md (anchors listed in its maintenance header). Per-owner dynamic state (plan/provider) is NOT in the skill; it lives in `~/.openclaw/isol8-facts.json`, written by `core/services/owner_facts.py`.

The Isol8 repo does not vendor OpenClaw source. The base image is built from the **Isol8 fork `Isol8AI/agent`** (a GitHub fork of [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)). The fork's `main` tracks upstream — it was realigned to `openclaw/openclaw:main`, so `git pull upstream main` merges cleanly. Isol8's patches do **not** live on `main`; they sit on `isol8/*` branches plus `v<version>-isol8.<n>` tags, which the fork's `docker-release.yml` builds and publishes to GHCR `ghcr.io/isol8ai/agent` (the carried patch set is enumerated in `openclaw-version.json`'s `notes`). The writable fork workspace is checked out at **`~/dev/agent-fork`** (origin = `Isol8AI/agent`, upstream = `openclaw/openclaw`). A stale second clone at `~/Desktop/agent` is pending delete — it sits on iCloud-synced Desktop and git operations hang there; do not use it. Treat fork checkouts as outside this repo — do not commit them into it.

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

**Database:** DynamoDB tables provisioned by CDK (`apps/infra/lib/stacks/database-stack.ts`). No relational DB on the Isol8 side — items are plain dicts, accessed via repositories in `apps/backend/core/repositories/`. (The per-tenant Paperclip BFF has its own Aurora Postgres, also provisioned in `database-stack.ts`.)

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
// PATCH -- wholesale-rewrites openclaw.json via write_user_configs + redeploys (preserves container).
// Seeds agents.list from the owner's LIVE Paperclip roster, so it CAN wire existing Friday/Atlas ids;
// it cannot CREATE the Paperclip company or agents (see Provisioning Flow).
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

The E2E journey test no longer uses a fixed account — it mints a random
`isol8-e2e-<rand>@mailsac.com` per run and deletes it in `cleanupUser`
(`apps/frontend/tests/e2e/fixtures/user.ts`), so there is no reserved address
to avoid clobbering.

For manual dev testing, create your own Clerk account (or use an existing
personal email) at https://dev.isol8.co/sign-up. The dev Clerk instance is
the same one used for prod previews, so test accounts created in dev are
real accounts -- don't use a production email.

**Signing a browser E2E in as an EXISTING dev test account without its
password** (Claude-in-Chrome / automation): mint a Clerk **sign-in token**
with the admin API and redeem it via the ticket strategy in the page — no
password/OTP typed, the same ticket mechanism `routers/desktop_auth.py`
uses. Get the dev Clerk secret from Secrets Manager
(`isol8/dev/clerk_secret_key`), find the user id via
`GET https://api.clerk.com/v1/users` (dev accounts are `+clerk_test`
addresses; the users table has no email, Clerk is the only email truth),
then:
```bash
curl -sX POST https://api.clerk.com/v1/sign_in_tokens \
  -H "Authorization: Bearer $CLERK_SECRET_KEY" -H 'Content-Type: application/json' \
  -d '{"user_id":"user_XXX"}'   # -> {token, status:"pending"} (one-time, ~mins TTL)
```
In the browser (already on a dev.isol8.co page, Clerk loaded):
```js
const si = await window.Clerk.client.signIn.create({strategy:'ticket', ticket:'<token>'});
await window.Clerk.setActive({session: si.createdSessionId});
```
Password prompts and OTP entry are off-limits; this admin-issued ticket is
the sanctioned path. NOTE the `+clerk_test` bootstrap-admin collision bug
(`admin@isol8.co` as an owner can never seed Paperclip — issue #2003); pick
a different test account for import/Teams flows.

### CI/CD Monitoring

```bash
gh run watch <run-id> --repo Isol8AI/isol8 --exit-status
gh run view <run-id> --repo Isol8AI/isol8 --json status,jobs
```

**Deployment methods:**
- **Frontend:** Vercel auto-deploy on push to main (configured via Vercel dashboard)
- **Backend:** `deploy.yml` (the CDK pipeline) builds the Docker image and deploys the ECS Fargate service. `backend.yml` is **PR lint + test only** — its header says so; it deploys nothing.
- **Infrastructure (CDK):** GitHub Actions (`deploy.yml`) → `cdk synth` → `cdk deploy` per environment
- **Extended OpenClaw image:** GitHub Actions (`build-openclaw-image.yml`) → build on Dockerfile/`openclaw-version.json` changes → push to ECR `isol8/openclaw-extended`. Promotion is MANUAL — the workflow never writes the version pins; a human lands a PR flipping `dev.tag`/`prod.tag`.
- **Desktop app:** GitHub Actions (`desktop-build.yml`) → signed DMG via Tauri. Standalone workflow (NOT part of `deploy.yml`), path-filtered on `apps/desktop/**`, runs on a **self-hosted macOS runner** (`runs-on: [self-hosted, macos-desktop]`). Version is `0.4.${{ github.run_number }}`.
- **Mobile app:** `mobile-ci.yml` (PR lint/typecheck/jest + `expo export` Metro gate, self-hosted mac), `mobile-release.yml` (`eas build --local` + `eas submit` to TestFlight, on `mobile-v*` tags), `mobile-ota.yml` (`eas update --channel production` on merge to main). See the "Mobile App" section.
- **E2E journey test:** GitHub Actions (`e2e-dev.yml`) runs the Playwright journey test against dev **on-demand only** (not on every deploy); keep regularly-needed coverage hermetic/per-PR

---

## Knowledge Graph (graphify)

`graphify-out/` (gitignored — `graph.json` is ~57MB and must never be committed) holds an
**isol8-only** knowledge graph: ~29.5k nodes / 63k edges / 1,018 communities over `apps/`,
`packages/`, `docs/`, `.github/` (excludes `.playwright-cli/`, `.codex/`, `.agents/`, images).
Deliberately NOT cross-repo: a merged isol8+OpenClaw+Paperclip graph was built and deleted
2026-08-19 — `merge-graphs` produces disjoint per-repo islands (zero cross-repo edges), and
the fork bulk (5×) buries isol8 in every ranking. Keep it isol8-only.

- Ask it questions: `graphify query "<question>"` (also `graphify path "A" "B"`, `graphify explain "X"`).
- Refresh after changes: `/graphify . --update` — code-only changes re-extract via AST, no LLM cost.
- `graphify-out/cache/` (~35MB) holds the paid ~1.2M-token semantic extraction of the repo's
  docs. Don't delete it: with cache warm, a full rebuild costs ~0 tokens and ~2 minutes.
- Structural hubs by degree (verified 2026-08-19, orientation only): backend `AuthContext` /
  `get_current_user`, `run_in_thread()`, `PaperclipAdminClient`, `TeamsContext`,
  `GatewayConnection`, `put_metric()`; frontend `components/teams/shared/types.ts`,
  `useTeamsApi()`, `cn()`.

---

## Architecture

### High-Level Data Flow

```
Chat WS:   Client --> wss://ws-{env}.isol8.co (API Gateway v2 WebSocket, Clerk JWT authorizer, ?token=)
             --> ws-connect / ws-disconnect / ws-message Lambdas (connectionId map in isol8-{env}-ws-connections)
             --> ws-message POSTs http://{ALB}/api/v1/ws/message (x-isol8-ws-internal shared secret)
             --> FastAPI --> Cloud Map DiscoverInstances --> per-user OpenClaw container (ECS Fargate)
             <-- FastAPI pushes frames via the Management API (HTTPS POST @connections/{id})

REST:      Client --> https://api-{env}.isol8.co (API Gateway HTTP API) --> VPC Link V2 --> ALB --> FastAPI
Browser:   Client --> wss://bv-{env}.isol8.co (CloudFront VpcOrigin — raw WS the HTTP API can't carry)
             --> ALB browser-view listener (path-capped /api/v1/browser-view/*) --> routers/browser_view.py
                                                    |
                                        EFS /mnt/efs/users/{user_id}/
                                        agents/{agent_uuid}/, workspace-{agent_id}/
```

There is **no NLB and no VPC Link V1** — that architecture was replaced; see "WebSocket Architecture".

### Production Infrastructure

Turborepo monorepo (`Isol8AI/isol8`): `apps/{frontend,backend,desktop,infra,mobile}`,
`packages/shared` (see the Frontend section — it is load-bearing). The non-obvious
entries at the root:

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

**Entry point:** `main.py` -- FastAPI app. The lifespan handler's real startup work is `_resume_wake_resize_transitions`, a one-time trial-idle reconcile sweep, and the running-gauges loop (`startup_containers()` is a no-op — ECS services are always-on; the gateway pool is created lazily on first `get_gateway_pool()`); shutdown is a `/health`-gated bounded ~20s graceful drain with `handoff_inflight_provisions`. Middleware worth knowing: `HostDispatcherMiddleware` rewrites requests arriving on `company[-env].isol8.co` (via the `X-Isol8-Public-Host` header set by API GW parameter mapping) onto the `/__paperclip_proxy__` sentinel mount, plus `ShutdownAware`, `SecurityHeaders`, `ProxyHeaders` (VPC-CIDR-scoped), `AdminMetrics`, and observability middleware. A global `PaperclipApiError` handler self-heals Teams-BFF 401s by re-minting the Paperclip session cookie. CORS middleware, custom OpenAPI schema with BearerAuth.

No SQLAlchemy, no ORM — DynamoDB items are plain dicts accessed via repositories.

> **File inventories are not kept here.** They rot faster than anyone updates
> them. Run `ls` on the directory you're touching. What this file documents is
> the *why* — invariants, single-writers, and things that look wrong until you
> know the reason.

### Core (`core/`)

Flat modules: `config.py` (Pydantic settings), `auth.py` (Clerk JWT via PyJWT;
`get_current_user` → `AuthContext`, JWKS cache w/ 5-minute TTL + 15-min
max-stale fail-closed), `dynamodb.py`, `encryption.py` (Fernet),
`constants.py` (`SYSTEM_ACTOR_ID = "__system__"`), `openclaw_version.py`,
`secrets_manager.py`, `tenancy_codes.py`.

Subpackages: `billing/` (Bedrock AIPs, pricing, gate URL — see the Billing
section), `containers/`, `gateway/`, `repositories/`, `services/`, `crypto/`,
`middleware/`, `observability/`.

### Repositories (`core/repositories/`)

Mostly one repo per DynamoDB table (`marketplace_repo.py` fronts five
marketplace tables); items are plain dicts. Table names follow
`isol8-{env}-{suffix}` (see `database-stack.ts`).

### Container Orchestration (`core/containers/`)

| File | Purpose |
|------|---------|
| `__init__.py` | Singletons: `get_ecs_manager()`, `get_workspace()`, `get_gateway_pool()` (lazy). `startup_containers()` is a documented no-op; `shutdown_containers()` closes the pool. |
| `ecs_manager.py` | Per-user ECS Fargate lifecycle. Container IP discovery is **Cloud Map** `DiscoverInstances` (`discover_ip`), **not** `describe_tasks`: every per-user service registers into ONE shared Cloud Map service via `serviceRegistries`, and the lookup is scoped to a tenant by filtering the `ECS_SERVICE_NAME` attribute. `HealthStatus="HEALTHY_OR_ELSE_ALL"` because the shared service has no `HealthCheckCustomConfig`, so ECS-registered instances report `UNKNOWN`. **Never cache a container IP across reconnects** — the service is long-lived but the task IP is ephemeral (changes on every deploy / OOM / health-restart / reprovision); a cached dead IP caused a prod outage where the pool showed "connected" and every RPC timed out. Per-user EFS access points. Service naming: `openclaw-{user_id}-{hash}`. |
| `config.py` | `write_openclaw_config()`: generates `openclaw.json` with Bedrock provider and memory plugin. |
| `workspace.py` | EFS file I/O at `/mnt/efs/users/{user_id}/`. |

### Gateway (`core/gateway/`)

| File | Purpose |
|------|---------|
| `connection_pool.py` | `GatewayConnectionPool`: persistent WebSocket connections to per-user OpenClaw containers. OpenClaw **signed-device** handshake (receive `connect.challenge` nonce → sign canonical payload → connect; `minProtocol: 4`), RPC proxy, event forwarding, **trial-only** idle scale-to-zero (`_grace_close`/`_maybe_stop_trial_container` — the general idle reaper was removed at the flat-fee cutover), usage recording inline. |
| `frame_sender.py` | Per-connection ordered frame queue over the Management API; shapes delta/append chunks and full-text resyncs. |
| `operator_rpc.py` | `perform_signed_connect` + `gateway_rpc_oneshot` (one-shot RPCs outside the pool). |
| `node_connection.py` | Per-user node-host connection for desktop browser-proxy / exec-approval flows. |

### Services (`core/services/`)

`ls apps/backend/core/services/` for the current list. The ones whose behaviour
you cannot guess from the filename:

- `user_provision.py` — `ensure_user_ready` is the canonical readiness entry
  point. It is dispatched from `GET /container/status` polls **and** from the
  Teams reconcile path (`routers/teams/deps.py`) and the Paperclip-proxy
  handshake (`routers/paperclip_proxy.py`). Pre-gate steps:
  `ensure_personal_trial_default` (auto `trial_local` row),
  `ensure_metronome_customer`, `evaluate_provision_gate`, and
  `_maybe_arm_nightly_ceo`. On a FRESH provision the Paperclip seed runs
  **inside the provision executor** (step 0 of `scripts/provision_runner.py`,
  single-flight under `try_claim_provision` — kills the duplicate-company
  race); `ensure_user_ready` seeds on the poll thread only as reconcile for
  already-running rows.
- `provision_dispatch.py` — `dispatch_provision`, the single provisioning
  funnel. A fresh claim starts the SFN watchdog execution FIRST, then runs
  attempt 0 as a backend child process for speed. The machine watches the
  row (watch loop in `service-stack.ts`) and takes the ECS leg over on
  error or after its tick backstop — custody never depends on a client poll.
- `paperclip_provisioning.py` — owns `provision_org`, which seeds Friday + Atlas.
- `config_patcher.py` — EFS-safe JSON deep-merge. `openclaw.json` writers take
  the persistent `.isol8-guard` and select the lock protocol recorded in
  `.isol8-protocol` (constants + `O_EXCL` acquire live in `efs_file_lock.py`);
  backend-only files use NFS-compatible `fcntl.lockf` (not `flock`).
- `clerk_admin.py` — Clerk REST wrapper for the admin surface. New Clerk calls
  **should** go through here, but two predate it and still hit `api.clerk.com`
  inline via httpx: `routers/billing.py` and `routers/desktop_auth.py`. A
  fleet-wide change to how we call Clerk has to touch all three.
- `node_proxy.py` — per-user node-host tracking. Lives in services/, not
  routers/, despite the name; `routers/websocket_chat.py` consumes it directly.
- `key_service.py` — BYO LLM keys. Stored **solely in AWS Secrets Manager**
  (`isol8/{env}/user-keys/{user}/{provider}`), referenced by the ECS task def;
  no DynamoDB copy is kept.
- `catalog_service.deploy_from_artifact` — the consumption-side entry point for
  marketplace deploys.
- `agent_export.py` — seller-agent → tarball snapshotter. It tars
  `workspace-{id}/` (the seller's actual content) and **never** `agents/{id}/`
  (OpenClaw runtime state — `sessions/` logs, `agent/` auth tokens). Runtime
  state is excluded *by which root it points at*, not by a filter list; the
  filters only drop dotfiles and junk dirs. See `_resolve_agent_dir`'s docstring.

### Routers (`routers/`) — all mounted under `/api/v1`

~44 flat modules plus the `routers/teams/` subpackage (the Teams Inbox BFF —
agents, approvals, feed, feedback, inbox, interactions, issues, runs, search,
settings, tree_control, work; composes the `paperclip_*` services). `ls` both
for the current set.

Worth knowing: `paperclip_proxy.py` is the per-tenant Paperclip proxy and has a
route-filter security model — see `docs/audit-2026-05-02-paperclip-routes.md`
before touching it. `debug.py` returns 403 in prod.

### Models (`models/`) and schemas (`schemas/`)

`models/` is intentionally empty — DynamoDB items are plain dicts, no ORM.
`schemas/` holds the Pydantic request/response models for the HTTP surface.

---

## Frontend (Next.js 16 App Router)

React 19, Tailwind CSS v4, Clerk auth, SWR, lucide-react icons.

**`packages/shared` (`@isol8/shared`) is load-bearing:** the framework-neutral
API client (`createApiClient`), the gateway WS connection machine
(`useGatewayConnection` — backoff, keepalive pings, RPC correlation, routing),
shared data hooks (`useAgents`, `useOnboardingStatus`, `useChatGPTOAuth`, …)
and protocol types are consumed verbatim by web AND mobile, with platform
specifics injected via a `PlatformAdapter`. No `next/*`, no react-native, no
DOM globals in that package.

### Pages (`src/app/`)

`find apps/frontend/src/app -name page.tsx` for the current routes. The two
that will mislead you otherwise:

- **`/chat`** is the whole app. Desktop renders the **dockable workspace shell**
  (`components/workspace/` — chat, teams, control, file-viewer, and browser
  panels); mobile renders `ChatLayout` via `MobileChatShell`. Teams and control
  are in-shell panels, **not routes** — the legacy `/teams/*` URL tree was
  removed and 404s. The **browser** panel is a singleton live view of the
  container's Chrome (one Xvfb display per container); its WS deliberately
  targets `bv[-env].isol8.co` (CloudFront) because the API Gateway HTTP API
  cannot pass WebSocket upgrades (`workspace/panels/browserViewUrl.ts`).
- **`/admin`** only resolves on allowed admin hosts (`admin.isol8.co`,
  `admin.dev.isol8.co`, `admin.localhost:3000`; overridable via
  `NEXT_PUBLIC_ADMIN_HOSTS`) and 404s everywhere else. Allowlist lives in
  `apps/frontend/src/middleware.ts`.

### Source map (`src/`)

Per-file tables drift too fast to keep here — run `ls` on the directory you're
touching. The load-bearing structure:

- `components/workspace/` — the dockable desktop shell: `panelRegistry.ts` /
  `panelComponents.ts` map panel keys → components (`PanelKind = chat | teams |
  control | file-viewer | browser`); `workspaceController.ts` +
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
  `onboarding`, `settings`, `support`, `ui`) — what their names say; `ui/` is
  shadcn/ui.
- `hooks/` — `useGateway.tsx` (the WEB wrapper around `@isol8/shared`'s
  `useGatewayConnection`: React context, idle park / presence wake, realtime
  status invalidation, Tauri IPC), `useAgentChat.ts` (chat state machine),
  `useGatewayRpc.ts` (SWR-wrapped RPC), `useTeamsApi.ts` (Teams BFF fetch),
  `useProvisioningState.ts` (stepper/blocked-state machine), plus ~27 more.
- `lib/api.ts` — `useApi()`: wraps `@isol8/shared`'s `createApiClient`
  (`get`, `post`, `put`, `patch`, `del`, `upload`, `uploadFiles`, `syncUser`).
  New data-fetching should go through this (or `useGatewayRpc` for WS RPC),
  not hand-rolled `fetch`.

The chat WS URL is derived from `NEXT_PUBLIC_API_URL` by an api→ws hostname
rewrite (`wss://ws[-env].isol8.co`; override with `NEXT_PUBLIC_WS_URL`), and
the Clerk JWT rides a `?token=` query param.

### Middleware

`src/middleware.ts` -- Clerk middleware protects `/chat(.*)`, `/onboarding`, `/settings(.*)`. An admin-host gate (`decideAdminHostRouting`) also runs on every request: `/admin` paths on non-admin hosts return 404, and admin hosts straying off `/admin` redirect back to `/admin` (with `/sign-in`/`/sign-up` exempted to avoid a redirect loop). A third gate: `PAPERCLIP_HOSTS` (`company.isol8.co`, `dev.company.isol8.co`) bypass Clerk entirely so the `next.config.ts` beforeFiles rewrite can proxy those hosts.

---

## Agent Message Flow

```
Client          API Gateway WS      FastAPI              OpenClaw (ECS Fargate)
  |                  |                  |                       |
  | WS message       |                  |                       |
  | {type:"agent_chat|  $default route  |                       |
  |  agent_id,       |  ws-message λ →  |                       |
  |  message}        |  POST /ws/message|                       |
  |----------------->|--(ALB, internal->|                       |
  |                  |   secret header) | Cloud Map discover    |
  |                  |                  | -> user container IP  |
  |                  |                  | WebSocket RPC pool    |
  |                  |                  | chat.send {message}   |
  |                  |                  |---------------------->|
  |                  |                  |                       | Bedrock (via gate)
  |                  |                  |  agent events         |
  |                  |  Management API  |<----------------------|
  | WS push frames   |  (HTTPS POST @connections)               |
  |<-----------------|<-----------------|                       |
```

**Event transformation** (`connection_pool.py` — `_transform_agent_event` +
the chat-state handling in `_handle_message`):
- `stream: "assistant"` text → `{type: "chunk", content, append: true}` (delta frames; a full-text authoritative chunk is re-sent at `final`)
- thinking deltas → `{type: "thinking"}`
- `stream: "tool"` `phase: "start"` → `tool_start`; `"update"` → `tool_progress`; `"result"` → `tool_end`, or `tool_error` when `isError`
- command output → `command_output`; plan/patch/item/acp activity → `agent_activity`
- `chat` state `"final"` → authoritative full-text `chunk`, then `{type: "done"}`, then a synthesized `run_ended`
- `chat` state `"error"` → `{type: "error", message, code}` (errorKind + budget-refusal reclassification)
- `chat` state `"aborted"` → `{type: "error", message: "Agent run was cancelled"}`

---

## WebSocket Architecture

**Why WebSocket?** API Gateway HTTP API buffers SSE responses, breaking streaming. WebSocket API Gateway supports true real-time bidirectional communication.

**Current design (the old VPC Link V1 → NLB chain is GONE):**
1. **Chat WS door:** API Gateway v2 WebSocket API; `$connect`/`$disconnect`/`$default` are **Lambda integrations** (`apps/infra/lambda/ws-*`). `ws-message` POSTs the payload to `http://{ALB}/api/v1/ws/message` inside the VPC.
2. Backend `/ws/*` endpoints require the `x-isol8-ws-internal` shared-secret header, so forged callers arriving over the public HTTP API are rejected.
3. Responses are pushed via the Management API (HTTPS POST to `@connections/{id}`), sequenced per-connection by `core/gateway/frame_sender.py`.
4. Lambda Authorizer validates the Clerk JWT (`?token=` query param); DynamoDB `isol8-{env}-ws-connections` maps connectionId → user.
5. **Raw WS door:** the browser live-view panel needs an unbuffered WS the HTTP API can't carry, so `wss://bv[-env].isol8.co` is a CloudFront distribution with a **VpcOrigin** to the ALB's browser-view listener (path-capped to `/api/v1/browser-view/*`); the Clerk JWT rides the `Sec-WebSocket-Protocol` offer.
6. REST rides the API Gateway HTTP API → VPC Link **V2** → ALB, including the `company[-env]` host dispatch (`X-Isol8-Public-Host` parameter mapping → `HostDispatcherMiddleware`).

---

## Provisioning Flow

A first-time user moves from "just signed up" to "ready to chat" through one canonical path. Understanding it matters because **Paperclip wiring is only set up at provision time** — there is no PATCH that retrofits a missing Paperclip company onto a pre-existing container.

```
Clerk sign-up
   └─ frontend polls GET /container/status every ~3s
        └─ routers/container.py dispatches ensure_user_ready(auth)
             └─ core/services/user_provision.py
                  1. ensure_personal_trial_default + ensure_metronome_customer
                  2. evaluate_provision_gate — subscription/credits/OAuth pre-checks
                  3. asyncio.create_task(dispatch_provision(owner_id))
                       └─ SFN watchdog started FIRST; attempt 0 = scripts/provision_runner.py
                            0. _seed_owner_paperclip (single-flight under try_claim_provision)
                                 └─ paperclip_provisioning.provision_org
                                      ├─ sign up Paperclip user (Better Auth)
                                      ├─ create Paperclip company; bootstrap-admin invite/accept/approve
                                      ├─ write PaperclipCompany DDB row (status="active")
                                      ├─ seed Friday (CEO) with adapter_type="openclaw_gateway"
                                      │     adapter_config.url = wss://ws-<env>.isol8.co
                                      └─ seed Atlas (role="general") — best-effort; Atlas failure does NOT undo Friday
                            1. ecs_manager.provision_user_container
                                 ├─ per-user EFS access point + ECS service
                                 └─ write_user_configs → write_openclaw_config(paperclip_agents=[…])
                                       └─ openclaw.json on EFS lists Friday/Atlas agent_ids in agents.list[]
                  (on later polls of an already-running row, ensure_user_ready reconciles the
                   Paperclip seed inline and arms Friday's nightly routine — _maybe_arm_nightly_ceo)
```

Key invariants:

- **`ensure_user_ready` is the canonical readiness entry point** — dispatched from `GET /container/status` polls, plus the Teams reconcile path and the Paperclip-proxy handshake. Clerk webhooks are pure record-keeping; they do not trigger provisioning.
- **Paperclip URL is NOT in `openclaw.json`** — the Paperclip↔OpenClaw binding is on the Paperclip seed-agent's `adapterConfig.url`. The container connects to Paperclip via the agent's adapter at runtime; the container's own config only knows the agent IDs and workspace paths.
- **`provision_org` is idempotent** — short-circuits on existing active rows. Member adds and duplicate signups return 409, which the caller treats as success.
- **Fresh-provision Paperclip seeding is single-flight inside the executor** (`provision_runner.py` step 0) — deliberately, to kill the duplicate-company race a poll-thread seed allowed.

### Legacy containers (pre-Paperclip provisioning)

Owners whose container was provisioned **before** Paperclip integration shipped have a container row but no `PaperclipCompany` row, and an `openclaw.json` that lists no agents. They get 409s from `/teams` and have no Friday/Atlas to chat with.

Two fixes:

1. **Full reprovision** — `DELETE /api/v1/debug/provision` then `POST /api/v1/debug/provision`. The new poll cycle invokes `ensure_user_ready` against an empty slate.
2. **Backfill script** — a one-shot `backfill_paperclip_for_legacy_users.py` (already run against prod, since deleted) paginated the `containers` table, found owners with no Paperclip row, and called `provision_org` for each. Recover it from git history only if a new legacy cohort appears.

**`PATCH /debug/provision` cannot CREATE Paperclip state.** It wholesale-rewrites `openclaw.json` via `write_user_configs`, seeding `agents.list` from the owner's LIVE Paperclip roster — so it can wire existing Friday/Atlas ids into config, but it cannot create a Paperclip company, sign up a Paperclip user, or seed agents. Config repair ≠ provisioning.

---

## Billing System

### Plan model

Post-2026-04-26 flat-fee cutover. Three paid shapes. Orgs pay a base **plus** per-seat:

| shape | Stripe price(s) | amount | who |
|---|---|---|---|
| **Flat** | `STRIPE_FLAT_PRICE_ID` | $50/mo (`FLAT_PRICE_USD`, echoed by `/billing/account`) | personal owners |
| **Teams** | `STRIPE_TEAM_BASE_PRICE_ID` **+** `STRIPE_TEAM_SEAT_PRICE_ID` | `TEAM_BASE_PRICE_USD` $50 base **+** `TEAM_SEAT_PRICE_USD` $20/seat | orgs |
| **Ultra Pro** | `STRIPE_ULTRA_PRICE_ID` | $150/mo for personal; for orgs it is a **base-item price swap** — $150 base + N × $20 seats (seat item untouched; fresh Ultra checkout refused for an org with no live team sub). Display dollars are frontend-only (`settings/page.tsx`); the backend has only the price id. | large marketplace companies over the flat tier's agent budget |

**A Teams subscription is TWO line items, not one.** `create_team_seat_checkout` builds `[{base, quantity: 1}, {seat, quantity: seat_count}]`, so a 4-person org pays `$50 + 4 × $20 = $130/mo`. Both amounts are echoed to the frontend by `/billing/account` (`team_base_price_usd`, `team_seat_price_usd`) so `TeamBillingCard` renders "$50 + N × $20" without hardcoding either number.

Two constraints in that code worth not breaking:
- **Nothing may select the line items by position** — Stripe does not guarantee order. The webhook picks the seat item by price id (`subscription_item_on_price`), and `create_team_seat_checkout` refuses to start if the base and seat price ids are equal.
- **Seat quantity has a single writer**, `sync_team_seat_quantity`, driven off Clerk membership webhooks. Quantity is floored at 1 so a transient member-count read of 0 never creates a zero-quantity subscription. Pending invitations count as seats from the moment they're sent.

**Apple IAP** is a code-complete second purchase rail (App Store guideline 3.1.3(b)): `routers/apple_iap.py` (link-purchase endpoints + `POST /webhooks/apple-iap` App Store Server Notifications, deduped on `notificationUUID`, StoreKit 2 JWS verified against pinned Apple root certs), `billing_repo.set_apple_subscription`, GSI `apple-original-transaction-index` on the billing table. The router 503s until `APPLE_IAP_BUNDLE_ID` is set. The mobile app drives it (`co.isol8.mobile.plan.monthly` via expo-iap → `/billing/apple/link`).

Containers default to a 1 vCPU / 4 GB box launched on **Fargate Spot** (~70% cheaper than on-demand; `ecs_manager` falls back to on-demand when Spot capacity is unavailable — at service create and via a zero-placement timeout in the provisioning poller). The old *plan*-tier size ladder is gone, but size is not uniform: `_size_for_owner` returns `ULTRA_CPU`/`ULTRA_MEMORY_MIB` (2 vCPU / 8 GB) for **either** an `ultra_pro` plan **or** a live agent count over `COMPANY_STANDARD_PLAN_MAX_AGENTS` (10) — the latter is a RELIABILITY floor, not a billing signal, added after an 11-agent prod owner crash-looped on boot 2026-08-14 (24 SQLite DBs restoring concurrently starved the single vCPU; `database is locked` is what SQLite reports when a starved process exhausts its busy timeout). Sizing is derived at every provision/reprovision, so a hand-set size not backed by plan-or-agent-count is silently undone. Paid containers stay always-on while the subscription is active. Checkout bills the first month immediately — the 14-day Stripe trial was removed 2026-07-17 (evaluation happens on the no-card free tier below; the `/billing/trial-checkout` route path and `is_trial_blocked` names were kept for compatibility). Legacy `trialing` rows created before the cutover remain valid until they convert; for those, trial-clock state is owned by Stripe (do not compute trial-days-left from `trial_end` locally).

### Free tier (no card) — `trial_local`

A first-time personal signup with no billing row gets a `trial_local` row and a container **provisioned exactly like a paid Bedrock one, except with a SINGLE per-owner application-inference profile (the `sonnet` tier)**. The AIP is the attribution + pricing unit; model pinning is enforced by the **bedrock-gate** (a request may only name an AIP in the owner's own token row). `bedrock_trial` is a **config-path mode**, never a stored `provider_choice`.

The trial is bounded on TWO axes: a **$3.00/day spend cap** and a **3-day trial window** (`trial_end` stamped locally at signup; expiry → permanent `TRIAL_EXPIRED`). Daily spend resets on UTC day rollover (`provision_gate.maybe_restore_trial_bedrock`). There is deliberately **no cap-only check primitive** — a cap-only check once woke EXPIRED trials, so every door must call the full predicate (`trial_block_reason` / `reason_for_account`, cap AND expiry).

**The enforcement model — read this before changing anything here.** Every Bedrock inference call flows through the **bedrock-gate** (`apps/infra/lambda/bedrock-gate/`, a streaming Lambda fronting bedrock-runtime). Containers authenticate to it with per-owner bearer tokens (`AWS_BEARER_TOKEN_BEDROCK`; hash-stored in `isol8-{env}-bedrock-gate-tokens`); **owner task roles carry NO `bedrock:Invoke`** (`build_owner_bedrock_policy` is a deny-only fence), so the `exec: full` shell path AccessDenies — the gate is the only door. The gate discovers its own URL via SSM (`/isol8/{env}/bedrock-gate-url`, read by `core/billing/bedrock_gate_url.py`; the literal value `direct` is the fleet break-glass mode, paired with `scripts/bedrock_gate_break_glass.py` which re-grants direct IAM — see `docs/runbooks/bedrock-gate-break-glass.md`).

- **The valve (primary)** is the gate's pre-flight **reservation**: admit only if `spent + holds + ~$0.30 estimate < $3.00`, place the hold, forward verbatim (byte-identical eventstream — load-bearing for Claude Code), settle Bedrock's own reported usage on completion. Over budget → 429 before a single token is generated. Overshoot is bounded by in-flight requests (cents), so the cap is a TRUE **$3.00/day** — no enforce-early margin. **Lockstep constants — NEVER move one alone:** `CAP_MICROCENTS` has three copies (`trial_cap.py`, meter `index.py`, gate stack env) and `RESERVE_ESTIMATE_MICROCENTS` (300_000) has two (`trial_cap.py`, gate stack env); every door predicting admission must subtract the reserve or a dead zone opens.
- **The auditor + backstop** is the invocation-log **meter Lambda** (`apps/infra/lambda/bedrock-usage-meter/`). It sees EVERY call from AWS's own records — including aborted/crashed streams the gate cannot settle and any break-glass direct traffic — and reconciles via the shared `requestId` dedup table (gate = cashier writes first; meter counts only what the gate missed, emitting `meter.reconciled_gateway_miss`). It remains the **sole Metronome writer** (paid billing never touches the gate). If the counter crosses the cap anyway it fires the backstop kill: deny the gate token + `desiredCount=0` (+ the now-vestigial IAM revoke, which still bites in break-glass direct mode).
- **The gates** (`gate_chat`, the Teams guards, `ecs_manager.start_user_service`) are read-only checks on that counter. **They are UX** — the limit card instead of a failed run. The historical "no pre-send reserve" rule applies to the BACKEND (off the inference path, where a reserve is unenforceable guesswork — one existed, caused a 15-defect review, and was deleted); the gate's reservation is different in kind: it lives on the only path in/out and settles against its own atomic counter.
- **The container config bounds** (`agents.defaults.timeoutSeconds`, `runRetries.max`, `subagents.maxConcurrent`, `tools.loopDetection`) shrink the blast radius of a runaway loop. They are **insurance, not the fence**. Note `timeoutSeconds` defaults to **48 HOURS** upstream and the agentic loop has no step/tool-call/token cap at all.

Trial containers scale to zero when idle and wake on the next `/container/status` poll (gated on the full trial predicate). The frontend stops that poll when a tab has been open/idle a while — a fresh user interaction (click) is what resumes polling and triggers the wake, so a long-open tab shows "Agents asleep" until someone clicks. Trial owners are never ingested to Metronome — they have no Stripe customer, and Metronome is a ledger, not a valve (its threshold alerts poll on a ≥3-minute tick and it never blocks usage); paid credit balances are gated per-request by the gate against the `metronome-fast-gate` row.

**Load-bearing constraints (verified 2026-07-15/16; do not re-litigate without new facts):** OpenClaw sends **no `maxTokens`** on the adaptive path — a small injected cap silently truncates thinking, so the gate clamps only ABOVE model-max (128K Sonnet/Opus, tamper ceiling). Bedrock invocation-log **text delivery cannot be disabled** without blinding the meter entirely (the modality flags gate whole records; bodies — which have leaked plaintext Paperclip keys/JWTs — are bounded by 3-day log retention + DLQ redaction instead). A streaming Lambda **cannot be a fence by itself** (not reliably notified of client aborts; can't guarantee its own final write) — which is why the meter backstop + deny-flag kill stay, per the staff-review + AWS-docs research in `docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`.

### Paid provider paths

Three provider paths the user / org chooses between (stored on the per-owner `billing_accounts` record, not per-user):
1. **Bedrock managed** (`bedrock_claude`) — Isol8-billed Bedrock inference, draws from per-owner Bedrock credits.
2. **ChatGPT OAuth** (`chatgpt_oauth`) — available to personal owners AND org workspaces (see `routers/oauth.py` module docstring; the org admin who completes the device-code flow is the recorded `oauth_connector_user_id`, and only they can manage/re-select it — one ChatGPT account ↔ one container); user signs in to their own OpenAI account.
3. **BYO API key** (`byo_key`) — owner provides their own Anthropic / OpenAI key, stored **in AWS Secrets Manager** (`isol8/{env}/user-keys/{user}/{provider}`, referenced by the task def; no DynamoDB copy — `key_service.py`). Note: `ENCRYPTION_KEY`/Fernet no longer protects BYOK; it protects ChatGPT OAuth tokens + Paperclip credentials.

The provision gate (`core/services/provision_gate.py`) blocks chat when subscription is not active, OAuth tokens are missing, or Bedrock credits are exhausted. The frontend renders a structured "blocked" state instead of an indefinite spinner.

### Usage Flow

1. Agent chat completes → `chat.final` event → `sessions.list` RPC queries token counts
2. `record_usage()` writes DynamoDB atomic counters (owner + per-member) — **analytics only**; billing flows through Metronome, whose sole writer is the meter Lambda
3. Chat gating is `gate_chat` (provision gate) + `trial_block_reason` (trial cap/expiry) + the Metronome fast-gate for paid credits — there is no `check_budget()` and no Stripe Meters usage reporting

### Container Update System

- **Track 1 (silent):** Config patches via EFS deep merge + OpenClaw file watcher (chokidar polling). Deep merge preserves OpenClaw's runtime additions (`meta`, `commands`, `identity`).
- **Track 2 (notification):** Image/resize updates queued in DynamoDB, user picks when via banner.
- **Admin endpoints:** `PATCH /container/config/{owner_id}`, `PATCH /container/config` (fleet)

### Config Patching Notes

- `CHOKIDAR_USEPOLLING=true` env var required for EFS NFS compatibility
- `openclaw.json.lock` starts as the backend's legacy persistent `fcntl`
  sidecar. Boot recovery takes the persistent `.isol8-guard`, publishes a
  resumable `.isol8-protocol` transition, removes the legacy inode, and then
  backend hot patches use OpenClaw's ephemeral `O_EXCL` protocol (constants
  live in `core/services/efs_file_lock.py`). Never unlink the sidecar outside
  boot recovery while holding that guard. After the transition, boot recovery
  also removes an ephemeral sidecar abandoned by a crashed backend or the
  stopped prior OpenClaw task.
- Backend-only EFS files use `fcntl.lockf()` (not `flock()` -- incompatible
  with NFS).
- `chown` to UID 1000 after write (OpenClaw runs as node)

---

## Marketplace

Sellers publish custom OpenClaw agents; buyers discover them and deploy a copy into their own container. Publishing and provisioning are **decoupled** — artifacts in S3 are immutable + versioned, so an old container deploying an old listing is safe.

### Status state machine

`draft → review → published → retired | taken_down`

- **draft** — seller's working row, artifact may or may not be uploaded yet.
- **review** — seller has submitted; awaits admin approval.
- **published** — buyer-visible. Publishing a new version atomically retires the previous published version (`marketplace_service.publish_new_version` via `marketplace_repo.transact_publish_flip`, a `TransactWriteItems`).
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
        ├─ _scan_tarball_for_safety_flags → marketplace_safety.scan (credential / PII patterns)
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

### Admin surface

```
GET  /api/v1/admin/marketplace/listings         (list review-state rows)
POST /admin/marketplace/listings/{id}/approve   (review → published, with version transition)
POST /admin/marketplace/listings/{id}/reject
POST /admin/marketplace/listings/{id}/takedown  (+ GET /takedowns; cascade revokes licenses)
```

Plus purchase refunds (`marketplace_purchases.py`), a republish flow for
taken-down/retired listings, and seller profiles (`marketplace_sellers.py`
+ the `marketplace-sellers` table).

### Buyer flow

```
GET  /api/v1/marketplace/listings/search?q=…       (60s in-process cache, tokenized scoring)
GET  /marketplace/listings/{listing_id}            (DDB row + manifest; slugs are NOT unique)
POST /marketplace/listings/{listing_id}/deploy
   ├─ entitlement check (free listing or owns purchase row)
   ├─ fetch workspace.tar.gz + openclaw-slice.json from S3
   └─ catalog_service.deploy_from_artifact
        ├─ generate new agent_id (agent_<uuid>)
        ├─ untar into EFS at workspace-{new_agent_id}/
        ├─ deep-merge slice into buyer's openclaw.json
        ├─ remap cron job IDs + agent references
        └─ requirements detection → `needs_config_patch` in the deploy preview
```

**Agent-driven hires ride an approval rail:** `GET /marketplace/u/{owner_id}/recommend` (container-authed) files a pending `hire_agent` approval via `marketplace_hire.create_hire_approval`; the owner approving it in Teams triggers `marketplace_hire.deploy_for_approval`.

### Paid listings

`marketplace_purchases.py` handles Stripe Checkout for paid listings + the `/webhooks/stripe-marketplace` webhook that flips the purchase row to `entitled=true` (the webhook serves TWO Stripe registrations — platform + Connect — with two signing secrets). `marketplace_payouts.py` manages seller payout accounts.

---

## Environment Variables

### Backend (.env)

**`apps/backend/core/config.py` is the source of truth** — the `Settings` class
lists the variables and their defaults. (Beware: quite a few modules also read
`os.getenv` directly outside `Settings` — the `OPENCLAW_*` version overrides,
`workspace.py`, `billing_service.py`, `provision_dispatch.py`, and others.)
Only the ones with a non-obvious constraint are worth writing down here:

| Variable | Constraint |
|----------|-----------|
| `ENCRYPTION_KEY` | Fernet key protecting ChatGPT OAuth tokens + Paperclip credentials (NOT BYOK — those live in Secrets Manager). Rotating it orphans every stored token. |
| `CLOUD_MAP_SERVICE_ARN` | Registration target — the shared Cloud Map service every per-user ECS service attaches to via `serviceRegistries`. |
| `CLOUD_MAP_NAMESPACE_NAME` + `CLOUD_MAP_SERVICE_NAME` | Discovery side — `DiscoverInstances` takes *names*, not the ARN, so both are needed on top of the ARN. Unset ⇒ `discover_ip` returns None and every container looks unreachable. |
| `STRIPE_TEAM_BASE_PRICE_ID` | Org flat base, always quantity 1. Display dollars mirrored by `TEAM_BASE_PRICE_USD` (50). **Must differ from the seat price id** or checkout refuses to start. |
| `STRIPE_TEAM_SEAT_PRICE_ID` | Org per-seat; quantity = live Clerk member count. Display dollars mirrored by `TEAM_SEAT_PRICE_USD` (20). |
| `MARKETPLACE_LISTINGS_TABLE` | Must be set — there is NO disable gate; marketplace routers mount unconditionally and an empty value means runtime errors, not a designed off-switch. |
| `AGENT_EMAIL_DOMAIN` | Empty disables agent-email minting entirely. |
| `CORS_ORIGINS` | Comma-separated. Note the API Gateway HTTP API handles CORS preflight at the gateway on the REST path — its allow/expose headers must match FastAPI's. |

Everything ECS/EFS/WS/table-name-shaped is set by CDK — don't hand-set them.

### Frontend (.env.local)

| Variable | Purpose |
|----------|---------|
| `NEXT_PUBLIC_API_URL` | Backend REST API URL **including `/api/v1`** (the chat WS URL is derived from it; `NEXT_PUBLIC_WS_URL` overrides) |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key |
| `CLERK_SECRET_KEY` | Clerk secret key (server-side) |
| `GITHUB_RELEASES_TOKEN` | Fine-grained PAT (contents: read on Isol8AI/isol8) for the desktop download + auto-updater routes — the repo is private |

**API URL by environment** (there is no staging environment — `app.ts` creates dev + prod only):
- Local: `http://localhost:8000/api/v1`
- Dev: `https://api-dev.isol8.co/api/v1`
- Production: `https://api.isol8.co/api/v1`

---

## AWS CDK Stacks

Located in `apps/infra/lib/stacks/`. Entry point: `apps/infra/lib/app.ts`. Per-environment stage class: `isol8-stage.ts`. Stacks deliberately pass secret *names* (not `ISecret`) and KMS key *ARNs* (not objects) between stacks to avoid cross-stack KMS auto-grant cycles — see comments in `service-stack.ts`.

| Stack | Purpose |
|-------|---------|
| `network-stack.ts` | VPC (public/private subnets, NAT), ALB, security groups. (No NLB — the old WS chain is gone.) |
| `auth-stack.ts` | Secrets Manager secrets (Clerk, Stripe, encryption keys) + KMS key |
| `database-stack.ts` | The core DynamoDB tables (`isol8-{env}-{suffix}`) **and the Paperclip Aurora Serverless v2 cluster** (Postgres, 0–4 ACU, Data API). Three tables live elsewhere: `ws-connections` (api-stack), `bedrock-gate-tokens` (gate stack), `bedrock-invocation-dedup` (meter stack). |
| `container-stack.ts` | ECS cluster for per-user OpenClaw containers, EFS filesystem, Cloud Map namespace + the one shared Cloud Map service all per-user services register into, container task-definition scaffolding |
| `service-stack.ts` | Backend FastAPI service on ECS Fargate: task definition (Docker image from ECR), service, target-group wiring, DynamoDB/secrets IAM, the SFN provision watchdog, and the marketplace artifacts S3 bucket. **NOT autoscaled** — `desiredCount: 1` in both envs, with no scalable target, and that is deliberate: per-owner container WebSockets, `_pending_rpcs`, `_active_runs` and several other maps live in-process, so a second instance would not simply share load (see #1196). Note rolling deploys run at `maxHealthyPercent: 200`, so N is transiently 2 on every deploy — which is why the provisioning/bootstrap claim-leases exist. |
| `api-stack.ts` | Both front doors: the HTTP API (REST) with VPC Link **V2** → ALB + custom-domain host dispatch (`X-Isol8-Public-Host`) + Route53 records, AND the WebSocket API with **Lambda integrations** (`ws-connect`/`ws-disconnect`/`ws-message`), the Clerk-JWT Lambda authorizer, the `ws-connections` table, and the `bv-` CloudFront VpcOrigin distribution for raw WS. |
| `bedrock-gate-stack.ts` | The streaming Lambda fronting bedrock-runtime — the enforcement fence for all Bedrock inference (+ the gate-tokens table). See the Billing section. |
| `bedrock-meter-stack.ts` | Invocation-log meter Lambda — the auditor + backstop kill, and the sole Metronome writer (+ the invocation-dedup table). |
| `paperclip-stack.ts` | Per-tenant Paperclip BFF — Fargate service, Cloud Map service discovery, ALB host route (the Aurora cluster comes from database-stack as a prop). Cross-stack KMS-cycle posture documented inline. See `apps/infra/paperclip/RUNBOOK.md`. |
| `ses-inbound-stack.ts` | Inbound agent email (SES → Lambda → backend). |
| `dns-stack.ts` | HostedZone lookup + ONE wildcard ACM cert (`*.isol8.co`). Route53 records live with their consumers (mostly api-stack). |
| `observability-stack.ts` | CloudWatch dashboards, alarms, log retention |

`apps/infra/openclaw/` is a sibling directory (not a stack) containing the extended OpenClaw Dockerfile + README. Built by `.github/workflows/build-openclaw-image.yml` and pushed to ECR `isol8/openclaw-extended`. The tag Isol8 runs is pinned in `openclaw-version.json`.

**Paperclip is a fork too — verify against the fork, not the in-repo clone.** Like OpenClaw, Paperclip has a vanilla-vs-fork split. The gitignored in-repo `paperclip/` clone tracks **vanilla upstream** (`paperclipai/paperclip`) and is a *read-only reference only*. What we actually deploy is the **Isol8 fork `Isol8AI/paperclip`**, checked out at **`~/dev/paperclip-fork`**. Releases are cut from **tags** (`v2026.813-isol8.<n>`, built from branch `isol8/2026.813-circuit-breaker`; current pin v2026.813-isol8.3); rollback refs are `isol8/2026.8-circuit-breaker` + tag `v2026.8-isol8.6`. The fork carries ~15 patches in **real source** (circuit breaker, agent-delete FK sweep, webhook auth hardening, self-healing gateway runs, `PAPERCLIP_SKIP_BUILTIN_AGENTS`, …) — `paperclip-version.json`'s `notes` field is the authoritative enumeration. Its own CI builds `ghcr.io/isol8ai/paperclip` (private GHCR, pinned by **digest** in `paperclip-version.json`), which `apps/infra/paperclip-extended/Dockerfile` re-tags into ECR `isol8/paperclip-extended` (via `.github/workflows/build-paperclip-image.yml`) so per-tenant ECS tasks can pull it with execution-role creds. **When you need to confirm what the deployed Paperclip BFF actually returns (route projections, run/liveness fields, watchdog logic), grep `~/dev/paperclip-fork`, not the vanilla `paperclip/` clone** — they can diverge. Treat both as outside this repo — do not commit either into it.

---

## Desktop App

Tauri 2 app at `apps/desktop/`. Bundle ID: `co.isol8.desktop`. Product name: `Isol8`.

- Rust host at `apps/desktop/src-tauri/src/`: `lib.rs`, `main.rs`, `bootstrap.rs`, `device_token.rs`, `file_access.rs`, `keychain.rs`, `node_client.rs`, `node_invoke.rs`, `pair_client.rs`, `pair_state.rs`, `proxy.rs`, `screen.rs`, `subprocess.rs`, `tray.rs`
- Embedded web view: dev flavor points at `https://dev.isol8.co/chat`; the prod job **patches the URL to `https://isol8.co/chat`** (and the updater endpoint) at build time in `desktop-build.yml`
- Sidecar binaries (`isol8-browser-service` externalBin, per-arch `node` and `openclaw-host`) are NOT checked in — `scripts/vendor-sidecars.sh` populates `src-tauri/bin/` at build (supports aarch64 + x86_64; CI builds aarch64 only)
- Protocol handler: `isol8://` deep links (via `tauri-plugin-deep-link`); single-instance lock (`tauri-plugin-single-instance`)
- Signed DMG for macOS (arm64), built by the standalone `.github/workflows/desktop-build.yml` on a self-hosted macOS runner (label `macos-desktop`) — dev + prod jobs, auto-publishing `desktop-dev-v0.4.*` / `desktop-v0.4.*` GitHub Releases consumed by the Tauri auto-updater
- Auth: `POST /api/v1/auth/desktop/sign-in-token` (Clerk sign-in-token bridge, `routers/desktop_auth.py`) + the frontend's `/auth/desktop-callback` page; device pairing via `POST /api/v1/desktop/pair` / `DELETE /desktop/devices/{id}` (`routers/desktop_pair.py`). There is no device-code flow.
- Requirement driver: WebAuthn/passkey support (see `project_desktop_app_tauri` memory)

---

## Mobile App

Expo iOS app at `apps/mobile/` (`@isol8/mobile`): Expo SDK 57 / React Native 0.86, expo-router file-based routing. Bundle id `co.isol8.mobile`, scheme `isol8`, `applinks:isol8.co`.

- **Reuses the web protocol via `@isol8/shared`** — the same `useGatewayConnection` WS machine and `createApiClient`, with platform specifics in `src/lib/adapter.ts` (`PlatformAdapter`). Env via `EXPO_PUBLIC_API_URL` / `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY`, baked per `eas.json` profile (dev → api-dev, production → api.isol8.co).
- Routes: `(auth)/sign-in`; `(app)/chat`, `agent/[agentId]`, `settings`, `teams/*`, and a control tree mirroring the web panels (overview, agents, channels, integrations, llm, marketplace, sessions, skills, usage).
- Auth: Clerk Expo + `expo-apple-authentication`, token cache in `expo-secure-store`.
- Push: `PushRegistration.tsx` gets an Expo push token → `POST /devices {token, platform, deviceId}`; unregister `DELETE /devices/{deviceId}`.
- **Apple IAP** via expo-iap, product `co.isol8.mobile.plan.monthly`; `src/lib/appleIap.ts` posts the signed transaction to `/billing/apple/link`, falling back to `/billing/apple/web-checkout`.
- CI/release: `mobile-ci.yml` (PR: shared+mobile typecheck/lint/jest + `expo export` as the Metro-resolution gate, self-hosted mac); `mobile-release.yml` (`mobile-v*` tag → `eas build --local` → `eas submit` to TestFlight); `mobile-ota.yml` (merge to main → `eas update --channel production`). `runtimeVersion.policy: "fingerprint"` guarantees an OTA whose native fingerprint doesn't match the installed binary is never delivered.
- **Gotcha:** Metro inlines `EXPO_PUBLIC_*` at bundle time and `eas update` does NOT read eas.json build env — a no-env export once shipped an OTA with an empty Clerk key (see the comment block in `mobile-ota.yml`).
