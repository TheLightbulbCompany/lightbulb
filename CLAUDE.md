# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isol8 is an AI agent platform powered by [OpenClaw](https://github.com/openclaw/openclaw). Users subscribe, get a per-user ECS Fargate container running the OpenClaw Docker image, and interact with their agents via a WebSocket-based chat UI. The platform uses a Next.js 16 frontend (Vercel), FastAPI backend (ECS Fargate), Clerk for authentication, **DynamoDB** for metadata (users, containers, billing, api-keys, usage counters, pending updates, channel links, admin actions, marketplace listings/purchases/payouts/takedowns, plus the Teams BFF tables), AWS Bedrock + ChatGPT OAuth + BYO API key for LLM inference, Stripe for billing (subscription + marketplace purchases), EFS for per-user agent workspaces, and a per-tenant Paperclip BFF for the Teams Inbox. Infrastructure is managed via **AWS CDK** in `apps/infra/` (single source of IaC truth). The desktop app is **Tauri** (not Electron) — see `project_desktop_app_tauri` memory.

Every freshly provisioned owner gets two seed agents inside their Paperclip company: **Friday** (the per-owner CEO / orchestrator) and **Atlas** (the in-house agent-recruiter). Both are seeded by `paperclip_provisioning.provision_org` — see the "Provisioning Flow" section below. Custom agents beyond Friday/Atlas can be **published to the Isol8 marketplace** for other users to deploy — see the "Marketplace" section.

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

**Note on ECS cluster name:** The cluster ARN suffix changes on stack
recreation (e.g. `isol8-dev-container-ClusterEB0386A7-L9MzqPCGDupk` as of
2026-05-09). The hardcoded names in the commands below may be stale —
always discover dynamically:
```bash
CLUSTER=$(aws ecs list-clusters --profile isol8-admin --region us-east-1 \
  --query 'clusterArns[?contains(@,`isol8-dev-container`)]' --output text)
SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --profile isol8-admin \
  --region us-east-1 --query 'serviceArns[?contains(@,`isol8-dev-service`)]' \
  --output text | awk -F/ '{print $NF}')
```

**1. Delete ECS containers:** User deletes their OpenClaw ECS services + EFS access points manually from the AWS console (or via `DELETE /debug/provision` per user above). Programmatically:
```bash
# Per-user openclaw service + matching EFS access point
SERVICE=openclaw-<user_id>-<hash>
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0 \
  --profile isol8-admin --region us-east-1
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force \
  --profile isol8-admin --region us-east-1
# EFS access point name pattern: isol8-<user_id>
AP=$(aws efs describe-access-points --file-system-id <openclaw-efs-fs-id> \
  --profile isol8-admin --region us-east-1 \
  --query 'AccessPoints[?contains(Name||``,`<user_id>`)].AccessPointId' --output text)
aws efs delete-access-point --access-point-id "$AP" --profile isol8-admin --region us-east-1
```

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

**4. Wipe Paperclip company + user (Aurora — use RDS Query Editor):**

Paperclip data does NOT live in DynamoDB; it's in a per-env Aurora cluster
(`isol8-dev-paperclip-db`). Wiping DDB does NOT touch Paperclip — orphan
companies + users will linger. **An orphan user blocks re-signup with the
same email** because Better Auth's `sign_up_user` is not idempotent (see
`paperclip_provisioning.py:144-150`). So this step is required for a true
clean slate.

Two-stage cleanup because Paperclip exposes a company-delete API but NO
user-delete API (Better Auth's `admin()` plugin is not enabled — verified
in `auth/better-auth.ts`).

**4a. Delete the company via Paperclip admin API** (from the backend):
```bash
# Find the company_id by listing — these calls go through the
# already-authenticated PaperclipAdminClient inside the backend container.
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
  --profile isol8-admin --region us-east-1 --query 'taskArns[0]' --output text \
  | awk -F/ '{print $NF}')

# List companies + users (script uses the existing admin session)
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \
  --interactive --command "/bin/sh -c 'cd /app && uv run python -c \"
import asyncio, httpx
from core.config import settings
from core.services.paperclip_admin_session import get_admin_session_cookie
async def main():
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30) as c:
        h = {\\\"Cookie\\\": await get_admin_session_cookie(c)}
        if settings.PAPERCLIP_PUBLIC_URL: h[\\\"Origin\\\"] = settings.PAPERCLIP_PUBLIC_URL
        for p in [\\\"/api/companies\\\", \\\"/api/admin/users\\\"]:
            r = (await c.get(p, headers=h)).json()
            print(p, len(r))
            for x in r: print(\\\" \\\", x.get(\\\"id\\\"), x.get(\\\"name\\\") or x.get(\\\"email\\\"))
asyncio.run(main())
\"'" --profile isol8-admin --region us-east-1
```

