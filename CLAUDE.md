# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isol8 is an AI agent platform powered by [OpenClaw](https://github.com/openclaw/openclaw). Users subscribe, get a per-user ECS Fargate container running the OpenClaw Docker image, and interact with their agents via a WebSocket-based chat UI. The platform uses a Next.js 16 frontend (Vercel), FastAPI backend (ECS Fargate), Clerk for authentication, **DynamoDB** for metadata (users, containers, billing, api-keys, usage counters, pending updates, channel links, admin actions, plus the Teams BFF tables), AWS Bedrock + ChatGPT OAuth + BYO API key for LLM inference, Stripe for billing, EFS for per-user agent workspaces, and a per-tenant Paperclip BFF for the Teams Inbox. Infrastructure is managed via **AWS CDK** in `apps/infra/` (single source of IaC truth). The desktop app is **Tauri** (not Electron) — see `project_desktop_app_tauri` memory.

The OpenClaw container image Isol8 runs is an **extended image** built from `alpine/openclaw:<upstream>` with additional Linux skill binaries. Its Dockerfile lives at `apps/infra/openclaw/` and is published to ECR `isol8/openclaw-extended` by `.github/workflows/build-openclaw-image.yml`. The pinned upstream + per-env extended tags are tracked in `openclaw-version.json` at the repo root.

The Isol8 repo does not vendor OpenClaw source — upstream lives at [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw). For reading upstream code without a fresh clone, a local checkout sits alongside this repo at `~/Desktop/openclaw` (sibling of `~/Desktop/isol8.nosync`). Treat it as read-only reference — do not edit it from Isol8 work, and do not commit it into this repo.

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

### Monorepo Setup

This is a **Turborepo monorepo** using **pnpm** for JS and **uv** for Python.

```bash
pnpm install                          # All JS deps (from repo root)
cd apps/backend && uv sync            # Python deps
```

### Individual Services

**Database:** DynamoDB tables provisioned by CDK (`apps/infra/lib/stacks/database-stack.ts`). No relational DB — items are plain dicts, accessed via repositories in `apps/backend/core/repositories/`.

**All services (via Turborepo):**
```bash
turbo run dev                         # Start all services in parallel
turbo run dev --filter=@isol8/frontend    # Frontend only
turbo run dev --filter=@isol8/backend     # Backend only
```

**Backend:**
```bash
cd apps/backend
uv run uvicorn main:app --reload --port 8000
```

**Frontend:**
```bash
cd apps/frontend
pnpm run dev                   # localhost:3000
pnpm run build                 # Production build
pnpm run lint                  # ESLint
```

### Local Development with LocalStack

Full-stack local development using LocalStack to emulate AWS services and Ollama for local LLM inference. Deploys the real CDK infrastructure (same as dev/prod) to LocalStack via `cdklocal`.

**First-time setup:**
```bash
# 1. Install LocalStack CLI
brew install localstack/tap/localstack-cli

# 2. Install cdklocal (CDK wrapper for LocalStack)
npm install -g aws-cdk-local aws-cdk

# 3. Set your LocalStack auth token (from https://app.localstack.cloud)
localstack auth set-token <your-token>
export LOCALSTACK_AUTH_TOKEN=<your-token>

# 4. Set Clerk dev keys
export CLERK_ISSUER=https://up-moth-55.clerk.accounts.dev
export CLERK_SECRET_KEY=sk_test_...
export NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...

# 5. Set AWS credentials for LocalStack
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
```

**Running:**
```bash
# Start everything (LocalStack + CDK deploy + Ollama + Backend + Frontend)
./scripts/local-dev.sh

# Reset all data and start fresh
./scripts/local-dev.sh --reset

# Deploy infrastructure only (no app services)
./scripts/local-dev.sh --seed-only

# Stop everything
./scripts/local-dev.sh --stop
```

**What the script does:**
1. Starts LocalStack Pro + Ollama containers via Docker Compose
2. Bootstraps CDK for LocalStack (`cdklocal bootstrap aws://000000000000/us-east-1`)
3. Deploys all 6 CDK stacks (`cdklocal deploy "local/*"`) — auth, network, database, container, api, service
4. Extracts stack outputs to `localstack/generated.env`
5. Pulls Ollama model (qwen2.5:14b, first time only)
6. Starts backend in Docker (on same network as LocalStack containers)
7. Starts frontend on host

