# Apps backend (rail changes + the apps path) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any builder driving `lightbulb app` (a workspace agent or a developer's own Claude Code, through the same routes) can send an app's source to the backend and get back, through the existing Stripe Projects rail, a gated preview on the company's own Neon and Vercel, then publish, undo and remove it; Canvas can list, open and share the app.

**Architecture:** Six changes inside the existing rail (`provisioning_workflow.py`, `stripe_provisioning.py`, `provisioning_landing.py`), then one new service module (`apps_service.py`) that orchestrates the rail, a Vercel client, Route53 and S3, behind one router. No credential ever leaves the backend: the agent sends source and reads state; the backend holds the revealed Vercel and Neon credentials under the app's own Secrets Manager key and pushes them to Vercel.

**Tech Stack:** Python 3 / FastAPI backend in `apps/backend`, httpx with `MockTransport` tests, DynamoDB via `core/dynamodb.py` + moto, Secrets Manager via `integration_secret_store`, boto3 S3 and Route53, CDK in `apps/infra`. Tests: `cd apps/backend && uv run --no-sync pytest -q <paths>`.

**Spec:** `docs/superpowers/specs/2026-10-28-apps-design.md` (third draft, approved 29 Oct; §2, §3, Appendix A and B bind this plan). Spike facts: `docs/research/2026-10-29-apps-spike-revealed-keys.md`. Line numbers below are on `origin/main` c825a2f0a; re-check before editing.

This is plan 1 of the build (after plan 0, the baseline eval). Plan 2 (`2026-10-29-apps-2-cli-starter-harness.md`) builds `lightbulb app`, the starter and the builder harness; plan 3 (`2026-10-29-apps-3-canvas-skill.md`) builds Canvas and the skill. Both consume only the API contract below. The company-tools gateway (spec §4) is not in this plan: it waits for a real request.

## Global Constraints

- Stripe calls go through `core/services/stripe_provisioning.request` only, with `Stripe-Version: 2026-09-30.preview`; never from a route handler, a browser or an agent (spec §6).
- Every write to Stripe keeps the rail's order: persist intent → write → patch ids; unknown outcome → `requires_review`; no automatic replay (rail today).
- `livemode` is always sent explicitly on resource create; the sandbox test service needs `livemode: true` (spec §6).
- One Stripe Project per app, named `"{app name}"`; its id lives on the app record, never on the billing row.
- Each app gets three resources: `vercel/project` (environment `prod`), `neon/postgres` for live (environment `prod`), and a second `neon/postgres` for preview (environment `dev`): Stripe's way (founder ruling). The builder never writes the live database.
- App credentials land only in Secrets Manager under provider key `apps/{app_id}/{environment}` and are pushed to Vercel; never into any agent profile `.env`, log, card payload or API response (spec §6, §6a).
- Revealed names (spike, 29 Oct): Neon `connection_string` → `DATABASE_URL`; Vercel `token`, `org_id` (sent as `teamId`), `project_id`; the token is scoped to that one project.
- Vercel targets: `production` (live database) and `preview` (preview database). New Vercel projects ship with `ssoProtection: all_except_custom_domains`; it is turned off, the door replaces it.
- The first preview never waits for the database: it deploys as soon as Vercel is active, with `LIGHTBULB_SAMPLE_DATA=1` and no `DATABASE_URL`, and redeploys when the preview database is active.
- Undo (`rollback`) is code only. Lightbulb takes no database backups: backups and restores are Neon's, on the company's own account (founder ruling, 29 Oct).
- The app's address is `{name}.{APPS_DOMAIN}` with `APPS_DOMAIN` default `apps.thelightbulb.company` (spec §4).
- The pass is a compact EdDSA JWS signed with the network proof key, claims `{iat, exp, sub, name, email, ws, room, app}`, life 900 s (spec §4).
- App names are DNS labels: `^[a-z][a-z0-9-]{1,38}[a-z0-9]$`, unique per workspace.
- Sharing (`open_to`): `room` | `workspace` | `invited` | `public` (spec §3).
- All new code follows the rail's test style: moto for DynamoDB, `httpx.MockTransport` for vendors, `stripe_provisioning._raw` patched for Stripe.

**Who pays for what:** the company pays Vercel and Neon (free tiers by default) through Stripe Projects. The builder's model tokens are ordinary agent usage: in a workspace they are metered and capped by the existing Budgets (Settings → Budgets, warn at 80%, pause at the cap); a developer's own Claude Code pays for itself. No new metering in this plan. Our own costs are small: S3 for source archives (deleted with the app) and the eval runs (plan 0, capped at $400 a suite).

## API contract (consumed by plans 2 and 3)