Then call `DELETE /api/companies/{company_id}` similarly (admin client has
`delete_company`). Cascades to agents/threads/etc.

**4b. Delete the orphan user(s) via RDS Data API** (the fastest path):

There is NO Paperclip API for user deletion. Direct SQL is the only option.
**Don't try to ECS-exec a `psql` task or override the migrate task def** —
both shell-quoting paths failed silently in 2026-05 testing.

The Aurora cluster has **Data API enabled** (`HttpEndpointEnabled=true`), so
`aws rds-data execute-statement` works from the CLI without any network
plumbing. This is faster than the RDS console Query Editor and scriptable
into the rest of the cleanup:

```bash
# Find the credentials secret ARN (dev shown — replace with prod when needed)
SECRET=$(aws secretsmanager list-secrets --profile isol8-admin --region us-east-1 \
  --query "SecretList[?contains(Name,\`isol8-dev-paperclip-db-credentials\`)].ARN" \
  --output text)
CLUSTER=arn:aws:rds:us-east-1:877352799272:cluster:isol8-dev-paperclip-db

# List remaining users
aws rds-data execute-statement \
  --resource-arn "$CLUSTER" --secret-arn "$SECRET" --database paperclip \
  --sql 'SELECT id, email FROM "user" ORDER BY email' \
  --profile isol8-admin --region us-east-1

# Delete one or more orphan users. ``account`` and ``session`` cascade
# automatically (``onDelete: cascade`` per
# ``paperclip/packages/db/src/schema/auth.ts``).
aws rds-data execute-statement \
  --resource-arn "$CLUSTER" --secret-arn "$SECRET" --database paperclip \
  --sql "DELETE FROM \"user\" WHERE id IN ('<orphan_user_id_1>','<orphan_user_id_2>')" \
  --profile isol8-admin --region us-east-1
```

**Bootstrap admin to PRESERVE:**
- Dev: `admin@isol8.co` (Better Auth user id from `secrets/admin-id`)
- Prod: `admin+paperclip@isol8.co`

Deleting the bootstrap user breaks the backend's
`PaperclipAdminClient` — `get_admin_session_cookie` can't authenticate
without it. Cross-check the SELECT before issuing the DELETE.

The RDS console Query Editor (which also uses the Data API under the hood)
remains a valid fallback if you prefer a GUI.

**Why neither CloudShell `psql` nor ECS-exec `psql`:** the Aurora cluster
sits in a private subnet with no internet gateway; CloudShell can't reach
it directly, and the migrate task's shell-quoting through ECS exec
silently swallows the SQL. Data API uses HTTPS to a dedicated endpoint and
bypasses both.