**Prerequisites:** Docker Desktop, `LOCALSTACK_AUTH_TOKEN`, `CLERK_ISSUER`, `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `pnpm`, `uv`, `cdklocal`

**Architecture:** Backend runs in Docker (not on host) for container IP reachability with LocalStack-launched OpenClaw containers. Source code is bind-mounted for hot-reload. Frontend runs on host. Ollama provides local LLM inference via native OpenClaw provider. CDK stacks are the single source of truth — same infrastructure as dev/prod.

**Services emulated:** ECS, EFS, Cloud Map, DynamoDB, S3, Secrets Manager, KMS, API Gateway V2 (WebSocket), Lambda, VPC, ALB, IAM, CloudFormation

**Not emulated:** Clerk (real dev keys), Stripe (test mode keys), Bedrock (replaced by Ollama)

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

Full wipe of dev state for fresh testing. Requires AWS SSO (`aws sso login --profile isol8-admin`).

**1. Delete ECS containers:** User deletes their OpenClaw ECS services + EFS access points manually from the AWS console (or via `DELETE /debug/provision` per user above).

**2. Wipe EFS user workspaces** (via ECS exec on the backend task):
```bash
# Find the backend task
TASK=$(aws ecs list-tasks --cluster isol8-dev-container-ClusterEB0386A7-Cjwm2mIlW4Aw \
  --service-name isol8-dev-service-ServiceD69D759B-Va1bdS6qTw9Y \
  --profile isol8-admin --region us-east-1 \
  --query 'taskArns[0]' --output text | awk -F'/' '{print $NF}')

# List then wipe all user dirs
aws ecs execute-command --cluster isol8-dev-container-ClusterEB0386A7-Cjwm2mIlW4Aw \
  --task $TASK --container backend --interactive \
  --command "/bin/sh -c 'ls /mnt/efs/users/ && rm -rf /mnt/efs/users/* && ls -la /mnt/efs/users/'" \
  --profile isol8-admin --region us-east-1