Builder routes, prefix `/api/v1/apps/build`. One path for every builder (founder ruling, 29 Oct): a call is accepted with **either** a workspace agent's hook token (`Authorization: Bearer <hook token>` + `X-Lightbulb-Owner: <owner_id>`, checked with `container_auth._require_hook_token(owner_id, request)`) **or** a signed-in computer's proof from `lightbulb login` (`X-Lightbulb-Proof/Challenge/Sig`, checked with `network_login.computer_identity(request)`, owner = the proof's workspace; a person in no workspace gets 409 `workspace_required`). Both resolve through one dependency `builder_caller(request) -> BuilderCaller(owner_id, actor_id, actor_kind: "agent"|"person")` in `routers/apps.py`.

| Route | Body | Returns |
|---|---|---|
| `POST /source` | multipart: `name`, `room_id`, `commit`, `archive` (tar.gz of the tree, no `node_modules`/`.git`) | `{"app_id", "source_id"}` |
| `POST /up` | `{"name","room_id","services":[...],"open_to","budget_usd","source_id","origin": {"kind","ref"?},"issue_id"?}` (the actor comes from `builder_caller`)` | `UpResult` |
| `POST /{app_id}/publish` | `{"source_id","reason"?}` | `UpResult` with `version` |
| `POST /{app_id}/rollback` | `{"version"?: int}` | `UpResult` (code only; `error.next` says data is unchanged) |
| `POST /{app_id}/destroy` | `{}` | `{"state":"awaiting_owner","next":"the owner removes it from Canvas"}` (day one: destroy is the owner's typed-name action, below) |
| `GET /{app_id}/status` | | `AppStatus` |
| `GET /{app_id}/errors` | | `{"client":[{"at","message"}],"server":[{"at","message"}]}`: the last 50 lines from the app's `/api/_lightbulb/errors` stdout and Vercel runtime logs (`GET /v3/deployments/{id}/events`), secrets redacted |

`UpResult = {"app_id", "state": "awaiting_approval"|"provisioning"|"building"|"ready"|"failed", "cards": [{"workflow_id","provider","service","state"}], "preview_url"?, "live_url"?, "canvas_url", "version"?, "sample_data": bool, "cost_cents"?, "error": {"code","message","next"}?}`. `cost_cents` is the build's model cost when the CLI reports it, so the builder can post it with the link. `up` and `publish` are idempotent: a second call continues from the app's current state. Both accept `approved_destructive: "<card id>"`; when the source contains a migration plan 2's gate classified as destructive, the backend refuses unless that id is an approved Paperclip card for this owner (`shared_admin_client().get_approval`). The `name` the agent sends is the DNS label.

`AppStatus = {"app_id","name","room_id","state","open_to","budget_usd","preview_url","live_url","canvas_url","live_version","origin":{"kind","ref"},"versions":[{"n","commit","deployment_id","at","by"}],"resources":[{"provider","service","state"}],"last_opened_at","spend_month_cents"}`. `canvas_url` = `{workspace web base}/chat?panel=canvas&app={app_id}` (the same web base `company_stripe.py` uses for Account Link `return_url`); `UpResult` carries it too, so a builder can post one link.

Member routes (Clerk, `get_current_user`), prefix `/api/v1/apps`:

| Route | Who | Returns |
|---|---|---|
| `GET /` | any org member | `{"apps":[{AppStatus minus versions, "can_open": bool, "room_name", "created_by_name", "agent_name"}]}` (names resolved server-side so Canvas renders rows without extra calls) |
| `POST /{app_id}/open` | member allowed by `open_to` | `{"url": "https://{name}.{APPS_DOMAIN}/_lightbulb/session#token=<pass>"}`; also for `?target=preview` the preview deployment URL |
| `PATCH /{app_id}` | org admin | `{"open_to"?, "budget_usd"?}` → `AppStatus` |
| `POST /{app_id}/invites` | org admin | `{"emails":[...]}` → each gets a one-time link to this app only (`open_to` becomes `invited`) |
| `DELETE /{app_id}` | org admin | body `{"confirm_name"}` must equal the app name; removes every resource through the rail's existing remove path, the DNS records, and marks the app `destroyed` |
| `GET /issuer` | public | `{"keys":[public_key()]}` |

## File structure

- Modify `core/services/stripe_provisioning.py`: `create_resource(..., environment)`, new `update_limit`.
- Modify `core/services/provisioning_workflow.py`: app-aware `_project_for`, pair check, catalog re-read before POST, limit after approval, no `.env` landing for app rows.
- Modify `core/services/provisioning_landing.py`: `land_credentials(..., land_in_profiles)`.
- Modify `core/repositories/provisioning_workflow_repo.py`: `app_id`, `environment` fields.
- Modify `routers/company_provisioning.py`: room post after approval.
- Create `core/repositories/apps_repo.py`: the app record.
- Create `core/services/vercel_client.py` (thin httpx client) and `core/services/apps_db.py` (resets the preview database to migrations + seed).
- Create `core/services/apps_dns.py`: Route53 CNAME/TXT for `{name}.{APPS_DOMAIN}`.
- Create `core/services/apps_source.py`: S3 archive store + file iteration.
- Create `core/services/apps_service.py`: `up`, `publish`, `rollback`, `destroy`, `status`, `open`.
- Modify `core/services/network_proof.py`: `mint_app_pass`.
- Create `routers/apps.py`; register in `main.py`.
- Modify `apps/infra/lib/stacks/database-stack.ts` (table), `service-stack.ts` + `app.ts` (`APPS_DOMAIN`, `APPS_HOSTED_ZONE_ID`, Route53 and S3 policy).
- Modify `core/services/daily_digest.py`: the 30-day idle line.

## Review Focus

1. An approval clicked twice, or approved on the phone while the agent's `up` is polling: `on_approved` must be a no-op the second time and the room line must post once (Task 6 test `approval_posts_once_per_workflow`).
2. An app name that collides with another workspace's app: the domain `{name}.{APPS_DOMAIN}` is global, so uniqueness is global, not per workspace (Task 7 test `name_is_unique_across_owners`).
3. The Vercel build fails (bad migration, type error): `up` returns `state: "failed"` with Vercel's `errorMessage`, keeps the previous live deployment untouched, and never leaves a half-made version record (Task 12 test `failed_build_leaves_versions_unchanged`).
4. A preview whose sample data no longer matches a new schema (a column added by the change): the preview database is reset to the migrated schema plus the app's seed on that `up`, never left half-migrated (Task 12 test `test_preview_reseeds_after_a_schema_change`).
5. A pass minted for a room the user has since left: `open` re-checks membership at mint time and refuses with 403 `not_a_member` (Task 14 test `open_refuses_after_leaving_the_room`).

---

### Task 0: Production spike — done (29 Oct)

Ran on Lightbulb Demo Lab in production; full log `docs/research/2026-10-29-apps-spike-revealed-keys.md`. Facts this plan relies on: Neon reveals `branch_id, branch_name, connection_string, database_name, org_id, project_id` (no API key); Vercel reveals `org_id, project_id, project_link, project_url, token` (token scoped to the one project); file deploys READY in 31 s; env upsert, protection off, custom domain and rollback all work by API. Two rail bugs were fixed in production on the way (#4486, #4488). Leftovers on Demo Lab: Neon `postgres-85a63d`, Vercel project `spike-app`.

### Task 1: `environment` on resource create, and `update_limit`

**Files:**
- Modify: `apps/backend/core/services/stripe_provisioning.py:352-376, 426-451`
- Test: `apps/backend/tests/unit/services/test_stripe_provisioning_client.py`

**Interfaces:**
- Produces: `create_resource(account_id, *, service_ref, provider, project, name, configuration, livemode, catalog=None, environment: str | None = None)`; `async def update_limit(account_id: str, *, max_amount: int, currency: str, recurring_interval: str, provider: str | None = None) -> dict` → `POST /v2/provisioning/payment_profile/update_limit`.

- [ ] **Step 1: Write the failing tests** (patch `stripe_provisioning._raw`, assert the body):

```python
async def test_create_resource_sends_environment_when_given():
    # body["environment"] == "prod"; omitted when environment is None
async def test_update_limit_posts_string_amount_and_provider_override():
    # path endswith "/payment_profile/update_limit"; body["usage_limits"]["max_amount"] == "1000"; body["provider"] == "prvdr_x"
```
- [ ] **Step 2:** Run: `uv run --no-sync pytest -q tests/unit/services/test_stripe_provisioning_client.py -k "environment or update_limit"` → FAIL.
- [ ] **Step 3:** Implement both in `stripe_provisioning.py`; `update_limit` reuses `_usage_limits`.
- [ ] **Step 4:** Run the same command → PASS. Run the whole file → all pass.
- [ ] **Step 5:** Commit: `git commit -m "stripe rail: environment on resource create; update_limit client"`.

### Task 2: Workflow rows know their app; one Stripe Project per app

**Files:**
- Modify: `apps/backend/core/repositories/provisioning_workflow_repo.py:65-101` (fields `app_id: str | None = None`, `environment: str = "prod"` on `create`)
- Modify: `apps/backend/core/services/provisioning_workflow.py:248, 336-346, 393-490, 732-777, 916-936`
- Create: `apps/backend/core/repositories/apps_repo.py` (only `get`/`patch` needed here; full repo in Task 7 — write the module now with the fields in Task 7's Interfaces)
- Test: `apps/backend/tests/unit/services/test_provisioning_workflow.py`, `tests/unit/repositories/test_provisioning_workflow_repo.py`

**Interfaces:**
- Produces: `start(..., app_id: str | None = None, environment: str = "prod")`; `_project_for(account_id, row)` returns the app's project when `row.get("app_id")`: reads `apps_repo.get(app_id)["stripe_project_id"]`, else `api.create_project(account_id, name=f"{app['name']} production", catalog=...)` then `apps_repo.patch(app_id, {"stripe_project_id": ...})` (conditional on absent, loser re-reads); `_for_pair` matches on `(provider_id, service_id, app_id, environment)` (an app has two `neon/postgres` rows, `prod` and `dev`); `_create_resource` passes `environment=row["environment"]`.

- [ ] **Step 1: Write the failing tests:**
```python
async def test_app_row_gets_its_own_project_named_after_the_app():  # create_project called with name "crew-schedule production"; apps_repo row patched; billing row untouched
async def test_two_apps_same_service_do_not_reuse_a_workflow():  # start() for app A then app B with neon/postgres → two rows, neither "reused"
async def test_company_row_still_uses_company_project():  # app_id None → old behaviour, billing row
async def test_create_resource_threads_environment():  # body["environment"] == "prod"
async def test_same_app_can_have_prod_and_dev_databases():  # neon/postgres prod + dev → two rows, neither "reused"
```
- [ ] **Step 2:** Run: `uv run --no-sync pytest -q tests/unit/services/test_provisioning_workflow.py -k "app_row or two_apps or company_row or threads_environment"` → FAIL.
- [ ] **Step 3:** Implement. `_creates_project(owner_id, service, app_id=None)` reads the app record when `app_id`.
- [ ] **Step 4:** Run → PASS; run the full rail suite (seven files in the spec) → 341 + new pass.
- [ ] **Step 5:** Commit: `"stripe rail: one Stripe Project per app; app-scoped workflow pairs"`.

### Task 3: Re-read the catalog immediately before the resource POST

**Files:**
- Modify: `apps/backend/core/services/provisioning_workflow.py:916-936` (`_create_resource`)
- Test: `tests/unit/services/test_provisioning_workflow.py`

**Interfaces:**
- Produces: `_create_resource` calls `_read_catalog(account_id, provider_name, service_id)` and `_drifted(row["approval"], fresh)`; on drift → `_requires_review(row, "catalog_changed")` and no POST.

- [ ] **Step 1: Test** `test_catalog_drift_before_resource_post_parks_for_review` (price text changed between approval and POST → state `requires_review`, `error_code == "catalog_changed"`, `_raw` never called with `/resources`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit `"stripe rail: re-read catalog before the resource write"`.

### Task 4: App credentials never land in agent profiles

**Files:**
- Modify: `apps/backend/core/services/provisioning_landing.py:83-108`
- Modify: `apps/backend/core/services/provisioning_workflow.py:1006-1056` (`_bind`)
- Test: `tests/unit/services/test_provisioning_landing.py`, `test_provisioning_workflow.py`

**Interfaces:**
- Produces: `land_credentials(*, owner_id, resource, env, land_in_profiles: bool = True, secret_provider: str | None = None)`; for app rows `_bind` calls it with `land_in_profiles=False, secret_provider=f"apps/{app_id}/{environment}"` and the pointer carries `level: "app", app_id`. `build_provisioned_env` ignores keys under `apps/` (assert in test).

- [ ] **Step 1: Tests:**
```python
async def test_app_landing_skips_every_profile_and_uses_app_secret_key():  # patch_profile_env_and_config never called; secret name endswith "apps/app_1/prod"
async def test_build_provisioned_env_never_includes_app_secrets():
```
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"stripe rail: app credentials stay in Secrets Manager under the app"`.

### Task 5: Per-provider limit from the app budget

**Files:**
- Modify: `apps/backend/core/services/provisioning_workflow.py:492-562` (`on_approved`)
- Test: `tests/unit/services/test_provisioning_workflow.py`

**Interfaces:**
- Produces: when `row.get("app_id")` and the service is paid and `payment_profile` exists, `on_approved` calls `api.update_limit(account_id, max_amount=budget_usd*100, currency="usd", recurring_interval="month", provider=row["provider_id"])` before transitioning; `budget_usd` read from `apps_repo.get(app_id)["budget_usd"]`. Free services skip it.

- [ ] **Step 1: Tests:** `test_paid_app_resource_sets_provider_limit_from_budget` (`update_limit` called once with `"1000"`, provider id) and `test_free_app_resource_sets_no_limit`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"stripe rail: per-provider limit from the app budget"`.

### Task 6: Approval posts one line into the app's room

**Files:**
- Modify: `apps/backend/routers/company_provisioning.py:623-657` (`approve_workflow`) and `core/services/provisioning_workflow.py:1137-1180` (`_reconcile_approval`)
- Create: `apps/backend/core/services/apps_room.py`
- Test: `tests/unit/routers/test_company_provisioning_owner.py`, `tests/unit/services/test_provisioning_workflow.py`

**Interfaces:**
- Produces: `async def post_approval_line(*, owner_id: str, user_id: str, workflow: dict) -> None` in `apps_room.py`: for rows with `app_id`, reads the app's `room_id`, calls `messaging.create_human_message(AuthContext(user_id=user_id, org_id=owner_id), room_id, CreateMessageRequest(client_message_id=uuid5(NAMESPACE_URL, f"apps-approval:{workflow_id}"), text=f"approved: {provider_name}/{service_name} for {app name}, setting up", ...), via="apps", wake=True)` (pattern `ideas.py:635`). The uuid5 id makes a second call a no-op.

- [ ] **Step 1: Tests:** `test_approval_posts_once_per_workflow` (approve route twice → one message in the fake messaging table, text as above), `test_company_row_approval_posts_nothing`, and in the workflow suite `test_reconcile_approval_posts_line_with_paperclip_user`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: approval posts a wake line into the app's room"`.

### Task 7: The app record

**Files:**
- Create: `apps/backend/core/repositories/apps_repo.py`
- Modify: `apps/infra/lib/stacks/database-stack.ts:339-350` (copy the pattern: `createTable("AppsTable", "apps", {partitionKey: app_id})` + GSI `owner-index` (`owner_id`, `name`) + GSI `name-index` (`name`) for global uniqueness)
- Test: `tests/unit/repositories/test_apps_repo.py` (moto, pattern `test_provisioning_workflow_repo.py:11-37`), `apps/infra/test/database-stack.test.ts`

**Interfaces:**
- Produces: `async def create(*, owner_id: str, name: str, room_id: str, created_by: str, actor_id: str, actor_kind: str, origin: dict, services: list[str], open_to: str, budget_usd: int) -> dict` (item: `app_id="app_"+hex, owner_id, name, room_id, created_by, actor_id, actor_kind, origin, services, open_to, budget_usd, state="building", stripe_project_id=None, vercel={"project_id","team_id","project_url"}|None, neon={"project_id","database_name"}|None, domain=None, versions=[], live_version=None, preview_url=None, live_url=None, last_opened_at=None, created_at, updated_at`); raises `NameTaken` when `name-index` has it (conditional put on a `NAME#{name}` guard item). `get(app_id)`, `get_by_name(owner_id, name)`, `list_for_owner(owner_id)`, `patch(app_id, fields)`, `add_version(app_id, version: dict) -> dict` (append; sets `live_version` when `version["live"]`), `delete(app_id)`.

- [ ] **Step 1: Tests:** `test_create_then_get_round_trips`, `test_name_is_unique_across_owners` (second owner, same name → `NameTaken`), `test_add_version_appends_and_sets_live`, `test_list_for_owner_only_mine`.
- [ ] **Steps 2–5:** FAIL → implement (table + repo) → PASS (`uv run --no-sync pytest -q tests/unit/repositories/test_apps_repo.py` and `cd apps/infra && npx jest database-stack`) → commit `"apps: app record table and repo"`.

### Task 8: Vercel client

**Files:**
- Create: `apps/backend/core/services/vercel_client.py` (copy `link_wallet.py` shape: `TIMEOUT_S = 30`, `_transport`, `_client()`, `class VercelError(Exception)` with `.code`)
- Test: `tests/unit/services/test_vercel_client.py` (`httpx.MockTransport`)

**Interfaces:**
- Produces (every function takes `creds: dict` = the revealed Vercel env, reads `VERCEL_TOKEN`, `VERCEL_TEAM_ID` or `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`; every request adds `?teamId=`):
  - `async def upsert_env(creds, env: dict[str, str], targets: list[str]) -> None` → `POST /v10/projects/{id}/env?upsert=true`, `type: "encrypted"`.
  - `async def upload_file(creds, data: bytes) -> str` → `POST /v2/files` with `x-vercel-digest` sha1, returns the sha.
  - `async def create_deployment(creds, *, name: str, files: list[dict], target: str | None, build_command: str) -> dict` → `POST /v13/deployments?forceNew=1&skipAutoDetectionConfirmation=1` with `project`, `projectSettings: {"framework": "nextjs", "buildCommand": build_command}`; returns `{"id","url","readyState"}`.
  - `async def deployment_state(creds, deployment_id: str) -> dict` → one `GET /v13/deployments/{id}`; returns `{"readyState", "url", "errorMessage"}`. **No waiting inside a request:** like the Stripe rail, the backend checks once per read and the caller (the CLI) polls with backoff.
  - `async def add_domain(creds, name: str) -> dict` → `POST /v10/projects/{id}/domains`; returns `{"verified", "verification": [...], "cname": <from GET /v9/projects/{id}/domains/{name}>}`.
  - `async def verify_domain(creds, name: str) -> bool` → `POST /v9/projects/{id}/domains/{name}/verify`.
  - `async def rollback(creds, deployment_id: str) -> None` → `POST /v1/projects/{id}/rollback/{deployment_id}`.
  - `async def disable_protection(creds) -> None` → `PATCH /v9/projects/{id}` `{"ssoProtection": null, "passwordProtection": null}`.
  - `async def delete_project(creds) -> None` → `DELETE /v9/projects/{id}`.

- [ ] **Step 1: Tests** (one per function, asserting method, path, query `teamId`, body, and the mapped result): `test_upsert_env_posts_encrypted_targets`, `test_upload_file_sends_sha1_digest`, `test_create_deployment_sends_files_and_nextjs_settings`, `test_deployment_state_maps_ready_and_error`, `test_add_domain_returns_verification_and_cname`, `test_rollback_uses_v1_route`, `test_token_never_in_logs` (caplog has no token).
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: Vercel client"`.

### Task 9: Preparing the preview database (no backups)

Founder ruling (29 Oct): Lightbulb takes no database backups; that is Neon's job on the company's own account. The preview database is its own `neon/postgres` resource (environment `dev`) holding the app's sample data.

**Files:**
- Create: `apps/backend/core/services/apps_db.py`
- Test: `tests/unit/services/test_apps_db.py` (one integration test against a local Postgres 18 container when `APPS_DB_IT=1`)

**Interfaces:**
- Produces: `async def reset_preview(preview_url: str, *, migrations: list[str], seed_sql: str) -> None`: `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`, apply every migration in order, run the seed, in one connection with a 120 s timeout (psycopg 3, already a dependency if present, else asyncpg; no `pg_dump`, no Dockerfile change). `class AppsDbError(Exception)` with `.code` (`reset_failed`); the URL never appears in logs or errors.

- [ ] **Step 1: Tests:** `reset_applies_migrations_then_seed`, `reset_is_idempotent`, `errors_never_contain_the_url`; integration `preview_writes_never_reach_live` (two containers).
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: preview database reset"`.

### Task 10: DNS for `{name}.{APPS_DOMAIN}`

**Files:**
- Create: `apps/backend/core/services/apps_dns.py`
- Modify: `apps/backend/core/config.py` (after :445): `APPS_DOMAIN: str = "apps.thelightbulb.company"`, `APPS_HOSTED_ZONE_ID: str = ""`
- Modify: `apps/infra/lib/stacks/service-stack.ts` (:1852 pattern, plus a Route53 `ChangeResourceRecordSets`/`ListResourceRecordSets` policy on the task role scoped to the zone) and `apps/infra/lib/app.ts` (props)
- Test: `tests/unit/services/test_apps_dns.py` (boto3 `Stubber`), `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Produces: `async def point(name: str, *, cname_target: str, verification: list[dict]) -> None` (UPSERT CNAME `{name}.{APPS_DOMAIN}` → `cname_target`, TTL 300; one TXT per `verification` entry of type `TXT` at `verification["domain"]` with `verification["value"]`); `async def remove(name: str) -> None`; `def hostname(name: str) -> str`.

- [ ] **Step 1: Tests:** `test_point_upserts_cname_and_txt`, `test_remove_deletes_both`, `test_hostname_uses_setting`; CDK test asserts the policy names the zone.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: Route53 records for app hostnames"`.

### Task 11: Source archive store

**Files:**
- Create: `apps/backend/core/services/apps_source.py`
- Test: `tests/unit/services/test_apps_source.py` (moto S3)

**Interfaces:**
- Produces: `async def put(owner_id: str, app_id: str, *, commit: str, data: bytes) -> str` (key `apps/{owner_id}/{app_id}/src/{sha256[:16]}.tar.gz`, returns `source_id` = that sha prefix; refuses > 100 MB with `SourceTooLarge`; refuses archives containing `node_modules/`, `.git/`, `.env*` entries with `SourceRefused`); `async def get(owner_id, app_id, source_id) -> bytes`; `def iter_files(data: bytes) -> Iterator[tuple[str, bytes]]` (safe extraction: rejects absolute paths and `..`).

- [ ] **Step 1: Tests:** `test_put_then_get_round_trips_and_ids_by_hash`, `test_refuses_node_modules_and_env_files`, `test_iter_files_rejects_path_escape`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: source archive store"`.

### Task 12: The apps service — `up`, `publish`, `rollback`, `destroy`, `status`

**Files:**
- Create: `apps/backend/core/services/apps_service.py`
- Test: `tests/unit/services/test_apps_service.py` (Stripe `_raw` patched, vendor `MockTransport`s, moto DynamoDB + S3, fake Secrets Manager as in `test_link_wallet.py`)

**Interfaces:**
- Consumes: Tasks 1–11 as named above; `provisioning_workflow.start/advance_on_read`; `integration_secret_store.get(owner_id, f"apps/{app_id}/prod")`.
- Produces:
  - `async def up(owner_id: str, *, name, room_id, services, open_to, budget_usd, source_id, actor: BuilderCaller, origin: dict, issue_id=None) -> UpResult`
  - `async def publish(owner_id: str, app_id: str, *, source_id, actor: BuilderCaller, reason="") -> UpResult`
  - `async def rollback(owner_id: str, app_id: str, *, version: int | None) -> UpResult`
  - `async def destroy(owner_id: str, app_id: str, *, actor: BuilderCaller) -> dict`
  - `async def status(owner_id: str, app_id: str) -> AppStatus`
  - `UpResult` and `AppStatus` as `TypedDict`s matching the contract.

Behaviour `up` (each step idempotent, state kept on the app record):
1. `apps_repo.get_by_name` or `create` (records `origin`).
2. Ensure three workflows: `vercel/project` (prod, configuration `{"name": name}`), `neon/postgres` (prod), `neon/postgres` (dev), each via `provisioning_workflow.start(..., app_id=app_id, environment=...)` plus one `advance_on_read`. Report cards for any row awaiting approval; `failed|requires_review` → `state: "failed"`, `error.next = "open Inbox"`.
3. **As soon as Vercel is active** (the database may still be provisioning): read the app's Vercel secret; `vercel.disable_protection`; `upsert_env` for both targets: `LIGHTBULB_ISSUER_KEY` = the issuer public key as a JWK string `{"kty":"OKP","crv":"Ed25519","x":"<base64url raw key>","kid": network_proof.kid()}` (plan 2's door file parses exactly this), `LIGHTBULB_APP` = name, `LIGHTBULB_OPEN_TO` = open_to, `LIGHTBULB_COOKIE_SECRET` = 32 random bytes base64url generated once per app and kept in the app secret; while the preview database is not active also `LIGHTBULB_SAMPLE_DATA=1` (preview target), and the result says `sample_data: true`.
4. Upload every file from `apps_source.iter_files`, `create_deployment(target=None, build_command="next build")` (preview builds never migrate); store the deployment id as `pending_deployment` and return `state: "building"` at once. Each later `up`/`status` read calls `deployment_state` once: `READY` → patch `preview_url`, clear `pending_deployment`, `state: "ready"`; `ERROR|CANCELED` → `state: "failed"` with Vercel's `errorMessage`.
5. **When the preview database becomes active** (on a later `up` or `status` read), and on every `up` whose source adds a migration: `apps_db.reset_preview(preview_url, migrations=<the source's drizzle/*.sql>, seed_sql=<drizzle/seed.sql>)`; `DATABASE_URL` (preview target) = its `connection_string`; remove `LIGHTBULB_SAMPLE_DATA`; redeploy the preview; `sample_data: false`.

