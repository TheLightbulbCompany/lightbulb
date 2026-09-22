# Admin "Wipe Account" Tool — Design

**Status:** Approved to build (decisions locked below)
**Motivation:** There is no way to fully delete a Lightbulb account. The reprovision
skill preserves billing; the dev-reset is all-or-nothing. Cleaning up a single
account (a test signup, an offboarding user, a GDPR erase) means hand-running a
long, error-prone sequence of AWS/Clerk/Aurora commands — and even that misses
resources no code path deletes (the per-owner Bedrock inference profile + IAM
role). This builds a single audited admin action that tears an account down
completely, with a dry-run preview and a partial-failure-tolerant report.

## Locked decisions

1. **In-backend wipe.** The whole teardown runs as one audited backend operation.
   The backend task role gains the destructive permissions it lacks today
   (`bedrock:DeleteInferenceProfile`, `iam:DeleteRole`/`DeleteRolePolicy`/
   `DetachRolePolicy`, `efs:DeleteAccessPoint` if not already granted) via a CDK
   `service-stack` change. Gated behind the admin endpoint + type-to-confirm.
2. **Full billing cleanup.** Archive the Metronome customer; for paid accounts,
   cancel the Stripe subscription + delete the Stripe customer (conditional —
   trial_local has neither).
3. **Dogfood.** First real run is wiping `isol8prodtest` (the test account),
   with the dry-run preview reviewed before executing.

## Scope

