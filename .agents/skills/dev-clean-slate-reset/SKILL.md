---
name: dev-clean-slate-reset
description: Full wipe of Lightbulb dev state (AgentCore runtimes, S3 Hermes profiles, DynamoDB, Paperclip Aurora, Stripe, Clerk) for fresh end-to-end testing. Use when the user asks to reset dev, wipe dev state, or clean-slate a dev account.
---

# Clean-Slate Dev Reset

Full wipe of dev state for fresh testing. Requires AWS SSO
(`aws sso login --profile isol8-admin`).

## This applies to PROD too — substitute the env and follow every step

There is no separate prod procedure. When wiping prod, use these same steps
with `dev` → `prod` throughout, and **do not improvise a shorter list**.

**Step 3 (Paperclip Aurora) is the one that gets dropped, and dropping it
breaks the very thing a wipe is for.** Paperclip lives in Aurora, not
DynamoDB, so clearing DDB + Clerk + the runtime + Stripe looks complete and
is not. The leftover Better Auth user then blocks re-signup on that email:
`provision_org` gets a **422 duplicate-email**, writes the company row as
`status=failed` with an empty `company_id`, and every downstream Teams
surface degrades. Symptoms are indirect and do not mention Paperclip —
the onboarding funnel shows *"Couldn't save that just yet. Retry"* and
retrying never succeeds.

Verified live on 2026-07-26: a prod wipe that skipped it left `admin@isol8.co`
in Better Auth, and the next signup's org could not create its Paperclip
company at all.

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

---

## 1. Prefer the backend's own door

`DELETE /api/v1/debug/user-data` (403 outside dev/local/test) is the whole of
step 2 below plus the DDB rows, run by the code that owns the teardown seam.
It is what the e2e harness calls at the end of each run, and it is one of only
two terminal callers that pass `delete_profiles=True`: `account_wipe` (user-keyed)
and `workspace_wipe` (org-keyed). Per signed-in
user, from the browser console on a dev page:

```javascript
await fetch('https://api-dev.thelightbulb.company/api/v1/debug/user-data', {method:'DELETE', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
```

Everything below is the console-driven equivalent, for owners whose Clerk
session you do not have.

## 2. Tear down each workspace's AgentCore runtime

`hermes_provisioning.teardown_hermes_runtime` is the sequence; by hand it is
four best-effort calls per owner, each idempotent. **Order matters for the
first two:** a warm session caches the bearer it read at cold start, so
stopping the session before deleting the runtime is what stops it presenting a
now-dead credential until AWS reaps it.

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
OWNER=org_xxx
BUCKET=lightbulb-dev-openclaw-replicas   # OWNER_STATE_BUCKET; name is a scar, not a second bucket

# a) the runtime ARN + the live bearer generation live on the containers row
ARN=$(aws dynamodb get-item --table-name lightbulb-dev-containers \
  --key "{\"owner_id\":{\"S\":\"$OWNER\"}}" \
  --output text --query 'Item.agentcore_runtime_arn.S')