`publish`: requires all three resources active. `n = len(versions)+1`. `DATABASE_URL` (production target) = live `connection_string`. Deploy with `target="production"` and `build_command="npx drizzle-kit migrate && next build"` (the migration gate in plan 2 guarantees add-only migrations) and return `state: "building"` at once; the version is recorded when a later read sees `READY`. First publish only: `add_domain(hostname)` and `apps_dns.point(name, cname_target, verification)` at once, then one `verify_domain` per read until verified; patch `domain`, `live_url`. On READY: `add_version({"n","commit","deployment_id","at","by": actor.actor_id, "live": True})`; `state: "live"`.

`rollback(version)`: **code only.** Target = given `n` or `live_version - 1`; `vercel.rollback(deployment_id)`; `live_version` = target; `error = {"code": "code_only", "message": "Version {n} is live again.", "next": "Data is unchanged. To restore data, use Neon's own restore on the company's account (Canvas links to it)."}`.

`destroy(owner_id, app_id, *, actor)` (builder route): patches `destroy_requested_by = actor.actor_id` and returns `{"state": "awaiting_owner", "next": "the owner removes it from Canvas"}`. `remove(owner_id, app_id, *, confirm_name, user_id)` (owner route): refuses unless `confirm_name == app["name"]`; for each active resource calls the same function the owner route `POST /resources/{provider}/{resource_id}/remove` (`company_provisioning.py:849`) calls; then `apps_dns.remove(name)`, `vercel.delete_project` is NOT called (the Stripe remove deprovisions it); `apps_repo.patch(state="destroyed")`. No new card kind.

