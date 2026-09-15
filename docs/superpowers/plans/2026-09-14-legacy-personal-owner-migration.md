# Legacy personal owners → workspaces (2026-09-14)

Working plan. Delete when the cohort is migrated and the residue purged.

## Why

Onboarding went workspace-first on 2026-09-10 (`6de2144e2`, in prod since the
2026-09-14 11:36Z promotion). Every human is now a member of a Clerk organization
("workspace"). Before that, a signup with no org got a **personal** workspace
keyed by their Clerk user id. Those are legacy, and they bite: the
`paperclip-companies` table has one row per user, so a personal owner who later
joins an org has one row that flips between the two contexts. Leonardo's
personal nightly routine woke his personal box, the executor re-ran the personal
seed, minted a third company and stole his org row for three days
(PRs #2619, #2622, #2623 are the code-side fixes).

## Cohort (prod, read 2026-09-14 ~16:00Z; no emails recorded here)

`purge_legacy_personal_workspaces.py` dry run: **39 candidate owner ids**,
8 non-company `paperclip-companies` rows skipped (claims / pending briefs).

| class | count | what they are |
|---|---|---|
| paying, running, no org | 3 | two external customers (last active Sep 12 and Sep 8) and one team member. `subscription_status=active`. **Must not be purged; must be migrated — and urgently: since `96143cea9` ("require workspace context for product APIs", in prod as of the 2026-09-13/14 promotions) every product API 409s a caller with no org, so these three are locked out of their own workspaces today.** |
| dual-tenant | 7 | already in an org (founder org ×4 incl. the bootstrap admin, plus three other orgs); only personal residue remains (a stopped box, an orphan company, a trial row). |
| personal-only, expired `trial_local`, stopped | 28 | signups Jul 28 → Sep 10 who never converted; most never came back after day one. |
| personal-only, `canceled`, no container | 1 | residue only. |

Paperclip: 40 companies total; 4 are referenced by org rows; 33 are referenced
by no surviving row (the purge candidates' own companies plus true orphans such
as Leonardo's `e13f6059…` and `00d38093…`).

Every one of these owners has resources keyed by their `user_…` owner id:
ECS service (name embeds the id), EFS access point + `/mnt/efs/users/<id>`,
Litestream prefix `s3://lightbulb-prod-openclaw-replicas/<id>/`, a Bedrock
application inference profile + IAM role + gate-token row, and rows in the
owner-keyed tables (`containers`, `billing-accounts`, `pending-updates`,
`usage-counters`, `integrations`, `channel-links`, `bedrock-aip-owners`, the
messaging `OWNER#<id>` partition), plus a `paperclip-companies` row whose
agents' adapter service tokens carry `sub=<id>`.

## Approach: migrate by reprovision, carry over what matters

A faithful re-key of every owner-keyed resource is days of work and would run
against paying customers. Instead:

1. **Give every legacy owner a workspace** — `backfill_legacy_owner_workspaces.py`
   creates a Clerk organization per owner (name = their Paperclip company name,
   else first name, else email local part; user is admin). Nothing else moves.
   On their next sign-in `useOnboardingGate` activates the sole org and the
   normal workspace provisioning runs (workspace trial, org container, starter
   roster).
2. **Paying owners (3): carry the runtime over before they switch.** Runbook,
   canary on the team member first:
   - re-key the billing row to the new org id (same Stripe customer +
     subscription; update Stripe metadata `owner_id`), so the org is entitled
     on first poll and nobody is charged twice;
   - stop the personal box, harvest the OAuth seed if provider is ChatGPT
     (`scripts/harvest_oauth_seed.py`), `mv /mnt/efs/users/<user> /mnt/efs/users/<org>`
     and copy the Litestream prefix, so agent workspaces, memory and sessions
     survive;
   - re-point their `paperclip-companies` row at the org (`org_id`), re-mint the
     agents' adapter service tokens with `sub=<org>` (pattern:
     `scripts/backfill_adapter_gateway_urls.py`), so the org's first
     `_ensure_paperclip_for_org` finds an active company and joins rather than
     seeding a new one;
   - let the org's first `/container/status` provision the org container on top
     of the moved directory; verify chat, Teams and billing; then purge the
     personal residue with `purge_legacy_personal_workspaces.py --owner <user>`.
3. **Everyone else: purge the personal side** with the purge script (it skips
   the paying three by construction), then delete the orphan companies with
   `--delete-company` from the post-purge dry run.

## Owner rulings (2026-09-14)

- **Expired-trial owners: workspace only.** They get a Clerk org and start fresh
  with the starter roster; their old Friday/Atlas company and files are purged.
  (They will get a fresh workspace trial on first poll — accepted.)
- **Paying owners: full runtime carry-over** via #2627, canary on the team
  member first, then the two customers.
- **Cap Clerk at one organization per user** (instance setting; was 5, and no
  user held more than one membership when checked). That closes the
  one-row-per-user flip between two orgs without a table re-key.

## Long-term code fixes (in flight)

- #2619 — the picker reads Clerk org membership, never the Paperclip row.
- #2622 — a personal seed can never re-point an org member's row.
- #2623 — `organizationMembership.created` performs the Paperclip join.
- #2626 — no door can create a personal workspace (`ensure_workspace_trial_default`
  403s a `user_` id; `ensure_user_ready` refuses non-org callers first).
- #2624 — the purge script.
- #2625 — the workspace backfill (`--skip-paid` keeps paying owners out until #2627 ran for them).
- #2627 — the paying-owner runtime migration.