```

**3. Wipe DynamoDB tables** (all 8 isol8-dev-* tables):
```bash
for t in isol8-dev-api-keys isol8-dev-billing-accounts isol8-dev-channel-links \
         isol8-dev-containers isol8-dev-pending-updates isol8-dev-usage-counters \
         isol8-dev-users isol8-dev-ws-connections; do
  keys=$(aws dynamodb describe-table --table-name "$t" --profile isol8-admin \
    --region us-east-1 --query 'Table.KeySchema[].AttributeName' --output text)
  count=0
  while read -r line; do
    [ -z "$line" ] && continue
    aws dynamodb delete-item --table-name "$t" --key "$line" \
      --profile isol8-admin --region us-east-1 >/dev/null 2>&1
    count=$((count+1))
  done < <(aws dynamodb scan --table-name "$t" --profile isol8-admin \
    --region us-east-1 --output json 2>/dev/null | python3 -c "
import json, sys
keys = '$keys'.split()
d = json.load(sys.stdin)
for item in d.get('Items', []):
    print(json.dumps({k: item[k] for k in keys}))
")
  echo "  $t: $count deleted"
done
```

**4. Clean up Stripe:** Delete orphan test customers manually from the [Stripe dashboard](https://dashboard.stripe.com/test/customers) (dev uses test mode keys).

**5. Clean up Clerk:** Delete test users/orgs from the [Clerk dashboard](https://dashboard.clerk.com) if needed.

After all five steps, re-provision starts fresh: new Clerk sign-up → onboarding → container provision → single Stripe customer.

### Manual dev testing accounts

DO NOT use `isol8-e2e-testing@mailsac.com` for manual dev testing. That Clerk
account is reserved for the Playwright E2E gate that runs on every deploy.
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
- **Desktop app:** GitHub Actions (`desktop-build.yml`) → signed DMG via Tauri
- **E2E gate:** GitHub Actions (`e2e-dev.yml`) runs Playwright journey test on dev before prod promotion

---

## Architecture

### High-Level Data Flow

```
Client --> wss://ws-dev.isol8.co --> API Gateway --> Lambda Auth --> VPC Link V1 --> NLB --> ALB
  --> FastAPI /ws/message --> Cloud Map discovery (?) --> per-user OpenClaw container (ECS Fargate)
  <-- FastAPI pushes streaming chunks via Management API
                                                    |
                                        EFS /mnt/efs/users/{user_id}/
                                        agents/{agent_uuid}/
```

### Production Infrastructure

```
isol8/                       # Turborepo monorepo (Isol8AI/isol8)
+-- apps/
|   +-- frontend/            # Next.js 16 (Vercel auto-deploy)
|   +-- backend/             # FastAPI on ECS Fargate (uv managed, Docker image to ECR)
|   +-- desktop/             # Tauri desktop app (signed DMG via CI)
|   +-- infra/               # AWS CDK (TypeScript) — the single IaC source of truth
|       +-- lib/stacks/      # auth, network, database, container, api, service, paperclip, dns, observability
|       +-- openclaw/        # Extended OpenClaw Dockerfile (ECR: isol8/openclaw-extended)
+-- packages/                # Shared packages (empty/future)
+-- paperclip/               # GITIGNORED — read-only upstream Paperclip clone for greppable reference
+-- openclaw-version.json    # Pinned upstream + per-env extended image tags
+-- turbo.json               # Task orchestration
+-- pnpm-workspace.yaml      # Workspace definitions
+-- package.json             # Root (devDeps: turbo)
```

```
+---------------+     +----------------------------------------------------+
|   Vercel      |     |                      AWS                            |
|  (Frontend)   |     |                                                    |
|               |---->|  WebSocket API GW --> VPC Link V1 --> NLB --> ALB   |
|               |<----|       | Management API                              |
|               |     |       Lambda Authorizer (Clerk JWT)                 |
|               |     |       DynamoDB ws-connections table                 |
|               |     |                                                    |
|  REST API     |---->|  ALB ----------------------> ECS Fargate            |
|               |     |                                (FastAPI backend    |
|               |     |                                 service — uvicorn) |
|               |     |                                      |             |
|               |     |                          connection pool to        |
|               |     |                          per-user OpenClaw         |
|               |     |                                                    |
|               |     |  ECS Fargate (per-user OpenClaw containers)        |
|               |     |  EFS (per-user workspaces at /mnt/efs/users/)     |
|               |     |  Cloud Map (service discovery)                     |
|               |     |  DynamoDB (users, containers, billing,             |
|               |     |            api-keys, usage-counters,               |
|               |     |            pending-updates, channel-links,         |
|               |     |            admin-actions, ws-connections)          |
|               |     |  Secrets Manager + KMS                             |
|               |     |  S3 (config + catalog) + CloudFront                |
|               |     |  ECR (isol8-backend, isol8/openclaw-extended)      |
+---------------+     +----------------------------------------------------+
```

### AWS Configuration

| Setting | Value |
|---------|-------|
| Account ID | `877352799272` |
| Deploy Region | `us-east-1` |
| SSO Start URL | `https://isol8.awsapps.com/start` |
| CLI Profile | `isol8-admin` |

---

## Backend (FastAPI + DynamoDB)

**Entry point:** `main.py` -- FastAPI app with lifespan handler that boots the gateway connection pool, starts container lifecycle management (`startup_containers` / `shutdown_containers`), starts the update-service scheduled worker, boots the Teams event broker singleton, and installs observability middleware. ~25 routers registered under `/api/v1` (run `ls apps/backend/routers/` and `ls apps/backend/routers/teams/` for the current set — the per-router annotations below have a tendency to drift). CORS middleware, custom OpenAPI schema with BearerAuth.

No SQLAlchemy, no ORM — DynamoDB items are plain dicts accessed via repositories.

### Core (`core/`)

| File / Dir | Purpose |
|------------|---------|
| `config.py` | Pydantic settings: ECS/EFS/CloudMap, Stripe billing, Bedrock, CORS, plan budgets, S3 buckets, encryption key. |
| `auth.py` | Clerk JWT validation via PyJWT. `get_current_user` dependency returns `AuthContext`. JWKS cache with 1-hour TTL. |
| `dynamodb.py` | Low-level DynamoDB client + table helpers. |
| `encryption.py` | Fernet symmetric encryption helpers (used by key_service + channel links). |
| `constants.py` | `SYSTEM_ACTOR_ID = "__system__"` for audit logs. |
| `crypto/` | `kms_secrets.py` (KMS-backed secret fetch), `operator_device.py` (desktop auth device attestation). |
| `middleware/` | `admin_metrics.py` — per-request metric emission for the admin surface. |
| `observability/` | `logging.py`, `metrics.py` (CloudWatch EMF), `middleware.py`, `e2e_correlation.py` (correlation ID propagation). |

### Repositories (`core/repositories/`)

DynamoDB-backed data access. One repo per table.

| File | Table(s) |
|------|----------|
| `user_repo.py` | `users` |
| `container_repo.py` | `containers` |
| `billing_repo.py` | `billing-accounts`, usage counters |
| `api_key_repo.py` | `api-keys` (BYOK, encrypted) |
| `usage_repo.py` | `usage-counters` (atomic counters + daily rollups) |
| `update_repo.py` | `pending-updates` (container update queue) |
| `channel_link_repo.py` | `channel-links` (Telegram/Discord/WhatsApp bindings) |
| `admin_actions_repo.py` | `admin-actions` (audit log of admin ops) |

### Container Orchestration (`core/containers/`)

| File | Purpose |
|------|---------|
| `__init__.py` | Singletons: `get_ecs_manager()`, `get_workspace()`, `get_gateway_pool()`. `startup_containers()` / `shutdown_containers()` lifecycle. Usage-recording callback. |
| `ecs_manager.py` | Per-user ECS Fargate lifecycle. Cloud Map discovery. Per-user EFS access points. Service naming: `openclaw-{user_id}-{hash}`. |
| `config.py` | `write_openclaw_config()`: generates `openclaw.json` with Bedrock provider and memory plugin. `write_mcporter_config()` for MCP servers. |
| `workspace.py` | EFS file I/O at `/mnt/efs/users/{user_id}/`. |

### Gateway (`core/gateway/`)

| File | Purpose |
|------|---------|
| `connection_pool.py` | `GatewayConnectionPool`: persistent WebSocket connections to per-user OpenClaw containers. OpenClaw protocol 3.0 handshake, RPC proxy, event forwarding, idle scale-to-zero reaper, usage callback. |
| `node_connection.py` | Per-user node-host connection for desktop browser-proxy / exec-approval flows. |

### Services (`core/services/`)

The services directory has grown past per-file annotation. Run `ls apps/backend/core/services/` for the current list. Notable groups:

- **Billing / usage**: `billing_service.py` (Stripe customers, checkout, portal, subscription lifecycle), `usage_service.py` (per-owner + per-member atomic counters → Stripe Meters), `credit_ledger.py`.
- **Container lifecycle**: `update_service.py` (Track 1 silent config patches + Track 2 queued updates; `run_scheduled_worker`), `config_patcher.py` (EFS-safe JSON deep-merge under NFS-compatible `fcntl.lockf`), `config_policy.py` / `config_reconciler.py` (tier-based config protection), `provision_gate.py` (subscription / credits / OAuth gate evaluation).
- **Gateway / WebSocket**: `connection_service.py` (DDB connectionId↔user_id state), `management_api_client.py`, `node_proxy.py` (per-user node-host tracking — relocated from routers/ in May 2026).
- **Catalog**: `catalog_service.py` plus `catalog_s3_client.py` / `catalog_slice.py` / `catalog_package.py` helpers.
- **Admin surface**: `admin_service.py`, `admin_audit.py`, `admin_redact.py`, `clerk_admin.py` (Clerk REST wrapper — every Clerk API call should go through here), `cloudwatch_logs.py` / `cloudwatch_url.py`, `posthog_admin.py`.
- **Teams BFF (Paperclip)**: many `paperclip_*` services (`paperclip_admin_client.py`, `paperclip_admin_session.py`, `paperclip_user_session.py`, `paperclip_event_client.py`, `paperclip_provisioning.py`, `paperclip_autoprovision.py`, `paperclip_owner_email.py`, `paperclip_event_router.py`, `teams_event_broker_singleton.py`, etc.) coordinating with the per-tenant Paperclip BFF.
- **Auth / identity**: `oauth_service.py` (ChatGPT OAuth flow), `key_service.py` (BYOK API key encryption via Fernet), `service_token.py` (JWT minting for cross-service calls).
- **Cross-cutting**: `idempotency.py` (DDB conditional-write keyed store), `webhook_dedup.py`, `system_health.py`.

### Routers (`routers/`) — all mounted under `/api/v1`

Run `ls apps/backend/routers/` and `ls apps/backend/routers/teams/` for the current set. The flat-`routers/` files cover users, webhooks (Clerk + Stripe), websocket_chat, billing, container lifecycle (`container.py`, `container_rpc.py`, `container_recover.py`), `control_ui_proxy.py`, `channels.py`, `settings_keys.py`, `integrations.py` (MCP servers), `updates.py`, `catalog.py`, `config.py`, `workspace_files.py`, `desktop_auth.py`, `paperclip_proxy.py` (per-tenant Paperclip proxy with route-filter security model — see `docs/audit-2026-05-02-paperclip-routes.md`), `oauth.py` (ChatGPT OAuth), `admin.py`, `admin_catalog.py`, and `debug.py` (403 in prod).

The `routers/teams/` subpackage is the Teams Inbox BFF surface — agents, threads, messages, channels, members, attachments, search, etc. It composes the `paperclip_*` services and is what the `/teams/*` UI calls.

Per-user node-host tracking (`handle_node_connect`, `is_node_connection`, etc.) was relocated to `core/services/node_proxy.py` in May 2026 — it never lived behind an HTTP route despite the historical filename, and `routers/websocket_chat.py` consumes it directly.

### Models (`models/`)

Intentionally empty (just an `__init__.py` docstring: *"DynamoDB items are plain dicts, no ORM models needed."*). Use repositories instead.

### Request/response schemas (`schemas/`)

Pydantic request/response models for the HTTP surface: `billing.py`, `user_schemas.py`.

### Paperclip ↔ OpenClaw agent pairing

Paperclip is the source of truth for agent identity. Every Paperclip agent
has a UUID; that UUID is also the OpenClaw `agentId` in the user's
container — `agents.list[].id`, the workspace dir, the agent dir, and the
session-key prefix all share the same string.

Provisioning order:

1. Sign-up → `provision_org` creates the Paperclip company + seeds a
   "Friday" agent (role=ceo). Paperclip auto-generates the UUID.
2. User opens `/chat` → container provisions → `write_user_configs` calls
   `fetch_paperclip_agents_for_owner` → live agent list from Paperclip's
   REST API → written into `openclaw.json`'s `agents.list`.
3. Container boots with all known Paperclip agents pre-registered.

Forward-going creates:

- `/teams` POST `/agents`: Paperclip create_agent first, then
  `pair_with_openclaw` if the container is reachable.
- `/chat` `agents.create` RPC: backend interception calls Paperclip
  first, then forwards `agents.create` (with name=UUID) and
  `agents.update` (with friendly name) to the container.

Reprovision (`PATCH /debug/provision`) re-fetches the live Paperclip list
and rewrites `agents.list`. Stale workspaces on EFS are not GC'd by this
flow — handled separately.

**Important:** `<EFS>/users/{owner_id}/agents/main/agent/auth-profiles.json`
is the **container-wide auth-fallback location** — not tied to any agent
in `agents.list`. OpenClaw's `resolveOpenClawAgentDir` returns this path
unconditionally for ChatGPT-OAuth credential resolution. Do not relocate
this file to a paired-agent directory; doing so breaks OAuth for all
agents.

**Multi-tenant model:** the container is keyed by `owner_id` (Clerk org_id
for orgs, user_id for personal users). Org members share their owner's
container, EFS, and Paperclip company. Member-level chat isolation comes
from `routers/websocket_chat.py:881`'s `agent:<agentId>:<member_user_id>`
session-key format. Issue-driven runs use `paperclip:issue:<issue_id>`
(via `synthesize_openclaw_adapter`'s `sessionKeyStrategy="issue"`).

See `docs/architecture/2026-05-07-paperclip-openclaw-pairing.md` for the
full design rationale and the seven consistency surfaces.

---

## Frontend (Next.js 16 App Router)

React 19, Tailwind CSS v4, Clerk auth, Framer Motion, SWR, lucide-react icons.

### Pages (`src/app/`)

| Path | Purpose |
|------|---------|
| `/` | Landing page (Navbar, Hero, Features, Pricing, FAQ, Footer, GooseTown promo block) |
| `/chat` | Main app: chat + control panel (Clerk-protected) |
| `/teams` | Teams Inbox surface — agents, threads, messages, channels (Clerk-protected; consumes `routers/teams/` BFF) |
| `/onboarding` | Post-signup provisioning flow (Clerk-protected) |
| `/settings` | Settings layout (Clerk-protected) |
| `/admin` | Admin surface — accessible only via allowed admin hosts (`admin.isol8.co`, `admin-dev.isol8.co`); 404s on non-admin hosts |
| `/sign-in`, `/sign-up` | Clerk auth pages |
| `/auth/desktop-callback` | Desktop app OAuth callback |
| `/privacy`, `/terms`, `/support` | Static marketing pages |

### Components (`src/components/`)

**Chat (`chat/`):**
| Component | Purpose |
|-----------|---------|
| `ChatLayout.tsx` (+ `.css`) | Main layout: sidebar + chat/control tabs + header with UserButton |
| `AgentChatWindow.tsx` | Chat UI: connection bar, messages, input, file upload, bootstrap suggestion |
| `AgentDetailPanel.tsx`, `AgentDialogs.tsx` | Agent inspector + create/edit/delete dialogs |
| `ChatInput.tsx` | Text input + file upload + stop button + suggested message (Tab to accept) |
| `MessageList.tsx` | Message rendering: markdown, syntax highlighting, thinking blocks, tool-use indicators |
| `ApprovalCard.tsx` | Inline card for desktop exec-approval prompts |
| `FileTree.tsx`, `FileViewer.tsx`, `FileContentViewer.tsx` | Workspace file browser |
| `GallerySection.tsx`, `GalleryItemRow.tsx` | Image/asset gallery surface |
| `HealthIndicator.tsx` | Container/gateway health dot |
| `ProvisioningStepper.tsx` | Onboarding flow: billing -> container -> gateway -> channels -> ready |
| `ModelSelector.tsx` | Model dropdown |
| `ProviderIcons.tsx` | Brand marks for model providers |

**Control Panel (`control/`):**
| Component | Purpose |
|-----------|---------|
| `ControlPanelRouter.tsx` | Routes to active panel |
| `ControlSidebar.tsx` | Navigation for control panels (`NAV_ITEMS` is the live, sidebar-visible set) |
| `panels/OverviewPanel.tsx` | Dashboard: health, uptime, agents, sessions, billing, usage |
| `panels/AgentsPanel.tsx` + `AgentOverviewTab.tsx` + `AgentToolsTab.tsx` + `AgentCreateForm.tsx` + `AgentChannelsSection.tsx` | Agent CRUD + per-agent inspector |
| `panels/SessionsPanel.tsx` | Session management |
| `panels/UsagePanel.tsx` | Usage analytics |
| `panels/SkillsPanel.tsx` | Skill/tool management |
| `panels/McpServersTab.tsx` | MCP server list |
| `panels/CronPanel.tsx` (+ `cron/` subdir) | Cron job view / edit / vet |
| `panels/LLMPanel.tsx`, `panels/CreditsPanel.tsx` | Provider / model + Bedrock-credit surfaces |

> **Dark panels:** `ConfigPanel`, `LogsPanel`, `InstancesPanel`, `NodesPanel`, `DebugPanel` are wired into `ControlPanelRouter` but not linked from `ControlSidebar.NAV_ITEMS`. They are reachable only via `?panel=<name>` URL surgery and have not been actively maintained — see `docs/audit-2026-05-04/SUMMARY.md` for the keep-or-delete decision.

**Admin (`admin/`):** `AuditRow.tsx`, `CodeBlock.tsx`, `ConfirmActionDialog.tsx`, `EmptyState.tsx`, `ErrorBanner.tsx`, `LogRow.tsx`, `UserSearchInput.tsx`

**Channels (`channels/`):** `BotSetupWizard.tsx`

**Settings (`settings/`):** `MyChannelsSection.tsx`

**Landing (`landing/`):** Navbar, Hero, Features, OurAgents, Skills, Pricing, GooseTown, FAQ, Footer, ScrollManager

**Providers (`providers/`):** Global SWR/Clerk/PostHog providers. Root-level: `DesktopAuthListener.tsx`, `ErrorBoundary.tsx`, `PostHogProvider.tsx`.

**UI (`ui/`):** shadcn/ui (button, alert-dialog, dropdown-menu, checkbox, input, popover, scroll-area)

### Hooks (`src/hooks/`)

| Hook | Purpose |
|------|---------|
| `useGateway.tsx` | WebSocket provider for gateway RPC + chat. Reconnection, ping/pong, event pub-sub, RPC request/response matching. |
| `useAgentChat.ts` | Agent chat via useGateway. Message cache, bootstrap flag, tool-use tracking, cancel via `chat.abort`, thinking blocks. |
| `useGatewayRpc.ts` | SWR-wrapped RPC calls via WebSocket. `useGatewayRpc<T>()` (read) + `useGatewayRpcMutation()` (write). |
| `useAgents.ts` | Agent CRUD via RPC: `agents.list/create/delete/update`. |
| `useBilling.ts` | Billing account + checkout via REST API. |
| `useContainerStatus.ts` | Container status polling via REST. |
| `useProvisioningState.ts` | Provisioning state machine consumer for `ProvisioningStepper` / blocked-state UI. |
| `useCatalog.ts` | Catalog data fetching. |
| `useWorkspaceFiles.ts` | Workspace file tree / content. |
| `useDesktopAuth.ts` | Desktop app OAuth flow. |
| `useSystemHealth.ts` | System health surface (admin). |
| `useScrollToBottom.ts` | Auto-scroll hook. |

### Utilities (`src/lib/`)

| File | Purpose |
|------|---------|
| `api.ts` | `useApi()` hook: authenticated REST methods (`get`, `post`, `put`, `del`, `syncUser`, `uploadFiles`). |
| `utils.ts` | Tailwind `cn()` class merging. |
| `channels.ts` | Channel helpers. |
| `filePathDetection.ts` | Detect file paths in chat output for linkification. |
| `snooze.ts` | Snooze helper. |
| `tar.ts` | Tar utilities. |

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
  |                  |                  | Cloud Map discovery   |
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

## Billing System

### Plan model

Post-2026-04-26 flat-fee cutover: a single $50/mo subscription per owner (personal user OR org). Every container is the same 0.5 vCPU / 1 GB box; there is no longer a tier-based size ladder. There is no scale-to-zero path — containers stay always-on while the subscription is active. A 14-day trial is provisioned by Stripe; trial-clock state is owned by Stripe (do not compute trial-days-left from `trial_end` locally — see memory `feedback_stripe_owns_trial_clock`).

Three provider paths the user / org chooses between (stored on the per-owner `billing_accounts` record, not per-user):
1. **Bedrock managed** — Isol8-billed Bedrock inference, draws from per-owner Bedrock credits.
2. **ChatGPT OAuth** — personal owners only (org guard enforced server-side, see memory `project_chatgpt_oauth_personal_only`); user signs in to their own OpenAI account.
3. **BYO API key** — owner provides their own Anthropic / OpenAI key, encrypted via Fernet in `api_keys` table.

The provision gate (`core/services/provision_gate.py`) blocks chat when subscription is not active, OAuth tokens are missing, or Bedrock credits are exhausted. The frontend renders a structured "blocked" state instead of an indefinite spinner — see `provision-gate-ui` plan/spec.

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

## Environment Variables

### Backend (.env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLERK_ISSUER` | Clerk domain for JWT validation | Required |
| `CLERK_SECRET_KEY` | Clerk secret key | Optional |
| `CLERK_WEBHOOK_SECRET` | Clerk webhook verification | Optional |
| `AWS_REGION` | AWS region | `us-east-1` |
| `BEDROCK_ENABLED` | Enable Bedrock model discovery | `true` |
| `ENVIRONMENT` | Environment name (dev/staging/prod) | Empty |
| `ECS_CLUSTER_ARN` | ECS Fargate cluster | Required for containers |
| `ECS_TASK_DEFINITION` | ECS task definition | Required |
| `ECS_SUBNETS` | Comma-separated subnet IDs | Required |
| `ECS_SECURITY_GROUP_ID` | Security group for containers | Required |
| `CLOUD_MAP_NAMESPACE_ID` | Cloud Map namespace | Required |
| `CLOUD_MAP_SERVICE_ID` | Cloud Map service | Required |
| `EFS_MOUNT_PATH` | EFS mount root | `/mnt/efs/users` |
| `EFS_FILE_SYSTEM_ID` | EFS filesystem | Required |
| `S3_CONFIG_BUCKET` | Config bucket | Required |
| `WS_MANAGEMENT_API_URL` | Management API endpoint | Set by CDK |
| `WS_CONNECTIONS_TABLE` | DynamoDB table | `isol8-websocket-connections` |
| `STRIPE_SECRET_KEY` | Stripe API key | Empty |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook verification | Empty |
| `STRIPE_METER_ID` | Stripe usage meter ID | Empty |
| `BILLING_MARKUP` | Markup multiplier | `1.4` |
| `ENCRYPTION_KEY` | Fernet key for BYOK | Required for key management |
| `CORS_ORIGINS` | Comma-separated origins | `http://localhost:3000` |
| `STRIPE_FLAT_PRICE_ID` | Stripe fixed price for the single $50/mo flat tier | Required for billing |
| `STRIPE_METERED_PRICE_ID` | Stripe metered price for overage | Required for billing |

### Frontend (.env.local)

| Variable | Purpose |
|----------|---------|
| `NEXT_PUBLIC_API_URL` | Backend REST API URL **including `/api/v1`** |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key |
| `CLERK_SECRET_KEY` | Clerk secret key (server-side) |

**API URL by environment:**
- Local: `http://localhost:8000/api/v1`
- Dev: `https://api-dev.isol8.co/api/v1`
- Staging: `https://api-staging.isol8.co/api/v1`
- Production: `https://api.isol8.co/api/v1`

---

## AWS CDK Stacks

Located in `apps/infra/lib/stacks/`. Entry point: `apps/infra/lib/app.ts`. Per-environment stage classes: `local-stage.ts`, `isol8-stage.ts`. Stacks deliberately pass secret *names* (not `ISecret`) and KMS key *ARNs* (not objects) between stacks to avoid cross-stack KMS auto-grant cycles — see comments in `service-stack.ts`.

| Stack | Purpose |
|-------|---------|
| `network-stack.ts` | VPC (public/private subnets, NAT), ALB, NLB (for WebSocket VPC Link V1), security groups |
| `auth-stack.ts` | Secrets Manager secrets (Clerk, Stripe, encryption keys) + KMS key |
| `database-stack.ts` | All DynamoDB tables (users, containers, billing-accounts, api-keys, usage-counters, pending-updates, channel-links, admin-actions, ws-connections, plus Teams BFF tables) |
| `container-stack.ts` | ECS cluster for per-user OpenClaw containers, EFS filesystem, Cloud Map namespace/service, container task-definition scaffolding |
| `service-stack.ts` | Backend FastAPI service on ECS Fargate: task definition (Docker image from ECR), service, autoscaling, target-group wiring, DynamoDB/secrets IAM |
| `api-stack.ts` | WebSocket API Gateway, Lambda authorizer (Clerk JWT), VPC Link V1 wiring |
| `paperclip-stack.ts` | Per-tenant Paperclip BFF — Aurora cluster, Fargate service, Cloud Map service discovery, ALB host route. Cross-stack KMS-cycle posture documented inline. See `apps/infra/paperclip/RUNBOOK.md`. |
| `dns-stack.ts` | Route53 records + ACM certs (ALB + CloudFront) |
| `observability-stack.ts` | CloudWatch dashboards, alarms, log retention |

`apps/infra/openclaw/` is a sibling directory (not a stack) containing the extended OpenClaw Dockerfile + README. Built by `.github/workflows/build-openclaw-image.yml` and pushed to ECR `isol8/openclaw-extended`. The tag Isol8 runs is pinned in `openclaw-version.json`.

---

## Desktop App

Tauri 2 app at `apps/desktop/`. Bundle ID: `co.isol8.desktop`. Product name: `Isol8`.

- Rust sidecar host at `apps/desktop/src-tauri/src/` (`lib.rs`, `main.rs`, `browser_sidecar.rs`, `exec_approvals.rs`, `node_client.rs`, `node_invoke.rs`, `tray.rs`)
- Points at `https://dev.isol8.co/chat` as the embedded web view
- Bundled binaries (`src-tauri/bin/`): `isol8-browser-service` (externalBin), per-arch `node` and `openclaw-host` copies for macOS (aarch64 + x86_64)
- Protocol handler: `isol8://` deep links (via `tauri-plugin-deep-link`)
- Single-instance lock (`tauri-plugin-single-instance`)
- Signed DMG for macOS, built by `.github/workflows/desktop-build.yml`
- Auth flow uses the backend's `/desktop-auth` device-code endpoints + the frontend's `/auth/desktop-callback` page
- Requirement driver: WebAuthn/passkey support (see `project_desktop_app_tauri` memory)