`status`: `spend_month_cents` = the sum, for this app's resource ids, of the current-month charges the owner route `GET /charges` (`company_provisioning.py:143`) already reads from Stripe; builder: call that route's service function, filter by `resource_id`.

- [ ] **Step 1: Tests:**
```python
async def test_up_files_one_workflow_per_service_and_returns_cards()
async def test_up_is_idempotent_while_cards_pending()      # second call files nothing new
async def test_no_request_waits_on_vercel()                 # up/publish return building at once; readiness moves only on reads
async def test_up_deploys_preview_when_all_active()         # env upsert for preview then production; deployment target None; preview_url set
async def test_failed_build_leaves_versions_unchanged()     # deployment_state ERROR on a later read → state failed, versions == before
async def test_up_deploys_sample_data_preview_before_the_database_is_active()
async def test_up_switches_preview_to_its_database_when_it_becomes_active()
async def test_preview_reseeds_after_a_schema_change()
async def test_publish_deploys_and_adds_domain_once()
async def test_rollback_is_code_only_and_says_so()
async def test_no_credential_in_any_result_or_log()         # UpResult/AppStatus/caplog contain no DATABASE_URL/VERCEL_TOKEN value
```
- [ ] **Step 2:** Run: `uv run --no-sync pytest -q tests/unit/services/test_apps_service.py` → FAIL.
- [ ] **Step 3:** Implement `apps_service.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `"apps: up, publish, rollback, destroy, status"`.

### Task 13: Routes

**Files:**
- Create: `apps/backend/routers/apps.py`
- Modify: `apps/backend/main.py:843-929` (`app.include_router(apps.router, prefix="/api/v1/apps", tags=["apps"])`)
- Test: `tests/unit/routers/test_apps_agent.py`, `tests/unit/routers/test_apps_member.py` (`TestClient(app)`, hook-token pattern from `test_company_provisioning_container.py`, Clerk override from `test_company_provisioning_owner.py`)

**Interfaces:**
- Consumes: Task 12 functions; `container_auth._require_hook_token`; `_owner_gate`-style org-admin check from `company_provisioning.py:102` for `PATCH`.
- Produces: the routes in the API contract. Error mapping: `WorkflowRefused` → 409 `{code, message}` (reuse `_refusal`), `ProvisioningError` → 502 (reuse `_stripe_failure`), `VercelError/AppsDbError` → 502 `{code, message, next}`, `NameTaken` → 409 `name_taken`, `SourceTooLarge` → 413.

- [ ] **Step 1: Tests:** `test_build_routes_accept_hook_token_or_computer_proof_and_nothing_else`, `test_proof_caller_without_workspace_gets_409`, `test_up_returns_up_result`, `test_status_shape_matches_contract`, `test_list_marks_can_open_by_room_membership`, `test_patch_requires_org_admin`, `test_delete_requires_typed_name_and_removes_resources`, `test_invites_require_admin_and_set_open_to_invited`, `test_errors_route_redacts_secrets`, `test_destructive_source_needs_an_approved_card`, `test_issuer_is_public_and_returns_kid`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: routes"`.

