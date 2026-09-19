---
name: prod-org-reprovision
description: Wipe a prod owner's workspace runtime + Paperclip side so the next /container/status poll re-provisions the starter roster on the latest Hermes image, while preserving Stripe subscription, credits, Clerk org, and marketplace rows. Use when the user asks to reprovision a prod org or bring a stale org current.
---

# Prod Org Reprovision (preserving billing/sub/marketplace)

Different workflow from the dev reset. The goal is to wipe an existing owner's
**workspace runtime + Paperclip side** so the next `/container/status` poll
re-provisions the starter roster onto the currently pinned Hermes image — but
keep their Stripe subscription, Bedrock credits, Clerk org, and any marketplace
listings/purchases intact.

## First: is a reprovision even the right tool?

Three cheaper things it is often confused with. Reach for them first.

| you want | do this, not a reprovision |
|---|---|
| a newer agent image | flip `hermes-version.json`'s `prod.tag`, promote, then run `apps/backend/scripts/roll_hermes_runtimes.py`. A roll re-specs the SAME runtime in place and keeps every profile. |
| rewritten agent config | `PATCH /api/v1/debug/provision` in dev; in prod, `hermes_provisioning.write_profiles` + `StopRuntimeSession`. The `.revision` bump IS the delivery. |
| the bundled skills refreshed | `apps/backend/scripts/reapply_hermes_skills_dirs.py`. A new image alone does not move skills into a profile that already has a copy. |

A reprovision destroys the owner's Bedrock AIPs along with the row that names
them, so only the provision runner can honestly recover — that is why step 6
is "let the funnel run" and not a hand-rolled sequence.

**Preserved (do NOT touch):** `users`, `billing-accounts`, `api-keys`,
`usage-counters`, `admin-actions`, all `marketplace-*` tables, Clerk
org/users, Stripe customer + subscription.

**Wiped:** the AgentCore runtime, the boot secret, the gate-token rows, the
owner's Bedrock AIPs + role, the `containers` row, `pending-updates`,
`channel-links`, the Paperclip company in Aurora (cascades to agents/threads),
the Aurora Better Auth `user` rows for each org member, and all
`paperclip-companies` DDB rows for the org.

**Hermes profiles in S3 are NOT wiped by default, and that is deliberate
(M6-R42).** `delete_workspace_runtime`'s `delete_profiles` defaults to `False`
precisely because non-terminal callers share it, and passing `True` from a
routine reprovision destroyed a migrated owner's memory, skills, `SOUL.md` and
`auth.json`. If this owner's profiles are the problem, say so explicitly and
delete `s3://$OWNER_STATE_BUCKET/$OWNER/hermes/` by hand — do not make it the
default.

---

## 1-5. Tear down (scripted)

Steps 1-4 are exactly what `hermes_provisioning.delete_workspace_runtime`
does. Prefer calling it over the CLI: it is the teardown seam every caller
shares, and the Bedrock reap in step 4 is structural there — no caller can
forget it. From the backend task:

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
OWNER=org_xxx

CLUSTER=$(aws ecs list-clusters --output text --query 'clusterArns' \
  | tr '\t' '\n' | grep lightbulb-prod-container)
SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --output text --query 'serviceArns' \
  | tr '\t' '\n' | grep lightbulb-prod-service)
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "${SERVICE##*/}" \
  --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')

# 1-4: StopRuntimeSession -> DeleteAgentRuntime -> revoke the gate-token hash ->
#      delete the boot secret -> delete the containers row -> reap the Bedrock
#      footprint off its ALL_OLD image -> sweep channel_links. Profiles KEPT.
SCRIPT_B64=$(cat <<PYEOF | base64
import asyncio
from core.services.hermes_provisioning import delete_workspace_runtime
print(asyncio.run(delete_workspace_runtime("$OWNER")))
PYEOF
)
SCRIPT_B64=$(echo "$SCRIPT_B64" | tr -d '\n')
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \
  --interactive --command "/bin/sh -c 'cd /app && echo $SCRIPT_B64 | base64 -d | uv run python -'"