- **General** — handles trial AND paid accounts. Every step is conditional on
  what actually exists (read the billing/containers/paperclip rows first; skip
  what's absent).
- **Personal accounts first.** Org accounts have per-member fan-out
  (N Paperclip rows + N Better Auth users). v1 targets **personal** owners
  (`owner_id == user_id`, one Paperclip company, one Better Auth user). Org
  support is a documented follow-up (§ Out of scope).
- Admin-gated + host-gated like the rest of the admin surface; audited to
  `admin-actions`.

## The complete teardown map (verified via code trace)

For a personal owner `OWNER = owner_id = user_id` (Clerk `user_...`). Table names
are prod-form `isol8-prod-<name>`; the code uses `isol8-<env>-<name>`.

**Discover first** (read, don't delete, to gather identifiers + drive the preview):
- `containers` row → `service_name`, `access_point_id`, `bedrock_aip_arns` (map,
  e.g. `{sonnet: arn}`), `bedrock_role_arn`.
- `paperclip-companies` row (PK `user_id`) → `company_id`, `paperclip_user_id`.
- `billing-accounts` row (PK `owner_id`) → `stripe_customer_id`,
  `stripe_subscription_id`, `metronome_customer_id` (any may be absent).

**Ordered teardown** (each step independent + try/caught; report per step):

| # | System | Action | Notes |
|---|---|---|---|
| 1 | Paperclip company | `PaperclipAdminClient.delete_company(company_id)` (bootstrap-admin cookie) | Cascades Friday/Atlas + threads. 404 = already gone (ok). |
| 2 | Paperclip Aurora user | `DELETE FROM "user" WHERE id = :paperclip_user_id` via RDS Data API | Cascades `account`/`session`. **Refuse** the bootstrap emails. |
| 3 | ECS service | drain (`desired-count 0`) then `delete-service --force` | Reuse `ecs_manager.delete_user_service`. Cloud Map auto-deregisters. |
| 4 | EFS workspace dir | `rm -rf /mnt/efs/users/{OWNER}` | Backend has EFS mounted — delete directly via `Workspace.delete_user_dir`. |
| 5 | EFS access point | `efs.delete_access_point(access_point_id)` | Must run AFTER the ECS task fully stops (else "in use"). |
| 6 | Bedrock AIP(s) | `bedrock.delete_inference_profile(arn)` for each tier in `bedrock_aip_arns` | **No code deletes this today.** New method. |
| 7 | Bedrock reverse rows | delete `bedrock-aip-owners` row per AIP ARN (PK `aip_arn`) | Keyed by ARN, not owner — use the ARNs from step 6. |
| 8 | IAM owner role | delete inline `owner-bedrock` → detach base managed → `delete-role` | Role `isol8-prod-owner-bedrock-{OWNER}`. **No code deletes this today.** Never delete the shared base managed policy. |
| 9 | Metronome | `POST /v1/customers/archive` (customer id from billing row) | New method. Skip if no `metronome_customer_id`. |
| 10 | Stripe | cancel `stripe_subscription_id` + `Customer.delete(stripe_customer_id)` | Skip entirely if absent (trial_local). |
| 11 | DynamoDB rows | delete: `usage-counters` (all periods via `usage_repo.delete_all_for_owner`), `containers`, `paperclip-companies`, `billing-accounts`, `users`, and conditionally `channel-links` / `integrations` / `connections` / `oauth-tokens` / `api-keys` if present | Do these AFTER the infra reads (steps rely on containers/paperclip/billing rows). Self-expiring tables (webhook-dedup, ws-connections, oauth-state, hook-events, metronome-fast-gate) are left to TTL. |
| 12 | Clerk user | `DELETE https://api.clerk.com/v1/users/{OWNER}` | **Last, decisively** — leaving it lets everything regrow on the next `/container/status` poll. New `clerk_admin.delete_user`. |

Ordering constraints that matter: **read all identifier rows before deleting any
DDB rows** (steps 6–11 need `containers`/`paperclip`/`billing` values); **EFS
access point (5) after ECS drain (3)**; **Clerk (12) last**.

## Architecture

**`core/services/account_wipe.py`** — the orchestrator.
- `preview_wipe(owner_id) -> WipeReport` — read-only: enumerate every resource
  that exists for the owner (does not delete). Drives the UI preview.
- `wipe_account(owner_id, *, actor_admin_id) -> WipeReport` — execute the ordered
  teardown. Each step wrapped in try/except; a failure records `status="failed"`
  with the error and **continues** (partial-failure-tolerant — a Paperclip
  timeout must not block the Clerk delete). Returns the structured report.
- `WipeReport` — a list of `WipeStep{ system, action, status: "ok"|"skipped"|"failed"|"would_delete", detail, error? }`. The same model powers both preview (all `would_delete`/`skipped`) and execute (`ok`/`skipped`/`failed`).
- Idempotent: safe to re-run after a partial failure — every delete treats
  "already gone" (404 / NoSuchEntity / ResourceNotFound) as success.

**New delete methods** (each lives with its domain, reused by the orchestrator):
- `core/billing/bedrock_aip.py`: `delete_owner_aips(owner_id)` (delete each AIP +
  its reverse row), `delete_owner_bedrock_role(owner_id)` (inline → detach → role).
- `core/services/clerk_admin.py`: `delete_user(user_id)`.
- `core/billing/metronome_client.py`: `archive_customer(customer_id)`.
- `core/services/billing_service.py`: `delete_stripe_customer(owner_id)` (cancel
  sub + delete customer; no-op if absent).
- Paperclip Aurora user delete: extract a reusable helper from the
  `recover_orphan_paperclip_member` SQL (RDS Data API `DELETE FROM "user"`),
  refusing the bootstrap emails.
- Reuse as-is: `ecs_manager.delete_user_service`, `Workspace.delete_user_dir`,
  `usage_repo.delete_all_for_owner`, `PaperclipAdminClient.delete_company`,
  `billing_repo.delete`, `paperclip_repo.delete`, `user_repo.delete`.

**Endpoints** (`routers/admin.py`, admin-gated):
- `POST /admin/users/{owner_id}/wipe/preview` → `WipeReport` (all `would_delete`).
- `POST /admin/users/{owner_id}/wipe` with body `{ confirm_email: str }` →
  executes only if `confirm_email` matches the account's email; returns the
  execute `WipeReport`. Writes an `admin-actions` audit row (actor, owner, the
  report).

**Frontend** (admin surface):
- A "Wipe account" action on the admin user view. Flow: click → calls preview →
  renders the list of what will be deleted → requires typing the account's email
  to enable the red "Permanently wipe" button → calls execute → renders the
  per-step result report. No wipe without the email match.

## Safety model

- **Admin + host gated** (existing admin-surface guards).
- **Two-step + type-to-confirm** — preview always precedes execute; execute
  requires the email match server-side (not just client-side).
- **Audited** — every execute writes `admin-actions` with the actor and the full
  report.
- **Partial-failure-tolerant + idempotent** — no step aborts the others; re-run
  is safe. The report is the source of truth for what remains.
- **Refuses protected targets** — the bootstrap Paperclip emails
  (`admin@isol8.co`, `admin+paperclip@isol8.co`); never deletes shared/CDK-managed
  resources (the base managed IAM policy, the EFS filesystem, the shared Cloud
  Map service, any env-level infra).
- **Never in the wipe path:** the shared EFS filesystem, `isol8-prod-owner-bedrock-base`
  managed policy, the meter Lambda / its tables, marketplace tables of OTHER
  users, any env-scoped resource.

## Out of scope (v1)

- **Org accounts (user-keyed v1 refuses them; workspace wipe now exists — see
  below)** — the user-keyed orchestrator (`preview_wipe` / `wipe_account`)
  still refuses org ids with `org_wipe_unsupported`, and now says why: when a
  member's Paperclip row names a workspace, the runtime skip names the org and
  points at the workspace wipe instead of a bare "no containers row"
  (2026-09-21 incident: a user-keyed wipe of a workspace member missed the
  ORG-keyed runtime while destroying every user-keyed row).
- **Workspace wipe (org-keyed) — SHIPPED 2026-09-22**
  (`core/services/workspace_wipe.py`, `POST /admin/workspaces/{org_id}/wipe/preview`
  + `POST /admin/workspaces/{org_id}/wipe`, audit action
  `account_wipe_workspace`): snapshots by ORG id (containers, billing,
  Paperclip-company set, integrations, gate rows, messaging OWNER partition),
  per-layer selection (runtime/paperclip/messaging/secrets/identity/billing —
  billing preserved by default, armed only with `confirm_billing='DELETE-BILLING'`),
  one shared step registry for preview + execute, stale-preview abort via
  snapshot fingerprint, explicit `confirm_org` match, Clerk org delete last.
- **Metronome hard-delete** — archive only (Metronome has no customer delete).
- **PostHog erasure** — analytics event stream; not part of a functional wipe
  (a GDPR person-delete is a separate concern).
- **Self-expiring rows** — webhook-dedup, ws-connections, oauth-state,
  hook-events, metronome-fast-gate are left to TTL.

## Success criteria

- Dry-run preview lists exactly the resources that exist for a given owner.
- Execute removes every one of them (verified: re-running preview returns an
  empty/all-skipped report), with a per-step report; a forced mid-step failure
  still completes the remaining steps and reports the failure.
- First real run: `isol8prodtest` wiped, preview-then-empty confirmed.