### Task 14: The pass

**Files:**
- Modify: `apps/backend/core/services/network_proof.py:61-83` (add beside `mint`)
- Modify: `apps/backend/core/services/apps_service.py` (`open`)
- Modify: `apps/backend/routers/apps.py` (`POST /{app_id}/open`)
- Test: `tests/unit/services/test_network_proof.py` (add), `tests/unit/routers/test_apps_member.py`

**Interfaces:**
- Produces: `def mint_app_pass(user_id: str, *, name: str, email: str, ws: str, room: str, app: str, now: int | None = None, life_s: int = 900) -> str` (same key, same `kid`, header `{"alg":"EdDSA","kid"}`, claims exactly `{iat, exp, sub, name, email, ws, room, app}`); `async def open(owner_id, app_id, *, auth: AuthContext, target: str = "live") -> str` (URL): checks `open_to`: `room` → `messaging.require_room_member(auth, room_id)`; `workspace` → org membership; `invited` → only via an invite link (below); `public` → no pass, returns the plain URL (the door is off: `LIGHTBULB_OPEN_TO=public`); sets `last_opened_at`; URL = `{live_url or preview_url}/_lightbulb/session#token={pass}`.

Invites: `async def invite(owner_id, app_id, *, emails: list[str], user_id) -> int` stores one invite per email (`apps_repo` sub-item `INVITE#{sha256(email)}` with a random 32-byte token hash and `expires_at` = 14 days) and emails a link `{live_url or preview_url}/_lightbulb/invite#t=<token>` through the existing agent-mail outbound (`apps/infra` agent-mail-outbound stack); `GET /api/v1/apps/invite/{token}` (public) swaps a valid token, once, for a pass with `sub = "invite:"+email`, `name = email`, `room = ""`.

