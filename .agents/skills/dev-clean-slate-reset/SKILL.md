---
name: dev-clean-slate-reset
description: Full wipe of Isol8 dev state (ECS containers, EFS, DynamoDB, Paperclip Aurora, Stripe, Clerk) for fresh end-to-end testing. Use when the user asks to reset dev, wipe dev state, or clean-slate a dev account.
---

# Clean-Slate Dev Reset

Full wipe of dev state for fresh testing. Requires AWS SSO (`aws sso login --profile isol8-admin`).

## This applies to PROD too — substitute the env and follow every step

There is no separate prod procedure. When wiping prod, use these same steps
with `dev` → `prod` throughout, and **do not improvise a shorter list**.

**Step 4 (Paperclip Aurora) is the one that gets dropped, and dropping it
breaks the very thing a wipe is for.** Paperclip lives in Aurora, not
DynamoDB, so clearing DDB + Clerk + ECS + EFS + Stripe looks complete and
is not. The leftover Better Auth user then blocks re-signup on that email:
`provision_org` gets a **422 duplicate-email**, writes the company row as
`status=failed` with an empty `company_id`, and every downstream Teams
surface degrades. Symptoms are indirect and do not mention Paperclip —
the onboarding funnel shows *"Couldn't save that just yet. Retry"* and
retrying never succeeds.

Verified live on 2026-07-26: a prod wipe that skipped step 4 left
`admin@isol8.co` in Better Auth, and the next signup's org could not create
its Paperclip company at all.

**Do NOT protect `admin@isol8.co` when deleting Better Auth users.** Only
`admin+paperclip@isol8.co` holds `instance_admin` — confirmed against
`instance_user_roles` (join column is `user_id`, not `userId`).
`admin@isol8.co` is a regular org owner, and treating it as a bootstrap
admin is the over-broad `BOOTSTRAP_EMAILS` guard that issue #1469 exists to
narrow. Deleting the real bootstrap admin breaks
`PaperclipAdminClient.get_admin_session_cookie`; deleting the other one is
required for a clean slate.

**Company rows cannot be removed with raw SQL** — `DELETE FROM companies`
is FK-blocked across ~155 tables. Leftover company shells are harmless
(a fresh signup mints a new company), so delete the `"user"` rows and leave
the shells.

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

**1b. Delete Litestream S3 replicas (O6 local-sqlite layout only):** once dev
runs `storage: "local-sqlite"` (openclaw-version.json), each owner's hot
SQLite replicates to `s3://isol8-dev-openclaw-replicas/{owner_id}/…` and the
container boot script RESTORES whatever survives there — skipping this step
resurrects the pre-wipe sessions/auth rows on the next provision. (The
backend's own wipe paths — `DELETE /debug/provision`, account_wipe — already
do this via `core/services/litestream_replicas.py`; this manual step is for
console-driven wipes.)

```bash
aws s3 rm "s3://isol8-dev-openclaw-replicas/<owner_id>/" --recursive --profile isol8-admin
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
