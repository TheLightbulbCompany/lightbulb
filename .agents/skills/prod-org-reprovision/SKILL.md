---
name: prod-org-reprovision
description: Wipe a prod owner's container runtime + Paperclip side so the next /container/status poll re-provisions Friday/Atlas on the latest OpenClaw image, while preserving Stripe subscription, credits, Clerk org, and marketplace rows. Use when the user asks to reprovision a prod org or bring a stale org current.
---

# Prod Org Reprovision (preserving billing/sub/marketplace)

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

# 2b. O6 local-sqlite layout only (prod.storage="local-sqlite" in
# openclaw-version.json): delete the owner's Litestream replicas, or the
# reprovisioned container restores the OLD hot state from S3 (sessions,
# device trust, auth rows) instead of starting clean.
aws s3 rm "s3://isol8-prod-openclaw-replicas/$OWNER/" --recursive --profile isol8-admin

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

# 5b. Delete each org member's orphan Aurora ``user`` row via the recovery
#     script so no member is missed (a surviving member row leaves an orphan
#     Better Auth user that wedges that member's /teams on their next visit
#     — re-signup 422s against the existing user). Org owners have ONE
#     PaperclipCompany row PER MEMBER, all sharing the same ``company_id``;
#     each row's ``paperclip_user_id`` is a separate Better Auth user.
#     Pass BOTH --email (scopes the Aurora delete) AND --user-id (the
#     member's Clerk user_id, scopes the DDB delete) so recovering one
#     member never touches another member's failed row.
cd apps/backend
# email:clerk_user_id pairs for each member being reprovisioned
for pair in member1@isol8.co:user_xxx member2@isol8.co:user_yyy member3@isol8.co:user_zzz; do
  email="${pair%%:*}"; uid="${pair##*:}"
  uv run python -m scripts.recover_orphan_paperclip_member \
    --env prod --email "$email" --user-id "$uid" --owner-id "$OWNER" --apply
done
# Post-condition: SELECT email FROM "user" must show ONLY
# admin+paperclip@isol8.co (the prod bootstrap). The script refuses to
# delete it, refuses ambiguous email matches, and refuses to delete more
# than one failed DDB row without --user-id — so cross-check the remaining
# rows before declaring the org clean.

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