- [ ] **Step 1: Tests:** `test_mint_app_pass_verifies_with_public_key_and_expires_in_900s`, `test_open_refuses_after_leaving_the_room` (403 `not_a_member`), `test_open_public_mode_returns_plain_url`, `test_open_sets_last_opened_at`, `test_invite_token_works_once_and_expires`, `test_invite_pass_is_scoped_to_one_app`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: signed pass and open route"`.

### Task 15: The 30-day idle line

**Files:**
- Modify: `apps/backend/core/services/daily_digest.py` (find the daily entry point; add `await apps_idle.post_idle_lines(owner_id)`)
- Create: `apps/backend/core/services/apps_idle.py`
- Test: `tests/unit/services/test_apps_idle.py`

**Interfaces:**
- Produces: `async def post_idle_lines(owner_id: str, *, now: datetime | None = None) -> int`: for each live app with `last_opened_at` older than 30 days and no `idle_posted_at` in the last 30 days, post into `room_id` via `messaging.post_agent_message` as the app's `actor_id` when `actor_kind == "agent"`, else as the workspace's CEO agent (`lightbulb-facts` roster `is_ceo`): `"{name} ({origin line}) has not been opened in 30 days. Say 'remove {name}' if it's no longer needed."`; patch `idle_posted_at`; returns the count.