## Status (2026-09-15 ~01:00Z)

| step | state |
|---|---|
| Clerk cap to one org per user (prod + dev) | done |
| Leonardo's personal residue + his two orphan companies | done |
| Prod promotion carrying #2619/#2622/#2623/#2626 | done (run 34906557563) |
| Workspace backfill, non-paying owners | done: 29 Clerk orgs |
| Purge of personal residue | done: 37 owners, 0 failures; Paperclip 40 → 9 companies |
| External Review (internal, paying) migration | data moved to `org_3JL4yREh34gkakAVsZ2GKspTdr0`, token row rebound, org container provisioning via the watchdog backstop |
| Two external customers (paying, ChatGPT) | not started — dry runs pass ("Lull Brands", "Admin") |
| Orphan companies "Bottled Fire" `817eb9fd…`, "Savor Social" `32644a40…` | referenced by no row and by no legacy owner; owner to decide |
| Delete the personal legs + this doc | after the two customers |

### Lessons from the External Review run (baked into the scripts since)

- **SSM exec sessions die on inactivity.** The first run moved billing, then died
  in the silent container drain; the drain and replica copy now print heartbeats.
  A half-migrated owner looks unpaid to the purge: never run the purge while a
  migration is mid-flight, and resume with `--org <id>`.
- **The backend task role cannot read the replicas bucket.** The in-task replica
  copy and the OAuth-seed harvest both need admin credentials in the exec
  environment (below). Check the task log group afterwards: no credential
  strings reached it on 2026-09-15.
- **ChatGPT grants are bound to a workspace.** The provider row (`user_id`) and the
  harness row (`user_id#harness`) carry `bound_owner_id`; every consumer reads
  them with `bound_to=<container owner>`. The script now rebinds whichever exist
  to the org, after the harvest (which needs the old binding), and names
  `oauth_connector_user_id` on the org billing row.
- **Clerk:** send a User-Agent, and never send `slug` — this instance has org
  slugs disabled (`organization_slugs_disabled`).
- `dispatch_provision` from a script spawns attempt 0 as a child that dies with
  the session; the Step Functions watchdog still provisions after its 40-tick
  (~40 min) backstop.

## Executing (the auto-mode classifier refuses prod writes from the agent)

Run from the owner's terminal (the `!` prefix in this session works), after
`aws sso login --profile isol8-admin`. The scripts are piped into the backend
task, so they run whatever is on the local branch — no promotion needed.

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
TASK=$(aws ecs list-tasks --cluster lightbulb-prod-container --service-name lightbulb-prod-backend \
  --desired-status RUNNING --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')
run() { B64=$(base64 < "$1" | tr -d '\n'); shift; aws ecs execute-command --cluster lightbulb-prod-container \
  --task "$TASK" --container backend --interactive \
  --command "/bin/sh -c 'cd /app && echo $B64 | base64 -d | uv run python - $*'"; }

# 0. Cap Clerk at one organization per user (prod, then the same with lightbulb/dev/clerk_secret_key)
SK=$(aws secretsmanager get-secret-value --secret-id lightbulb/prod/clerk_secret_key --query SecretString --output text)
curl -s -A lightbulb-ops/1.0 -X PATCH -H "Authorization: Bearer $SK" -H 'Content-Type: application/json' \
  -d '{"max_allowed_memberships":1}' https://api.clerk.com/v1/instance/organization_settings

# 1. Leonardo's residue (already in the founder org): stopped personal box + two orphan companies
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply \
  --owner user_3J8t0Yp76auS131Lk9ov9YCc5We \
  --delete-company e13f6059-6569-4e49-be06-8cb6345dda04 --delete-company 00d38093-a077-4822-92e9-9c78455c1f4b

# 2. Paying owners, one at a time, canary first (team member user_3IvTzqFIQCWp3ck05Prhn2moTTJ,
#    then user_3IEUfhBJrhDbABVjjcKo4ECy4wk and user_3IQ0sBQskW3v7XNiFSd1PIzZSQv).
#    Dry run, apply with provisioning, verify chat/Teams/billing as the user, then purge the residue.
run apps/backend/scripts/migrate_personal_owner_to_workspace.py --owner <user>
# the apply needs S3 read on the replicas bucket, which the task role lacks:
eval "$(aws configure export-credentials --profile isol8-admin --format env)"
aws ecs execute-command --cluster lightbulb-prod-container --task "$TASK" --container backend --interactive \
  --command "/bin/sh -c 'cd /app && AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN sh -c \"echo $(base64 < apps/backend/scripts/migrate_personal_owner_to_workspace.py | tr -d '\n') \
  | base64 -d | uv run python - --owner <user> --provision --apply\"'"
run apps/backend/scripts/purge_legacy_personal_workspaces.py --owner <user> --apply

# 3. A workspace for every other legacy owner (dry run, then apply)
run apps/backend/scripts/backfill_legacy_owner_workspaces.py --skip-paid
run apps/backend/scripts/backfill_legacy_owner_workspaces.py --skip-paid --apply

# 4. Purge the non-paying rest (dry run, then apply), then the orphan companies the run lists
run apps/backend/scripts/purge_legacy_personal_workspaces.py
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply --delete-company <id> [--delete-company <id> ...]
```

Branches to have checked out (or merged) so `run` pipes the right file:
`chore/purge-legacy-personal-workspaces` (#2624), `chore/backfill-legacy-owner-workspaces`
(#2625), `chore/migrate-personal-owner-to-workspace` (#2627). Merging all three to
main first is simplest.