```

By hand, if you cannot reach a backend task, the CLI equivalents are in the
`dev-clean-slate-reset` skill's step 2 — plus clearing the owner's rows from
`lightbulb-prod-bedrock-gate-tokens` and deleting their application inference
profiles, which the function does for you and a hand teardown silently skips.

**5a. Delete the Paperclip company** via the admin API (cascades to
agents/threads). Get `COMPANY_ID` from
`aws dynamodb scan --table-name lightbulb-prod-paperclip-companies` — all rows
for one org share the same `company_id`.

```bash
COMPANY_ID=...
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
  --interactive --command "/bin/sh -c 'cd /app && echo $SCRIPT_B64 | base64 -d | uv run python -'"
```

**5b. Delete each org member's orphan Aurora `user` row** via the recovery
script, so no member is missed. A surviving member row leaves an orphan Better
Auth user that wedges that member's `/teams` on their next visit (re-signup
422s against the existing user). Org owners have ONE PaperclipCompany row **per
member**, all sharing the same `company_id`; each row's `paperclip_user_id` is
a separate Better Auth user. Pass BOTH `--email` (scopes the Aurora delete) AND
`--user-id` (the member's Clerk user id, scopes the DDB delete) so recovering
one member never touches another member's failed row.

```bash
cd apps/backend
for pair in member1@example.com:user_xxx member2@example.com:user_yyy; do
  email="${pair%%:*}"; uid="${pair##*:}"
  uv run python -m scripts.recover_orphan_paperclip_member \
    --env prod --email "$email" --user-id "$uid" --owner-id "$OWNER" --apply
done
```

Post-condition: `SELECT email FROM "user"` must show ONLY
`admin+paperclip@isol8.co` (the prod bootstrap). The script refuses to delete
it, refuses ambiguous email matches, and refuses to delete more than one failed
DDB row without `--user-id` — so cross-check the remaining rows before
declaring the org clean.

**5c. Delete all PaperclipCompany DDB rows for the org's members.**

```bash
for uid in user_xxx user_yyy; do
  aws dynamodb delete-item --table-name lightbulb-prod-paperclip-companies \
    --key "{\"user_id\":{\"S\":\"$uid\"}}"
done
```

---

## Hands-off after 5c

6. **The owner visits `/chat`** → the frontend polls `GET /container/status` →
   `ensure_user_ready` → `dispatch_provision` → `scripts/provision_runner.py`.
   That runner mints the hook/gateway tokens and the Bedrock gate bearer into a
   fresh boot secret, re-seeds the Paperclip company + starter roster with the
   native `hermes_gateway` adapter, writes one Hermes profile per agent to
   `<owner>/hermes/<agent>/`, and calls `CreateAgentRuntime` on
   `AGENTCORE_HERMES_IMAGE` — so it picks up the currently pinned Hermes tag
   with no manual image step.
7. **Hit Hire on each marketplace listing** the org owns. The seller-self-deploy
   bypass (PR #712) skips Stripe checkout. Re-clicking after a lost response is
   safe: the deploy carries a stable `Idempotency-Key` derived from
   `{slug, listingId}`, so Paperclip replays the SAME agent rather than minting
   a second one — the retry just re-runs the profile write onto it. What IS
   gone is the install RECORD (the `.template` sidecar lived on EFS), so the
   "already installed" skip and the sidebar's installed-templates filter are
   inert: the UI will not tell you a listing is already deployed. Read the
   roster to know what you have, not the listing page.
8. **Re-pair the desktop app** if used — the containers row's device trust went
   with step 4.

**Multi-member org caveat:** PaperclipCompany is keyed per-user (NOT per-org),
so an org with N members has N DDB rows all pointing at the same `company_id`.
Each member also has their own Better Auth user record in Aurora. The cleanup
MUST remove every member's row in both places — leaving any one of them blocks
re-provision (the surviving PaperclipCompany row short-circuits
`provision_org`'s idempotency check, so nobody's roster is re-seeded).
