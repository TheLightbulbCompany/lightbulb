# Admin "Wipe Account" Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A single audited admin action that fully deletes a personal Isol8 account across Paperclip/Aurora, the container (ECS/EFS), Bedrock (inference profile + IAM role), DynamoDB, Metronome, Stripe, and Clerk — with a dry-run preview and a partial-failure-tolerant report.

**Architecture:** An in-backend orchestrator (`core/services/account_wipe.py`) reused by two admin endpoints (`/preview`, execute). New per-domain delete methods; the backend task role gains the destructive AWS perms via a CDK change. Admin UI does preview → type-email-to-confirm → execute → report.

**Tech stack:** FastAPI + boto3 + pytest (backend), AWS CDK (infra), Next.js/React/vitest (frontend admin).

**Spec:** `docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md` — read it; §"complete teardown map" is the authoritative list of what to delete and in what order.

## Global constraints

- **Personal accounts only in v1.** `owner_id == user_id`, one Paperclip company, one Better Auth user. Refuse org owners (`is_org_owner_id` / org context) with a clear error.
- **General across trial + paid** — every step conditional on what exists (read the rows first; skip absent resources). Trial has no Stripe; a bare Metronome customer; one sonnet AIP.
- **Idempotent + partial-failure-tolerant** — every delete treats "already gone" (404 / `NoSuchEntity` / `ResourceNotFoundException`) as success; each step is try/caught and never aborts the others.
- **Never delete shared/CDK resources:** the EFS filesystem, `isol8-<env>-owner-bedrock-base` managed policy (detach only), the shared Cloud Map service, the meter Lambda/tables, other users' rows.
- **Refuse protected Paperclip emails:** `admin@isol8.co`, `admin+paperclip@isol8.co`.
- **Audited** — execute writes an `admin-actions` row (actor, owner, full report).
- Backend tests: `cd apps/backend && uv run pytest <file> -v`. Frontend: `pnpm vitest run <file>` (single files, RAM-heavy). Commits local, never push.
- Implementers: **verify every reused signature against the code before calling it** — the spec cites file:line from a research pass; confirm it's current.

## File structure

- Create `apps/backend/core/services/account_wipe.py` (orchestrator + WipeReport/WipeStep).
- Modify `apps/backend/core/billing/bedrock_aip.py` (+`delete_owner_aips`, `delete_owner_bedrock_role`).
- Modify `apps/backend/core/services/clerk_admin.py` (+`delete_user`).
- Modify `apps/backend/core/billing/metronome_client.py` (+`archive_customer`).
- Modify `apps/backend/core/services/billing_service.py` (+`delete_stripe_customer`).
- Create `apps/backend/core/services/paperclip_user_delete.py` (Aurora `DELETE FROM "user"` helper, extracted from `scripts/recover_orphan_paperclip_member.py`).
- Modify `apps/backend/routers/admin.py` (+2 endpoints).
- Modify `apps/infra/lib/stacks/service-stack.ts` (task-role IAM perms) + `apps/infra/test/service-stack.test.ts`.
- Frontend admin: a new component + wiring on the admin user view.
- Tests colocated per backend/frontend conventions.

---

### Task 1: Bedrock delete methods

**Files:** Modify `apps/backend/core/billing/bedrock_aip.py`; Test `apps/backend/tests/unit/billing/test_bedrock_aip_delete.py`.

**Interfaces produced:**
- `delete_owner_aips(owner_id: str) -> list[str]` — for each AIP ARN in the owner's `bedrock_aip_arns` (read from the containers row via the existing repo/helper the module already uses), call `bedrock.delete_inference_profile(inferenceProfileIdentifier=arn)` and delete the `bedrock-aip-owners` reverse row (PK `aip_arn`). Idempotent (swallow `ResourceNotFoundException`). Returns the ARNs it removed.
- `delete_owner_bedrock_role(owner_id: str) -> None` — role name via the existing `_owner_role_name(owner_id)`; `iam.delete_role_policy(RoleName, PolicyName="owner-bedrock")` (swallow `NoSuchEntity`), `iam.detach_role_policy(RoleName, PolicyArn=<base arn>)` (swallow), `iam.delete_role(RoleName)` (swallow `NoSuchEntity`). Never delete the base managed policy.

**Implementation notes:** reuse the module's existing boto3 clients + naming helpers (`_aip_name`, `_owner_role_name`, the base policy ARN constant, the `bedrock-aip-owners` table accessor). Read the module fully first — the create/revoke paths show the exact client + constant names.

- [ ] **Step 1: failing tests** — moto or mock the bedrock/iam/ddb clients. Assert `delete_owner_aips` calls `delete_inference_profile` per ARN + deletes each reverse row, and returns `[]` (no crash) when the containers row has no `bedrock_aip_arns`. Assert `delete_owner_bedrock_role` calls delete-role-policy → detach → delete-role in order and swallows `NoSuchEntity`. (Mirror the existing `tests/unit/billing/test_bedrock_aip.py` mocking style.)
- [ ] **Step 2:** run → fail (methods undefined).
- [ ] **Step 3:** implement both methods reusing the module's clients/helpers.
- [ ] **Step 4:** run → pass; also run `tests/unit/billing/test_bedrock_aip.py` to confirm no regression.
- [ ] **Step 5:** commit `feat(wipe): bedrock delete_owner_aips + delete_owner_bedrock_role`.

