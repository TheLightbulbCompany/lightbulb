# Phase 1 Task 8b: Paperclip Exit (the live part) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. One task = one PR. Steps use `- [ ]`.

**Goal:** Nothing on a live path calls the Paperclip server, its Better Auth identities or its Aurora database. After that, the Paperclip Fargate stack, both Aurora clusters (dev and prod `paperclip-db`) and the Paperclip secrets can be deleted with no behaviour loss.

**Architecture:** Each thing Paperclip still owns moves to a store we already run. Agents go to `agent_registry` (`AGENT#` rows on the tasks table, `agent_registry_repo.put_new/upsert_fields/mark_deleted` already exist). Workspace facts (name, description, members, starters, status) go to the workspace META row (already on the tasks table via `PaperclipRepo`). Purchase approval goes to the Needs-you Question gate that `effect_doors.require_intent("purchase")` already enforces. Paths reachable only from Paperclip-woken runs or deleted features are deleted, not ported.

**Tech stack:** FastAPI backend (`apps/backend`), Hermes runtime (`apps/infra/hermes`), CDK (`apps/infra`).

**Spec:** Phase 1 plan task 8 (`docs/superpowers/plans/2026-11-07-phase1-prod-switch-and-legacy-deletion.md` on `docs/voice-pivot`), PR #4991 "Kept" list, `apps/backend/tests/unit/test_no_paperclip_imports.py`.

## Global constraints

- Prod is live with real workspaces. No behaviour regressions. Before each PR, run `cd apps/backend && uv run pytest tests/unit -n 2 -q` and paste the result line.
- Existing agent ids stay as they are (they are Paperclip UUIDs, and S3 profiles, rooms and tasks key on them). New agents get `uuid.uuid4()` ids.
- `GET /teams/agents` keeps answering with the same shape, because old mobile builds call it.
- The prod data-move scripts keep working until the lead says they are done: `copy_workspace_meta`, `backfill_agent_registry`, `cancel_paperclip_approvals`, `list_paperclip_webhook_triggers` and the code they use (`PaperclipAdminClient`, `paperclip_admin_session`, the META translation). Their deletion is task 9.
- No `apps/mobile` or `packages/shared` edits.
- Never delete infra, data or secrets. The infra PR is written and reviewed. The founder deploys it.
- Every PR edits `test_no_paperclip_imports.py`. Each PR removes only the lines of the files it cleaned.

## Review focus

1. **A workspace provisioned before this work and one provisioned after it, side by side.** Both must list agents, rename them, mail, text and call. Old rows carry `company_id`; new rows do not. Code must not treat a missing `company_id` as "not provisioned". Tests in tasks 1, 3 and 4.
2. **Agent create while the profile write fails.** The registry row must be rolled back, as Paperclip's create was before. Test in task 1.
3. **Deleting the last root agent.** `ensure_root_ceo_survives` must still return 409, now against the registry roster. Test in task 1.
4. **Workspace name never set** (new workspaces have no Paperclip company). Stripe account creation, the agent mail From name and the team address slug must fall back: META `name`, then the Clerk org name, then the owner id. Test in task 4.
5. **Account or workspace wipe while Aurora is gone.** It must complete with no failed step. Test in task 7.

---

### Task 1: Native agent writes (create, rename, delete, pause) on agent_registry

**Files:**
- Modify: `apps/backend/core/services/agent_registry.py`: add the write functions. Remove `mirror_from_paperclip` and `mirror_delete` once the admin client no longer calls them; keep the script-only `seed_from_paperclip`/`verify`.
- Modify: `apps/backend/routers/teams/agents.py`: `POST /agents`, `GET /agents/{id}`, `PATCH /agents/{id}`, `DELETE`, `pause`, `resume`, `reset-session`, `clear-error`, voice notes. All of them go through the registry. `_company_agent` checks `agent_registry.get(owner, id)`, not Paperclip `companyId`.
- Modify: `apps/backend/core/services/paperclip_agent_lifecycle.py`: `delete_agent_everywhere` and `create_paperclip_for_chat` become registry calls. Rename the file `agent_lifecycle.py`. Drop `prepare_paperclip_agent_delete`'s Paperclip issue/routine reassignment; the tasks table has none.
- Modify: `apps/backend/core/services/runtime_rpc.py` (`_create_agent`, `_mirror_agent_update`, agents.delete), `core/services/voice_calls.py:_rename_agent`, `routers/admin.py:admin_agent_delete`
- Modify: `apps/backend/core/services/hermes_provisioning.refresh_agent_soul`: it already reads `load_agent_roster`, which is the registry. No change unless a test shows otherwise.
- Test: `apps/backend/tests/unit/services/test_agent_registry_writes.py`, plus updates to the existing teams/agents and runtime_rpc tests