- [ ] **Step 1: Tests:** `test_posts_once_per_30_days_for_idle_live_apps`, `test_skips_recently_opened_and_destroyed`.
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `"apps: idle line after 30 days"`.

### Task 16: End-to-end in production (the acceptance test)

No new code. On Lightbulb Demo Lab (its services account exists), from a workspace agent with plan 2's CLI:

- [ ] **Step 1:** `lightbulb app new` + `up` → three cards; approve; the first preview answers within minutes on sample data, before the database is active; a later `up` switches it to the seeded preview database.
- [ ] **Step 2:** Open the preview from Canvas as a room member → signed in, name shown; without a pass → "Open this from Canvas in Lightbulb".
- [ ] **Step 3:** `publish` → `live_url` resolves at `{name}.apps.thelightbulb.company` within 2 minutes.
- [ ] **Step 4:** Add a row in live; change one file; `publish` (version 2); `rollback` → version 1's code is live and the row is still there; Canvas links to Neon's own restore for data.
- [ ] **Step 5:** Invite an outside email; the link opens the app once; `open_to: public` serves it without a pass.
- [ ] **Step 6:** Record timings and differences in `docs/research/2026-10-29-apps-spike-revealed-keys.md`; open the PR with the pr skill. **Do not merge without the founder.**

### Task 17: Payments (last)

The Stripe catalog's `stripe/sandbox` and `stripe/livemode` services hand an app a Stripe key (spec §4). Undocumented: for a company that connected an existing Stripe account, the Project lives on the Projects-only services account.

- [ ] **Step 1:** On Demo Lab, request `stripe/sandbox` for the acceptance app through the rail; record the revealed key names and which account they belong to (`GET /v1/account` with the key, account id only).
- [ ] **Step 2:** Run one test checkout through the acceptance app. Record whether it succeeds or what Stripe requires.
- [ ] **Step 3:** Write the result into the spike log and the design's §4 payments paragraph, and bring the founder the one decision it implies (none, or an extra Stripe onboarding step for connected-account companies).