**5. Clean up Stripe:** Delete orphan test customers manually from the [Stripe dashboard](https://dashboard.stripe.com/test/customers) (dev uses test mode keys).

**6. Clean up Clerk:** Delete test users/orgs from the [Clerk dashboard](https://dashboard.clerk.com) if needed.

After all six steps, re-provision starts fresh: new Clerk sign-up → onboarding → container provision → single Stripe customer + fresh Paperclip company.

### Prod Org Reprovision (preserving billing/sub/marketplace)

Different workflow from the dev reset above. The goal is to wipe an
existing owner's **container runtime + Paperclip side** so the next
`/container/status` poll re-provisions Friday/Atlas onto the latest
pinned OpenClaw image — but keep their Stripe subscription, Bedrock
credits, Clerk org, and any marketplace listings/purchases intact.

Use cases: orgs whose container was provisioned before a major upgrade
(e.g. pre-Paperclip, pre-marketplace) and need to be brought current.
Performed live on 2026-05-20 against `org_3DBS8G83oPrMm49yN2b70EjoFpN`.

**Preserved (do NOT touch):** `users`, `billing-accounts`, `api-keys`,
`usage-counters`, `admin-actions`, all `marketplace-*` tables, Clerk
org/users, Stripe customer + subscription.

**Wiped:** ECS service, EFS access point + workspace dir, `containers`
DDB row, `pending-updates`, `channel-links`, Paperclip company in
Aurora (cascades to agents/threads), Aurora Better Auth `user` rows
for each org member, all `paperclip-companies` DDB rows for the org.

```bash
OWNER=org_xxx                                  # from `containers` DDB scan
CLUSTER=$(aws ecs list-clusters --profile isol8-admin --region us-east-1 \
  --output text --query 'clusterArns' | tr '\t' '\n' | grep isol8-prod-container)
PER_USER_SERVICE=$(aws dynamodb get-item --table-name isol8-prod-containers \
  --key "{\"owner_id\":{\"S\":\"$OWNER\"}}" --profile isol8-admin --region us-east-1 \
  --output text --query 'Item.service_name.S')
ACCESS_POINT=$(aws dynamodb get-item --table-name isol8-prod-containers \
  --key "{\"owner_id\":{\"S\":\"$OWNER\"}}" --profile isol8-admin --region us-east-1 \
  --output text --query 'Item.access_point_id.S')
BACKEND_SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --profile isol8-admin \
  --region us-east-1 --output text --query 'serviceArns' | tr '\t' '\n' \
  | grep isol8-prod-service)

# 1. Drain + delete per-user ECS service. Wait for runningCount=0 before step 2.
aws ecs update-service --cluster "$CLUSTER" --service "$PER_USER_SERVICE" \
  --desired-count 0 --profile isol8-admin --region us-east-1
aws ecs delete-service --cluster "$CLUSTER" --service "$PER_USER_SERVICE" \
  --force --profile isol8-admin --region us-east-1

# 2. Delete EFS access point (must wait until step 1's task fully stopped or this errors with "in use").
aws efs delete-access-point --access-point-id "$ACCESS_POINT" \
  --profile isol8-admin --region us-east-1

# 3. rm -rf the EFS workspace dir via ECS exec on the BACKEND task.
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$BACKEND_SERVICE" \
  --profile isol8-admin --region us-east-1 --query 'taskArns[0]' --output text \
  | awk -F/ '{print $NF}')
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \
  --interactive --command "/bin/sh -c 'rm -rf /mnt/efs/users/$OWNER && ls /mnt/efs/users/'" \
  --profile isol8-admin --region us-east-1

# 4. Delete the containers DDB row.
aws dynamodb delete-item --table-name isol8-prod-containers \
  --key "{\"owner_id\":{\"S\":\"$OWNER\"}}" --profile isol8-admin --region us-east-1

# 5a. Delete the Paperclip company via the admin API. Pipe a base64-encoded
#     Python script through ECS exec to bypass shell-quoting issues — calls
#     PaperclipAdminClient.delete_company. Cascades to agents/threads.
COMPANY_ID=...   # from `aws dynamodb scan isol8-prod-paperclip-companies` (all rows for OWNER share the same company_id)
SCRIPT_B64=$(cat <<PYEOF | base64
import asyncio, httpx
from core.config import settings
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
async def main():
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30) as c:
        cookie = await get_admin_session_cookie(c)
        admin = PaperclipAdminClient(http_client=c)
        await admin.delete_company(session_cookie=cookie, company_id="$COMPANY_ID")
asyncio.run(main())
PYEOF
)
SCRIPT_B64=$(echo "$SCRIPT_B64" | tr -d '\n')
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \
  --interactive --command "/bin/sh -c 'cd /app && echo $SCRIPT_B64 | base64 -d | uv run python -'" \
  --profile isol8-admin --region us-east-1

# 5b. Delete the orphan Aurora ``user`` rows for each org member. Use the
#     Data API path documented above. Org owners have ONE PaperclipCompany
#     row PER MEMBER, all sharing the same ``company_id``; each row's
#     ``paperclip_user_id`` is a separate Better Auth user.
SECRET=$(aws secretsmanager list-secrets --profile isol8-admin --region us-east-1 \
  --query "SecretList[?contains(Name,\`isol8-prod-paperclip-db-credentials\`)].ARN" \
  --output text)
DB=arn:aws:rds:us-east-1:877352799272:cluster:isol8-prod-paperclip-db
aws rds-data execute-statement --resource-arn "$DB" --secret-arn "$SECRET" \
  --database paperclip \
  --sql "DELETE FROM \"user\" WHERE id IN ('<member1_pcuid>','<member2_pcuid>','<member3_pcuid>')" \
  --profile isol8-admin --region us-east-1
# Verify only ``admin+paperclip@isol8.co`` (the prod bootstrap) remains.

# 5c. Delete all PaperclipCompany DDB rows for the org's members.
for uid in user_xxx user_yyy user_zzz; do
  aws dynamodb delete-item --table-name isol8-prod-paperclip-companies \
    --key "{\"user_id\":{\"S\":\"$uid\"}}" --profile isol8-admin --region us-east-1
done
```

**Hands-off after step 5c.** Steps 6–8 require the user's Clerk session
and physical machine:

6. **Visit `/chat`** → frontend polls `GET /container/status` → backend's
   `ensure_user_ready` re-runs `provision_org` (re-seeds Paperclip company
   + Friday + Atlas with current adapter wiring) and dispatches
   `background_provision` which creates a new per-user ECS service.
   The service clones from the CDK base task def (e.g.
   `isol8-prod-openclaw:55`) so it automatically picks up the latest
   pinned OpenClaw image — no manual task-def step needed.
7. **Hit Hire on each marketplace listing** the org owns. Seller-self-deploy
   bypass (PR #712) skips Stripe checkout.
8. **Re-pair the desktop app** if used. The container row's
   `operator_device_id` + `operator_priv_key_enc` were wiped with step 4.

**Multi-member org caveat:** PaperclipCompany is keyed per-user (NOT
per-org), so an org with N members has N DDB rows all pointing at the
same `company_id`. Each member also has their own Better Auth user
record in Aurora. The cleanup MUST remove every member's row in both
places — leaving any one of them blocks re-provision (the surviving
PaperclipCompany row short-circuits `provision_org`'s idempotency check,
preventing Friday/Atlas re-seeding for everyone).

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
  --> FastAPI /ws/message --> ECS describe_tasks IP discovery --> per-user OpenClaw container (ECS Fargate)
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
| `ecs_manager.py` | Per-user ECS Fargate lifecycle. Container IP discovery via ECS `describe_tasks` (`discover_ip`), **not** Cloud Map — Cloud Map is registration-only (each per-user service is attached via `serviceRegistries`, but the backend never queries it for IPs). Per-user EFS access points. Service naming: `openclaw-{user_id}-{hash}`. |
| `config.py` | `write_openclaw_config()`: generates `openclaw.json` with Bedrock provider and memory plugin. |
| `workspace.py` | EFS file I/O at `/mnt/efs/users/{user_id}/`. |

### Gateway (`core/gateway/`)

| File | Purpose |
|------|---------|
| `connection_pool.py` | `GatewayConnectionPool`: persistent WebSocket connections to per-user OpenClaw containers. OpenClaw protocol v4 handshake, RPC proxy, event forwarding, idle scale-to-zero reaper, usage callback. |
| `node_connection.py` | Per-user node-host connection for desktop browser-proxy / exec-approval flows. |

### Services (`core/services/`)

The services directory has grown past per-file annotation. Run `ls apps/backend/core/services/` for the current list. Notable groups:

- **Billing / usage**: `billing_service.py` (Stripe customers, checkout, portal, subscription lifecycle), `usage_service.py` (per-owner + per-member atomic counters → Stripe Meters), `credit_ledger.py`.
- **Container lifecycle**: `update_service.py` (Track 1 silent config patches + Track 2 queued updates; `run_scheduled_worker`), `config_patcher.py` (EFS-safe JSON deep-merge under NFS-compatible `fcntl.lockf`), `config_policy.py` / `config_reconciler.py` (tier-based config protection), `provision_gate.py` (subscription / credits / OAuth gate evaluation).
- **Gateway / WebSocket**: `connection_service.py` (DDB connectionId↔user_id state), `management_api_client.py`, `node_proxy.py` (per-user node-host tracking — relocated from routers/ in May 2026).
- **Catalog**: `catalog_service.py` plus `catalog_s3_client.py` / `catalog_slice.py` / `catalog_package.py` helpers. `catalog_service.deploy_from_artifact` is the consumption-side entry point for marketplace deploys.
- **Marketplace**: `marketplace_service.py` (DDB rows + S3 artifact uploads + status state machine: draft → review → published; published-version transitions are atomic via TransactWriteItems), `marketplace_search.py` (60s in-process cache of published listings, Python-side tokenized scoring), `marketplace_safety.py` (scan tarballs for credential/PII patterns), `agent_export.py` (seller-agent → tarball snapshotter; explicitly filters `sessions/`, `agent/`, dotfiles, junk dirs — see `_OPENCLAW_RUNTIME_STATE_DIRS` docstring for why).
- **Provisioning**: `user_provision.py` (`ensure_user_ready` — the SINGLE canonical readiness entry point, called only from `GET /container/status`; owner path seeds Paperclip synchronously then dispatches container provision as background task), `container_provision.py` (`background_provision`), `friday_identity.py` (IDENTITY/SOUL/USER seeding for Friday + Atlas roster prompt copy).
- **Admin surface**: `admin_service.py`, `admin_audit.py`, `admin_redact.py`, `clerk_admin.py` (Clerk REST wrapper — every Clerk API call should go through here), `cloudwatch_logs.py` / `cloudwatch_url.py`, `posthog_admin.py`.
- **Teams BFF (Paperclip)**: many `paperclip_*` services (`paperclip_admin_client.py`, `paperclip_admin_session.py`, `paperclip_user_session.py`, `paperclip_event_client.py`, `paperclip_provisioning.py` — owns `provision_org` which seeds Friday + Atlas, `paperclip_autoprovision.py`, `paperclip_owner_email.py`, `paperclip_event_router.py`, `teams_event_broker_singleton.py`, etc.) coordinating with the per-tenant Paperclip BFF.
- **Auth / identity**: `oauth_service.py` (ChatGPT OAuth flow), `key_service.py` (BYOK API key encryption via Fernet), `service_token.py` (JWT minting for cross-service calls).
- **Cross-cutting**: `idempotency.py` (DDB conditional-write keyed store), `webhook_dedup.py`, `system_health.py`.

### Routers (`routers/`) — all mounted under `/api/v1`

Run `ls apps/backend/routers/` and `ls apps/backend/routers/teams/` for the current set. The flat-`routers/` files cover users, webhooks (Clerk + Stripe), websocket_chat, billing, container lifecycle (`container.py`, `container_rpc.py`, `container_recover.py`), `control_ui_proxy.py`, `channels.py`, `settings_keys.py`, `integrations.py` (MCP servers), `updates.py`, `catalog.py`, `config.py`, `workspace_files.py`, `desktop_auth.py`, `paperclip_proxy.py` (per-tenant Paperclip proxy with route-filter security model — see `docs/audit-2026-05-02-paperclip-routes.md`), `oauth.py` (ChatGPT OAuth), `admin.py`, `admin_catalog.py`, marketplace surfaces (`marketplace_listings.py` for seller create/submit + buyer search/deploy, `marketplace_admin.py` for admin approve/reject + retire, `marketplace_purchases.py` for the Stripe checkout/webhook for paid listings, `marketplace_payouts.py` for seller payout accounts), and `debug.py` (403 in prod).

The `routers/teams/` subpackage is the Teams Inbox BFF surface — agents, threads, messages, channels, members, attachments, search, etc. It composes the `paperclip_*` services and is what the `/teams/*` UI calls.

Per-user node-host tracking (`handle_node_connect`, `is_node_connection`, etc.) was relocated to `core/services/node_proxy.py` in May 2026 — it never lived behind an HTTP route despite the historical filename, and `routers/websocket_chat.py` consumes it directly.

### Models (`models/`)

Intentionally empty (just an `__init__.py` docstring: *"DynamoDB items are plain dicts, no ORM models needed."*). Use repositories instead.

### Request/response schemas (`schemas/`)

Pydantic request/response models for the HTTP surface: `billing.py`, `user_schemas.py`.

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
| `/admin` | Admin surface — accessible only via allowed admin hosts (`admin.isol8.co`, `admin.dev.isol8.co`, `admin.localhost:3000`); 404s on non-admin hosts. Allowlist lives in `apps/frontend/src/middleware.ts`. |
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
  |                  |                  | ECS describe_tasks    |
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
                  3. asyncio.create_task(background_provision(owner_id))
                       └─ ecs_manager.provision_user_container
                            ├─ per-user EFS access point + ECS service
                            └─ write_user_configs → write_openclaw_config(paperclip_agents=[…])
                                  └─ openclaw.json on EFS lists Friday/Atlas agent_ids in agents.list[]
```

Key invariants:

- **`GET /container/status` is the only caller of `ensure_user_ready`** (see `user_provision.py` module docstring). Clerk webhooks are pure record-keeping; they do not trigger provisioning.
- **Paperclip URL is NOT in `openclaw.json`** — the Paperclip↔OpenClaw binding is on the Paperclip seed-agent's `adapterConfig.url`. The container connects to Paperclip via the agent's adapter at runtime; the container's own config only knows the agent IDs and workspace paths.
- **`provision_org` is idempotent** — short-circuits on existing active rows. Member adds and duplicate signups return 409, which the caller treats as success (see `_ensure_paperclip_for_owner` line 141-156).

### Legacy containers (pre-Paperclip provisioning)

Owners whose container was provisioned **before** Paperclip integration shipped have a container row but no `PaperclipCompany` row, and an `openclaw.json` that lists no agents. They get 409s from `/teams` and have no Friday/Atlas to chat with.

Two fixes:

1. **Full reprovision** — `DELETE /api/v1/debug/provision` then `POST /api/v1/debug/provision`. The new poll cycle invokes `ensure_user_ready` against an empty slate.
2. **Backfill script** — `apps/backend/scripts/backfill_paperclip_for_legacy_users.py` paginates the `containers` table, finds owners with no Paperclip row, and calls `provision_org` for each. Idempotent. Defaults to dry-run; pass `--apply` to actually run. Required when the container itself should be preserved (e.g. existing EFS workspace state).

**`PATCH /debug/provision` cannot wire Paperclip.** It only deep-merges `openclaw.json`. It cannot create a Paperclip company, cannot sign up a Paperclip user, cannot seed Friday/Atlas. The `config_patcher` is for config drift, not for provisioning gaps.

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

## Marketplace

Sellers publish custom OpenClaw agents; buyers discover them and deploy a copy into their own container. Publishing and provisioning are **decoupled** — artifacts in S3 are immutable + versioned, so an old container deploying an old listing is safe.

### Status state machine

`draft → review → published → retired | taken_down`

- **draft** — seller's working row, artifact may or may not be uploaded yet.
- **review** — seller has submitted; awaits admin approval.
- **published** — buyer-visible. Publishing a new version atomically retires the previous published version (`marketplace_service.publish_v2` via `TransactWriteItems`).
- **retired / taken_down** — no longer searchable; previously bought entitlements still resolve.

### Seller publish pipeline

```
POST /api/v1/marketplace/listings                       (create draft row in DDB)
POST /listings/{id}/artifact-from-agent                 (snapshot EFS agent → S3)
   └─ agent_export.export_agent_from_efs
        ├─ _build_tarball   — filters .git, __pycache__, node_modules, .DS_Store, dotfiles
        │                     AND _OPENCLAW_RUNTIME_STATE_DIRS (sessions/, agent/)
        │                     — see agent_export.py:70-88 for why; PR #687.
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

**Friday is unpublishable** — `FRIDAY_PUBLISH_ERROR_MSG` guard in `agent_export.py:49` blocks the export at the router boundary. Publishing Friday would leak owner-keyed seeding content and hand a buyer a clone of someone else's CEO.

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
| `MARKETPLACE_LISTINGS_TABLE` | DDB listings table (draft / review / published / retired / taken_down rows) | Set by CDK; empty disables marketplace |
| `MARKETPLACE_PURCHASES_TABLE` | DDB purchases table (buyer entitlements + Stripe payment refs) | Set by CDK |
| `MARKETPLACE_PAYOUT_ACCOUNTS_TABLE` | DDB seller payout-account table | Set by CDK |
| `MARKETPLACE_TAKEDOWNS_TABLE` | DDB takedown / abuse-report table | Set by CDK |
| `MARKETPLACE_ARTIFACTS_BUCKET` | S3 bucket for `listings/{id}/v{n}/workspace.tar.gz` + `manifest.json` + `openclaw-slice.json` | Set by CDK |

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
| `database-stack.ts` | All DynamoDB tables (users, containers, billing-accounts, api-keys, usage-counters, pending-updates, channel-links, admin-actions, ws-connections, marketplace-listings + marketplace-purchases + marketplace-payout-accounts + marketplace-takedowns, plus Teams BFF tables) and the marketplace artifacts S3 bucket |
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
