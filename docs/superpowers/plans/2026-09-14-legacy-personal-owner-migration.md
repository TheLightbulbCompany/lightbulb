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
| paying, running, no org | 3 | two external customers (last active Sep 12 and Sep 8) and one team member. `subscription_status=active`. **Must not be purged; must be migrated.** |
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

## Decisions needed from the owner

- Expired-trial owners: pre-create the workspace (step 1) **and** carry over
  their old Friday/Atlas company + files (step 2 for all 28), or pre-create the
  workspace only and let them start fresh with the starter roster? Recommended:
  workspace only. They never used the personal one, and the starter roster is
  the product now.
- Paying owners: confirm the canary order (team member → the two customers) and
  whether to notify the two customers before their workspace switches.
- Does a migrated expired-trial owner get a fresh workspace trial? The workspace
  trial row is created by `ensure_workspace_trial_default` on first poll, so the
  default answer is yes unless we pre-write an expired row.

## Long-term code fixes (in flight)

- #2619 — the picker reads Clerk org membership, never the Paperclip row.
- #2622 — a personal seed can never re-point an org member's row.
- #2623 — `organizationMembership.created` performs the Paperclip join.
- fence (branch pending) — a non-org caller with no existing state can never
  create a trial row, a container or a company; the three paying personal
  owners keep being served until migrated.
- #2624 — the purge script.
- Open question: users in **two** orgs would hit the same one-row-per-user
  flip between orgs. Either cap Clerk at one organization per user (instance
  setting + backend invite gate) or re-key `paperclip-companies` per
  (user, org). Not started; needs a ruling.

## Executing (the auto-mode classifier refuses prod deletions from the agent)

Run from the owner's terminal (the `!` prefix in this session works), after
`aws sso login --profile isol8-admin`:

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
TASK=$(aws ecs list-tasks --cluster lightbulb-prod-container --service-name lightbulb-prod-backend \
  --desired-status RUNNING --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')
run() { B64=$(base64 < "$1" | tr -d '\n'); shift; aws ecs execute-command --cluster lightbulb-prod-container \
  --task "$TASK" --container backend --interactive \
  --command "/bin/sh -c 'cd /app && echo $B64 | base64 -d | uv run python - $*'"; }

# 1. Leonardo's residue (already in the founder org): stopped personal box + two orphan companies
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply \
  --owner user_3J8t0Yp76auS131Lk9ov9YCc5We \
  --delete-company e13f6059-6569-4e49-be06-8cb6345dda04 --delete-company 00d38093-a077-4822-92e9-9c78455c1f4b

# 2. Workspaces for every legacy owner (dry run, then apply)
run apps/backend/scripts/backfill_legacy_owner_workspaces.py
run apps/backend/scripts/backfill_legacy_owner_workspaces.py --apply

# 3. Purge the non-paying rest (dry run, then apply), then the orphan companies it lists
run apps/backend/scripts/purge_legacy_personal_workspaces.py
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply
run apps/backend/scripts/purge_legacy_personal_workspaces.py --apply --delete-company <id> [--delete-company <id> ...]
```