# b) stop the warm session, THEN delete the runtime
# session id == runtime_transport.session_id_for: "lb-<owner>-<sha256(owner)[:16]>",
# right-padded with "0" to AgentCore's 33-char floor.
SID=$(python3 -c 'import sys,hashlib
o=sys.argv[1]; c=f"lb-{o}-{hashlib.sha256(o.encode()).hexdigest()[:16]}"
print(c if len(c)>=33 else c.ljust(33,"0"))' "$OWNER")
aws bedrock-agentcore stop-runtime-session --agent-runtime-arn "$ARN" \
  --runtime-session-id "$SID" || true
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "${ARN##*/}"

# c) the ONE boot secret (hook token, gateway token, Bedrock bearer)
aws secretsmanager delete-secret --secret-id "lightbulb/dev/hermes/$OWNER/boot" \
  --force-delete-without-recovery

# d) EVERY Hermes profile + the root auth.json. THIS is the owner's content on
#    this transport — there is no EFS tree and no access point to delete.
aws s3 rm "s3://$BUCKET/$OWNER/hermes/" --recursive
```

Enumerate the runtimes you have not accounted for with
`aws bedrock-agentcore-control list-agent-runtimes` — dev runtime names are
`lb_dev_<sanitised owner>_<12 hex>` (`agentcore_runtime.runtime_name_for`).

> The session id above is derived from the owner id ON PURPOSE. AgentCore
> creates a new microVM per distinct `runtimeSessionId`, so a second id means
> two gateways writing one S3 prefix — reproduced 2026-09-16, it corrupts the
> replica set silently and every later restore hangs with no error. Do not
> invent one.

`delete_workspace_runtime` also reaps the owner's Bedrock footprint (gate-token
rows, application inference profiles, the per-owner IAM role) off the deleted
row's `ALL_OLD` image, because that row was the only handle on it. Doing this
by hand means also clearing `lightbulb-dev-bedrock-gate-tokens` and the
owner's AIPs — which is a good reason to use step 1 instead.

## 3. Wipe the DynamoDB tables

Names are `lightbulb-{env}-*` (58 tables in the account; `database-stack.ts` is
the list). The per-user ones a reset cares about are `containers`, `users`,
`billing-accounts`, `paperclip-companies`, `usage-counters`, `pending-updates`,
`channel-links`, `api-keys`, `ws-connections`, `messaging`, `integrations`,
`oauth-tokens`, `bedrock-gate-tokens`.

```bash
for t in lightbulb-dev-containers lightbulb-dev-users lightbulb-dev-billing-accounts \
         lightbulb-dev-paperclip-companies lightbulb-dev-usage-counters \
         lightbulb-dev-pending-updates lightbulb-dev-channel-links \
         lightbulb-dev-api-keys lightbulb-dev-ws-connections lightbulb-dev-bedrock-gate-tokens; do
  keys=$(aws dynamodb describe-table --table-name "$t" \
    --query 'Table.KeySchema[].AttributeName' --output text)
  count=0
  while read -r line; do
    [ -z "$line" ] && continue
    aws dynamodb delete-item --table-name "$t" --key "$line" >/dev/null 2>&1
    count=$((count+1))
  done < <(aws dynamodb scan --table-name "$t" --output json 2>/dev/null | python3 -c "
import json, sys
keys = '$keys'.split()
d = json.load(sys.stdin)
for item in d.get('Items', []):
    print(json.dumps({k: item[k] for k in keys}))
")
  echo "  $t: $count deleted"
done
```

## 4. Wipe Paperclip company + user (Aurora — RDS Data API)

Paperclip data does NOT live in DynamoDB; it is in a per-env Aurora cluster
(`lightbulb-dev-paperclip-db`). An orphan user blocks re-signup with the same
email because Better Auth's `sign_up_user` is not idempotent (see
`paperclip_provisioning.py`). Two stages, because Paperclip exposes a
company-delete API but NO user-delete API (Better Auth's `admin()` plugin is
not enabled — verified in `auth/better-auth.ts`).

**4a. Delete the company via the Paperclip admin API.** Run it from the backend
task, which already holds the admin session. `CLUSTER` is the platform ECS
cluster — the backend still runs on Fargate; only the per-user containers are
gone.

```bash
CLUSTER=$(aws ecs list-clusters --output text --query 'clusterArns' \
  | tr '\t' '\n' | grep lightbulb-dev-container)
SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --output text --query 'serviceArns' \
  | tr '\t' '\n' | grep lightbulb-dev-service)
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "${SERVICE##*/}" \
  --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')

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
\"'"
```

Then call `DELETE /api/companies/{company_id}` the same way (the admin client
has `delete_company`). Cascades to agents/threads/etc.

**4b. Delete the orphan user(s) via the RDS Data API.** There is NO Paperclip
API for user deletion; direct SQL is the only option. **Don't try to ECS-exec a
`psql` task or override the migrate task def** — both shell-quoting paths
failed silently in 2026-05 testing. The cluster has Data API enabled
(`HttpEndpointEnabled=true`), so this works from the CLI with no network
plumbing:

```bash
SECRET=$(aws secretsmanager list-secrets \
  --query "SecretList[?contains(Name,\`lightbulb-dev-paperclip-db-credentials\`)].ARN" --output text)
DB=arn:aws:rds:us-east-1:877352799272:cluster:lightbulb-dev-paperclip-db

aws rds-data execute-statement --resource-arn "$DB" --secret-arn "$SECRET" \
  --database paperclip --sql 'SELECT id, email FROM "user" ORDER BY email'

aws rds-data execute-statement --resource-arn "$DB" --secret-arn "$SECRET" \
  --database paperclip \
  --sql "DELETE FROM \"user\" WHERE id IN ('<orphan_user_id_1>','<orphan_user_id_2>')"
```

`account` and `session` cascade automatically (`onDelete: cascade` per
`paperclip/packages/db/src/schema/auth.ts`).

**Bootstrap admin to PRESERVE:** dev `admin@isol8.co`, prod
`admin+paperclip@isol8.co`. Cross-check the SELECT before issuing the DELETE.

**Why neither CloudShell `psql` nor ECS-exec `psql`:** the Aurora cluster sits
in a private subnet with no internet gateway, so CloudShell cannot reach it,
and the migrate task's shell-quoting through ECS exec silently swallows the
SQL. Data API uses HTTPS to a dedicated endpoint and bypasses both. The RDS
console Query Editor (same API underneath) is a valid GUI fallback.

## 5. Stripe

Delete orphan test customers from the
[Stripe dashboard](https://dashboard.stripe.com/test/customers) (dev uses
test-mode keys).

## 6. Clerk

Delete test users/orgs from the [Clerk dashboard](https://dashboard.clerk.com)
if needed. Note prod's org member cap is 20 (Clerk plan max); dev is 0
(unlimited).

---

After all six steps a fresh Clerk sign-up re-provisions from empty: onboarding
→ `ensure_user_ready` → `provision_runner` → a new Paperclip company + starter
roster → new Hermes profiles in S3 → a new AgentCore runtime → a single Stripe
customer.