**Interfaces (Produces):**
- `agent_registry.create(ws: str, *, name: str, role: str | None = None, title: str | None = None, persona: str | None = None, icon: str | None = None, reports_to: str | None = None, budget_monthly_cents: int = 0, listing: dict | None = None, agent_id: str | None = None) -> dict`: the roster-shaped row (`_roster_row`). `agent_id=None` → `str(uuid4())`. An idempotent replay of the same id in the same ws returns the existing row.
- `agent_registry.update(ws: str, agent_id: str, fields: dict) -> dict`: allowed keys `name, title, role, persona, icon, appearance, reportsTo, budgetMonthlyCents, status`. Returns the roster row. A missing or deleted agent raises `KeyError`.
- `agent_registry.delete(ws: str, agent_id: str) -> None` (soft: `mark_deleted`)
- Rule: an agent's `status` (`active`/`paused`) is display state only. No runtime reads it today (Paperclip pause only stopped heartbeats, which #4967 deleted). `reset-session` and `clear-error` return the agent row unchanged: there is no Paperclip runtime session left to reset.

- [ ] **Step 1: Failing tests**
  - `test_create_mints_uuid_and_lists`: `create("org_1", name="Ana")`, then `roster("org_1")` has one row named `"Ana"`, with a 36-char id.
  - `test_update_renames`: create, `update(ws, id, {"name": "Bo"})`, then `get(ws, id)["name"] == "Bo"`.
  - `test_delete_hides_from_roster`
  - `test_teams_create_rolls_back_registry_when_profile_write_fails`: patch `write_profile_for_agent` to raise. `POST /teams/agents` returns 5xx and the registry has no live row.
  - `test_teams_delete_last_root_is_409`
  - `test_teams_routes_never_call_paperclip`: patch `PaperclipAdminClient.__init__` to raise, then exercise list, create, get, patch, delete, pause, resume, reset-session, clear-error and both voice-notes routes. All of them return 2xx.
  - `test_runtime_rpc_agents_create_update_delete_native`
  - `test_voice_profile_rename_updates_registry`
- [ ] **Step 2: Run** `uv run pytest tests/unit/services/test_agent_registry_writes.py -q`: FAIL.
- [ ] **Step 3: Implement.** Response bodies keep the old keys (`id, name, role, title, status, reportsTo, capabilities, metadata, companyId`), because the web reads them.
- [ ] **Step 4: Run** the full unit suite with `-n 2`: PASS. Remove the `KEPT` lines for files that no longer mention Paperclip.
- [ ] **Step 5: Commit** `feat: agent create/rename/delete/pause native on agent_registry (task 8b.1)`

### Task 2: Hermes profiles without a Paperclip key

**Files:**
- Modify: `apps/backend/core/services/hermes_provisioning.py:1450-1475`: no `ensure_paperclip_api_key`, no `HermesProfileError` for a key
- Modify: `apps/backend/core/services/hermes_profiles.py:419-461,781-828`: remove the `paperclip_api_key` param, `PAPERCLIP_API_KEY`, `PAPERCLIP_API_URL` and `PAPERCLIP_COMPANY_ID`. Write `LIGHTBULB_AGENT_ID=<id>` and keep `PAPERCLIP_AGENT_ID=<id>` (same value) until the image roll in task 9.
- Delete: `apps/backend/core/services/paperclip_api_key.py`, `scripts/migrate_agent_keys_to_rows.py`; `PaperclipRepo.get/put/delete_agent_key`
- Modify (Hermes): `apps/infra/hermes/skills-src/lightbulb/scripts/lightbulb-mail.sh:30`, `lightbulb-text.sh:27` and `lightbulb-post.sh:38`: default the agent id from `${LIGHTBULB_AGENT_ID:-$PAPERCLIP_AGENT_ID}`. `plugins/lightbulb/__init__.py`: delete the Paperclip branches of `request_approval` and `ask_person`, and `_paperclip_call`. A non-task turn returns the existing "ask in your reply" text; #4969 is the draft of this half and needs the lead's ruling. `plugins/memory/agentcore/__init__.py:226 board_snapshot`: delete (the native `tasks_board` covers it). Delete `skills-src/paperclip/`.
- Keep: the wipes' Secrets Manager sweep of old `paperclip-agent-keys` secrets (AWS only; task 7 decides).
- Test: `tests/unit/services/test_hermes_profiles*.py`; Hermes `tests/` (`uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests -q`)