---

### Task 2: Clerk / Metronome / Stripe / Paperclip-Aurora delete methods

**Files:** Modify `clerk_admin.py`, `metronome_client.py`, `billing_service.py`; Create `core/services/paperclip_user_delete.py`; Tests colocated (`tests/unit/services/test_clerk_admin_delete.py`, `tests/unit/billing/test_metronome_archive.py`, `tests/unit/services/test_billing_delete_stripe.py`, `tests/unit/services/test_paperclip_user_delete.py`).

**Interfaces produced:**
- `clerk_admin.delete_user(user_id: str) -> None` — `DELETE {_CLERK_API_BASE}/users/{user_id}` with the existing bearer auth the module uses; treat 404 as success. Mirror an existing `clerk_admin` mutation (e.g. `ban_user`) for the request/auth pattern.
- `metronome_client.archive_customer(customer_id: str) -> None` — `POST /v1/customers/archive` body `{"id": customer_id}`; treat already-archived / not-found as success. Mirror `create_customer`.
- `billing_service.delete_stripe_customer(owner_id: str) -> bool` — read the billing row; if no `stripe_customer_id`, return False (nothing to do). Else cancel `stripe_subscription_id` (if present) then `stripe.Customer.delete(stripe_customer_id)`; treat missing-resource as success. Returns True if it deleted a customer.
- `paperclip_user_delete.delete_paperclip_user(*, paperclip_user_id: str, email: str, env: str) -> None` — RDS Data API `DELETE FROM "user" WHERE id = :id` (cascades account/session). **Refuse** the bootstrap emails (`admin@isol8.co`, `admin+paperclip@isol8.co`). Extract the cluster ARN / secret / DB name + Data API call from `scripts/recover_orphan_paperclip_member.py` (read it first — it has the exact RDS Data API wiring and the refuse-list).

- [ ] **Step 1: failing tests** — mock httpx/stripe/boto3-rds-data. Assert: clerk delete hits `DELETE /users/{id}` + swallows 404; metronome archive posts the id; stripe delete no-ops when no customer id and cancels+deletes when present; paperclip user-delete issues the DELETE SQL and **raises** for a bootstrap email.
- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement all four (each in its module).
- [ ] **Step 4:** run all four test files → pass.
- [ ] **Step 5:** commit `feat(wipe): clerk/metronome/stripe/paperclip-aurora delete methods`.

---

### Task 3: The orchestrator (`account_wipe.py`)

**Files:** Create `apps/backend/core/services/account_wipe.py`; Test `apps/backend/tests/unit/services/test_account_wipe.py`.

**Interfaces produced:**
- `@dataclass WipeStep{ system: str, action: str, status: Literal["ok","skipped","failed","would_delete"], detail: str = "", error: str | None = None }`
- `@dataclass WipeReport{ owner_id: str, steps: list[WipeStep], dry_run: bool }` + `def to_dict(self) -> dict`.
- `async def preview_wipe(owner_id: str) -> WipeReport` — read-only enumeration: check each system for what exists, emit `would_delete`/`skipped` steps. NEVER deletes.
- `async def wipe_account(owner_id: str, *, actor_admin_id: str) -> WipeReport` — the ordered teardown (spec §teardown map, steps 1–12). Each step try/caught → append `ok`/`skipped`/`failed`. Reads all identifier rows (containers/paperclip/billing) up front. **Refuses org owners** (raise a typed error the router turns into 400).

**Implementation notes:** import and call the reused + new methods (Tasks 1–2 + existing `ecs_manager.delete_user_service`, `get_workspace().delete_user_dir`, `usage_repo.delete_all_for_owner`, `PaperclipAdminClient.delete_company` via the admin-session helper, `billing_repo.delete`, `paperclip_repo.delete`, `user_repo.delete`). Verify each signature before calling. Order + conditionality per the spec table.

- [ ] **Step 1: failing tests** — patch every collaborator (each delete method) with AsyncMock/Mock. Assert `wipe_account`: (a) calls the steps in the spec's order; (b) a collaborator raising leaves that step `failed` but the LATER steps still run (partial-failure tolerance — the key property); (c) skips Stripe when the billing row has no `stripe_customer_id`; (d) org owner → raises. Assert `preview_wipe` calls NO delete method (only reads) and returns `would_delete`/`skipped` steps.
- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement the orchestrator.
- [ ] **Step 4:** run → pass.
- [ ] **Step 5:** commit `feat(wipe): account_wipe orchestrator (preview + execute, partial-failure-tolerant)`.

---

### Task 4: Admin endpoints

**Files:** Modify `apps/backend/routers/admin.py`; Test `apps/backend/tests/unit/routers/test_admin_wipe.py`.