- [ ] **Step 1: Failing tests:** `test_profile_env_has_no_paperclip_key` (`build_profile_env` output has no `PAPERCLIP_API_KEY` and does have `LIGHTBULB_AGENT_ID`); `test_write_profile_for_new_agent_never_mints_key` (admin client patched to raise, write succeeds); a Hermes test that a room-turn `ask_person` makes no HTTP call.
- [ ] **Step 2: Run**: FAIL. **Step 3: Implement.** **Step 4: Run** the backend unit suite with `-n 2` and the Hermes tests: PASS.
- [ ] **Step 5: Commit** `feat: Hermes profiles carry no Paperclip key; Paperclip tool branches removed (task 8b.2)`. The PR body says the change needs a Hermes tag flip, a deploy and `roll_hermes_runtimes` (the lead runs these).

### Task 3: Native workspace provisioning and membership

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py` → `workspace_provisioning.py`. `provision_org` writes the META row (`status=active`, `member_user_ids=[provisioner]`, `onboarding_generation`, `funnel_state`) and seeds `STARTER_ROSTER` with `agent_registry.create`. It writes `starter_agent_ids` and makes no HTTP call. `provision_member` → `repo.add_member`. `archive_member` → `repo.remove_member`. Delete sign-up, invite, `_purge_builtin_agents` and the identity writes.
- Modify: `apps/backend/core/services/user_provision.py:253-580`, `scripts/provision_runner.py:44-158` (step 0 is the native seed), `routers/webhooks.py:559-600,722` (no Paperclip retry enqueue; no identity delete), `core/services/update_service.py:142-200` (drop the provision-retry pass; keep the purge), `core/services/voice_persona.py` (import from the renamed module)
- Test: `tests/unit/services/test_workspace_provisioning.py`

- [ ] **Step 1: Failing tests:**
  - `test_provision_org_native`: admin client patched to raise. After `provision_org("org_1", provisioner="user_1")`, `resolve_owner_row("org_1").status == "active"`, the starter ids equal the registry roster ids, and `"user_1" in member_user_ids`.
  - `test_provision_org_idempotent`: a second call creates no new agents.
  - `test_member_join_and_leave_native`
  - `test_provision_runner_step0_needs_no_paperclip`
  - `test_default_room_and_welcome_after_native_seed`: `ensure_default_room` and `send_owner_welcome` are called, as they are today after a complete seed.
- [ ] **Step 2: Run**: FAIL. **Step 3: Implement**, reusing `PaperclipRepo.put_workspace/add_member/remove_member` and `agent_registry.create` (task 1). **Step 4: Run** the unit suite with `-n 2`: PASS.
- [ ] **Step 5: Commit** `feat: workspace provisioning and membership native (task 8b.3)`

### Task 4: Workspace name, description and agent names native

**Files:**
- Modify: `apps/backend/core/repositories/paperclip_repo.py`: `PaperclipWorkspace.name` and `.description` (META attributes `name` and `description`), plus `set_profile(org_id, *, name: str | None, description: str | None) -> None`
- Modify: `apps/backend/routers/teams/settings.py`: GET returns `{"name", "description"}` from META, falling back to the Clerk org name. PATCH writes META. The body whitelist (`PatchCompanySettingsBody`) is unchanged.
- Create: `apps/backend/core/services/workspace_name.py`: `async def workspace_name(owner_id: str) -> str`. Order: META `name`, then `clerk_admin.get_organization(owner_id)["name"]`, then `owner_id`. It replaces `company_stripe.resolve_company_name`, `agent_email.resolve_company_name` (its callers pass `owner_id`) and `launch_service._suggested_display_name`.
- Modify: `agent_email.resolve_agent_name`, `jev._paperclip_agent` and `agent_mail_owner_send.resolve_apex_ceo_agent_id` read `agent_registry.get`/`roster`. `agent_mail_owner_send:137,196` and `agent_mail_send:180` stop requiring `company_id`.
- Create: `apps/backend/scripts/copy_workspace_names.py`: for every active workspace with a `company_id`, read the Paperclip company name and description with the admin client and `set_profile` them. Flags: `--dry-run` (the default) and `--allow-prod`, as in `copy_workspace_meta.py`. It carries the task-3 ponytail marker.
- Test: `tests/unit/services/test_workspace_name.py`, `tests/unit/routers/teams/test_settings_native.py`, `tests/unit/scripts/test_copy_workspace_names.py`

- [ ] **Step 1: Failing tests:** `test_name_order_meta_then_clerk_then_owner`; `test_settings_get_patch_roundtrip_no_paperclip`; `test_agent_mail_from_uses_registry_name`; `test_team_address_minted_for_workspace_without_company_id`; `test_copy_names_dry_run_writes_nothing`.
- [ ] **Step 2: Run**: FAIL. **Step 3: Implement.** **Step 4: Run** the unit suite with `-n 2`: PASS.
- [ ] **Step 5: Commit** `feat: workspace name/description and agent names native (task 8b.4)`. Before the Paperclip server goes away, the lead runs `copy_workspace_names.py --allow-prod`.

### Task 5: Link purchase approval through the Needs-you Question

**Files:**
- Modify: `apps/backend/core/services/purchases.py`. `request_purchase` no longer files a board card, because the route has already passed `effect_doors.require_intent("purchase")`: a Question the mode asked for was answered, or the mode allowed the purchase. `issueId` is read as the task id; check it with `tasks_repo.get_task(owner, id)` (it must exist in the workspace). `projectId` is the task's project if it has one, else `"none"`. Delete `_issue_project`, `cancel_for_card` and `CARD_MARKER`. The Link approval and the caps (`_policy`) are unchanged.
- Delete: `apps/backend/core/services/board_cards.py`
- Modify: `apps/backend/routers/company_link_wallet.py` only if the field name must change. Keep `issueId` on the wire; the Hermes tool sends it.
- Test: `tests/unit/services/test_purchases*.py`

- [ ] **Step 1: Failing tests:** `test_purchase_on_task_needs_no_paperclip` (admin client patched to raise; returns `needs_owner` or `pending_approval`); `test_purchase_unknown_task_refused` (`issue_required`); `test_caps_still_enforced`.
- [ ] **Step 2: Run**: FAIL. **Step 3: Implement.** **Step 4: Run** the unit suite with `-n 2`: PASS.
- [ ] **Step 5: Commit** `feat: Link purchase approval through the Question gate, no Paperclip card (task 8b.5)`

### Task 6: Delete the paths only Paperclip runs reach

All of these are dead on the super-agent path (audit 7 Nov; file:line in the PR). They are deleted, not ported.
- `routers/messaging.py:1472-1720`: the `/internal/paperclip/hermes/*` facade
- `desktop_execution.py:755-818,958` and `mac_browser.py:250-732`: the `paperclip` run family, plus its `Literal` at `routers/container.py:652`
- `run_analytics.py:174-302`: the paperclip enrichment
- `ideas.py:394-453,539-545,644-687`: issue and project statuses, the dead except, and `resume_after_connect`'s admin sign-in and issue calls
- `inbound_triggers.py:151,264,687-690`
- `company_deploy_service.py`, `company_agent_materializer.py` and `paperclip_credential_rule.py`; move `_find_friday_id` to `agent_mail_owner_send`
- `messaging._issue_creator` and `AgentPostRequest.issue_id`; keep accepting the `--issue` flag in `lightbulb-post.sh` and ignore it
- `onboarding_brief.py:273-297,629-781`: the Paperclip goal, project and session part. Keep the `BRIEF.md` staging. The target agent comes from `agent_registry.get`.
- `routers/billing.py:111-118`: the trial recap counts answered Questions in the window instead of Paperclip approvals
- Test: `tests/unit/test_no_paperclip_routes.py` gains `/internal/paperclip`. Add `test_trial_recap_counts_questions`, `test_brief_staged_without_paperclip` and `test_resume_after_connect_no_signin`.
- [ ] Steps as above: failing tests, then delete, then the full suite with `-n 2`, then commit `chore: delete Paperclip-run-only paths (task 8b.6)`

### Task 7: Wipes without Paperclip or Aurora

**Files:**
- Modify: `account_wipe.py:218,451-470,883-917` and `workspace_wipe.py:456-497,638,1175-1180`. Drop the company delete, the member archive, the Better Auth and Aurora deletes, the bootstrap-email guard and the `paperclip_company` unfinished guard. Keep `remove_member`, the META/identity row deletes and the Secrets Manager key sweep (AWS, not Paperclip).
- Delete: `paperclip_user_delete.py`, `paperclip_sql.py`
- Test: `test_account_wipe_no_paperclip_steps` and `test_workspace_wipe_completes_without_aurora` (the RDS Data API and the admin client patched to raise; no failed step, and the rows are deleted)
- [ ] Steps as above; commit `feat: account and workspace wipes need no Paperclip or Aurora (task 8b.7)`

### Task 8: Remove the /teams BFF session and the Paperclip error handler

Runs after tasks 1, 4 and 6 (no route uses `ctx.session_cookie` any more).
- Modify: `routers/teams/deps.py`: `TeamsContext` without `session_cookie`, `paperclip_user_id` or `company_id`; `main.py:33,433-510` (the `PaperclipApiError` handler and the 401 re-mint); `core/services/activity.teams_context`; `ideas._teams_context`
- Delete: `paperclip_session_cookie.py`, `paperclip_user_session.py`, `paperclip_adapter_config.py`, `paperclip_meta.py`, `paperclip_agent_idempotency.py` and `paperclip_openclaw_pairing.py` (if task 1 left them unused); rename `paperclip_owner_email.py` → `owner_email.py`
- Test: `test_teams_ctx_has_no_session`; `GET /teams/agents` with no cookie returns 200 and sets no `Set-Cookie`.
- [ ] Steps as above; commit `chore: /teams BFF without Paperclip sessions (task 8b.8)`

### Task 9 (after the lead says the data moves are done): delete the client, rename the repo

- Delete: `paperclip_admin_client.py`, `paperclip_admin_session.py`, the four data-move scripts and `copy_workspace_names.py`, `agent_registry.seed_from_paperclip/verify`, and the META translation. Rename `PaperclipRepo` → `WorkspaceRepo` and `PaperclipWorkspace` → `Workspace` (`core/repositories/workspace_repo.py`). Drop the old-row methods (`org_company_ids`, `find_workspace_by_company`, identity methods, `agent_cost_remainder`, `automation_pause*`). Stop writing `PAPERCLIP_AGENT_ID` to profiles (after the Hermes roll from task 2). Remove the `PAPERCLIP_*` settings.
- Test: `test_no_paperclip_imports.py` ends with `KEPT` and `IMPORTERS` empty; `MENTIONS` holds only data names (the `paperclip-companies` table name, until the table is retired).
- [ ] Steps as above; commit `chore: Paperclip exit — client deleted, WorkspaceRepo (task 8b.9)`

### Task 10: Infra PR (written here, deployed by the founder)

- Delete: `apps/infra/lib/stacks/paperclip-stack.ts`; the Paperclip VPC, Aurora cluster and security group in `database-stack.ts:14-60` (and their construction); the Paperclip secrets and KMS grants in `auth-stack.ts` (`paperclipServiceTokenKey`, `paperclipEventsWebhookSecret`); the wiring in `lightbulb-stage.ts`; the settlement Lambda's Paperclip cost post in `openrouter-gate-stack.ts`; the `/__paperclip_proxy__` rule in `network-stack.ts:216`; `AccountWipePaperclipAurora*` in `service-stack.ts:845`; the Paperclip-run alarms P27 `nightly-ceo-fleet-zero`, P24 and W67 and their dashboard rows (lead ruling 7 Nov: the lead disables P27's actions when #5058 reaches prod, until this PR lands). Before removal, the Aurora cluster's `removalPolicy` must be `SNAPSHOT`, so that deleting the stack leaves a final snapshot.
- Keep: the `paperclip-companies` DynamoDB table (identity rows and old attributes; retiring it is a later data task).
- Test: `apps/infra/test/no-paperclip-stack.test.ts`. The prod and dev synth have no `AWS::RDS::DBCluster` and no logical id matching `/Paperclip/` other than `PaperclipCompaniesTable`. Run with `cd apps/infra && npx jest test/no-paperclip-stack.test.ts`.
- [ ] Steps as above; commit `infra: remove Paperclip stack, Aurora and secrets wiring (task 8b.10)`

## Order

1 → 3 (3 seeds through 1's `create`). 2, 4, 5, 6 and 7 are independent of each other and of 1. 8 runs after 1, 4 and 6. 9 runs after the lead's "data moves done" and the Hermes roll. 10 runs after 9 is deployed.

## Founder-only steps (in this order)

1. Run `scripts/copy_workspace_names.py --allow-prod` (and finish the other prod data moves) while the Paperclip server is still up.
2. Hermes tag flip, deploy and `roll_hermes_runtimes` for task 2's runtime half.
3. Deploy tasks 1–9 to prod and walk them: roster, rename, create agent, new workspace sign-up, mail, a purchase ask, a wipe on a test account.
4. Deploy the infra PR (task 10) to dev, check `aws rds describe-db-clusters` shows no `paperclip` cluster, then deploy it to prod. This deletes the Paperclip Fargate stack and both Aurora clusters (final snapshots kept).
5. Delete the secrets: `lightbulb/*/paperclip-agent-keys/*` and `paperclip*` in Secrets Manager, the Paperclip KMS key once it is unused, the `company[-env].isol8.co` DNS records, and later the Aurora final snapshots.