**Interfaces produced (consumed by the frontend):**
- `POST /api/v1/admin/users/{owner_id}/wipe/preview` → `WipeReport.to_dict()` (all `would_delete`/`skipped`). Admin-gated.
- `POST /api/v1/admin/users/{owner_id}/wipe` body `{ confirm_email: str }` → executes ONLY if `confirm_email` matches the account's email (look it up server-side; 400 on mismatch). Returns the execute report. Writes an `admin-actions` audit row. Admin-gated.

**Implementation notes:** mirror the existing admin-router auth dependency + the `admin-actions` audit-write pattern (read a nearby admin endpoint, e.g. an existing destructive one). The email match is server-side, not trusted from the client.

- [ ] **Step 1: failing tests** — TestClient + admin auth override (mirror existing admin router tests). Assert: preview returns the report + calls no deletes; execute with a matching email runs `wipe_account` + writes an audit row; execute with a mismatched email → 400 and NO wipe; non-admin → 403.
- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement both endpoints + register (already in admin router).
- [ ] **Step 4:** run → pass.
- [ ] **Step 5:** commit `feat(wipe): admin preview + execute endpoints (email-confirm + audit)`.

---

### Task 5: CDK — backend task-role permissions

**Files:** Modify `apps/infra/lib/stacks/service-stack.ts`; Test `apps/infra/test/service-stack.test.ts`.

Grant the backend task role the perms the wipe needs and lacks today. Read the file to find the existing task-role policy statements (it already has ECS + EFS create + bedrock create + DDB); add a statement (or extend) with:
- `bedrock:DeleteInferenceProfile`
- `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:DetachRolePolicy` — scoped to the owner-role name pattern `arn:aws:iam::<acct>:role/isol8-<env>-owner-bedrock-*` where possible.
- `efs:DeleteAccessPoint` (check whether already present from the create grant; add if not).
- `ecs:DeleteService`, `ecs:UpdateService` (confirm present — `delete_user_service` implies they are).

Scope the IAM statements as tightly as the resource ARNs allow (owner-bedrock role name pattern), matching how the create statements are scoped.

- [ ] **Step 1:** add the statements, scoped to the owner-role name pattern.
- [ ] **Step 2:** extend `service-stack.test.ts` — assert the synthesized task-role policy includes `bedrock:DeleteInferenceProfile` and `iam:DeleteRole` (mirror how the test asserts existing statements; use the CDK `Template` assertions already in the file).
- [ ] **Step 3:** run `cd apps/infra && npx jest test/service-stack.test.ts` → pass; run `npx cdk synth` (or the test's synth) to confirm no synth error.
- [ ] **Step 4:** commit `feat(wipe): grant backend task role bedrock/iam/efs delete perms`.

---

### Task 6: Frontend admin UI

**Files:** Create `apps/frontend/src/components/admin/WipeAccountDialog.tsx` (+ wire into the admin user view — find where admin user actions live); Test colocated `__tests__/WipeAccountDialog.test.tsx`.

Flow: a "Wipe account" action (danger-styled) → calls `POST /admin/users/{id}/wipe/preview` → renders the returned steps (what will be deleted) → an input requiring the account's email typed exactly to enable the red "Permanently wipe" button → on click calls `POST /admin/users/{id}/wipe` with `{confirm_email}` → renders the per-step result report. Mirror the admin surface's existing `ConfirmActionDialog` / dialog patterns and `useApi()` conventions.

- [ ] **Step 1: failing test** — mock `useApi`. Assert: opening the dialog fetches + renders the preview steps; the wipe button is disabled until the exact email is typed; clicking it posts `{confirm_email}` and renders the result. (Mirror an existing admin dialog test.)
- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement the dialog + wire the action into the admin user view.
- [ ] **Step 4:** run the test + `npx tsc --noEmit` + `pnpm run lint` → clean.
- [ ] **Step 5:** commit `feat(wipe): admin Wipe Account dialog (preview + confirm + report)`.

---

### Task 7: Verification + dogfood

- [ ] **Step 1:** backend full suite `uv run pytest tests/ -q` → green.
- [ ] **Step 2:** frontend `npx tsc --noEmit` + `pnpm run lint` + the touched test files → clean.
- [ ] **Step 3:** infra `npx jest test/service-stack.test.ts` → green.
- [ ] **Step 4 (dogfood — after merge + deploy, with the user):** on the admin surface, run **preview** for `isol8prodtest`, review the listed resources with the user, then execute with the email confirm. Re-run preview → expect an empty/all-skipped report. This is the first real wipe and the acceptance test.

---

## Self-review notes
- Spec coverage: teardown steps 1–12 → Tasks 1–3; endpoints → Task 4; IAM → Task 5; UI → Task 6; dogfood → Task 7. Org-refusal (spec out-of-scope) → Task 3 raises + Task 4 400s.
- The load-bearing property is Task 3's **partial-failure tolerance** — its test must prove a mid-step failure doesn't block later steps (esp. the Clerk delete).
- Deferred: org-account wipe, Metronome hard-delete, PostHog erasure (spec §out-of-scope).
