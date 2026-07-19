# Marketplace Companies PR2 — Deploy Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the "coming soon" 400 into a working one-click deploy that instantiates a whole `format="company"` listing into the owner's Paperclip org + OpenClaw container — agents (as a division under Friday, delegation preserved), skills, identity, and paused routines — with a resumable/rollback-able background job. Standard-plan (≤10 agents) companies only; admin-flagged until the dev E2E passes.

**Architecture:** A `DeployPlan` parser extends the existing `company_package.py`. A `company_deploy_service` orchestrates a topological per-agent loop reusing the exact live-create machinery (`synthesize_openclaw_adapter` → `PaperclipAdminClient.create_agent` → `pair_with_openclaw` → `ensure_operator_device_paired`), then materializes each agent's identity/skills onto EFS + `openclaw.json` via `config_patcher` (WITHOUT the marketplace single-agent containment clamp, so intra-company delegation survives), then creates routines paused. A new `marketplace-company-deploys` DDB table is the job ledger + polling surface. Deploy runs as a background asyncio task.

**Tech Stack:** FastAPI + DynamoDB repos + boto3, AWS CDK (TypeScript) for the table, Next.js 16 for the deploy-progress + routines-enable UI, pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-07-18-marketplace-companies-design.md`. Base branch: `feat/marketplace-companies-pr1-catalog` (PR1) — this is a stacked PR.

## Cost model (locked with user 2026-07-19)

- Routines start **paused** (dormant triggers — `enabled: False`; `create_agent_routines` already does this).
- Every imported agent gets a per-agent **`budget_monthly_cents`** cap (`COMPANY_IMPORT_AGENT_BUDGET_CENTS = 1000` = $10/mo, user-adjustable after).
- Imported agents **inherit the container's default 30m heartbeat** (paid: `isolatedSession`+`lightContext`, bounded). Per-agent heartbeat-off is NOT supported by current code and is an explicit non-goal for PR2 (documented follow-up; the budget cap is the backstop for heartbeat-plus-run cost). Do NOT touch `agents.defaults.heartbeat`.

## Global Constraints

- Stacked PR: branch `feat/marketplace-companies-pr2-deploy-engine` off PR1's HEAD; PR targets the PR1 branch. NEVER push without the user's explicit approval.
- Backend: `cd apps/backend && uv run pytest <target> -v` per task; full `uv run pytest tests/ -q` at the end. Disk runs tight — clean `.pytest_cache`/`.coverage` if ENOSPC; never delete other worktrees.
- Frontend: vitest single-file only (`pnpm vitest run <file>`); `tsc` typechecks tests.
- No PR numbers or bug references in code comments.
- **Adapter synthesis is server-side, always** — `adapter_type`/`adapter_config` come from `synthesize_openclaw_adapter`, NEVER from the package. This is the load-bearing security invariant.
- **Preserve intra-company delegation** — do NOT apply the marketplace `subagents.allowAgents=[self]` / `tools.deny += gateway` containment clamp to company agents. They must be able to delegate per the package `reportsTo` graph.
- **Standard-only in PR2** — only `min_plan == "standard"` (≤10 agents) companies deploy; `ultra_pro` stays blocked (PR3 unlocks it). Enforce server-side.
- **Admin-flagged** — the live deploy path is gated to platform admins (`require_platform_admin`) until the dev E2E passes; the guard flips to the plan-based gate in a final step.
- Friday's agent id is derived as the company agent with `role == "ceo"` (no dedicated helper exists).
- Every dispatch that copies brief-verbatim code must strip any PR-labeled wording before committing (repo rule).
- Commit at the end of every task.

## Key signatures (verified against current code — cite these, don't re-derive)

- `synthesize_openclaw_adapter(*, gateway_url, service_token, user_id, device_private_key_pem=None, agent_id=None) -> dict` — `core/services/paperclip_adapter_config.py:92`. `OPENCLAW_GATEWAY_TYPE = "openclaw_gateway"`.
- `PaperclipAdminClient.create_agent(*, session_cookie, company_id, name, role, adapter_type, adapter_config, title=None, capabilities=None, icon=None, reports_to=None, budget_monthly_cents=0, idempotency_key=None) -> dict` — `paperclip_admin_client.py:603`.
- `PaperclipAdminClient.list_agents(*, session_cookie, company_id) -> list[dict]` — used to find Friday (`role == "ceo"`).
- `pair_with_openclaw(*, owner_id, paperclip_agent, gateway_pool, ip, token, extra_update_fields=None) -> None` — `paperclip_openclaw_pairing.py:463`. `extra_update_fields` accepts ONLY emoji/avatar (strict schema).
- `ensure_operator_device_paired(*, owner_id, paperclip_agent, gateway_token) -> None` — `paperclip_openclaw_pairing.py:305`.
- `resolve_company_id_for_owner(*, owner_id, repo: PaperclipRepo) -> str | None` — `paperclip_openclaw_pairing.py:58`.
- `create_agent_routines(*, admin, session_cookie, company_id, agent_id, routines: list[dict]) -> list[str]` — `agent_routine_provisioning.py:141`; each spec `{title, description, cron}`; creates routine + **dormant** trigger (paused).
- `config_patcher.apply_deploy_aux_mutations(owner_id, *, agent_entry, plugins_patch, mcp_patch, skills_patch)` — deep-merges an agent entry + skills into `openclaw.json` `agents.list` under an fcntl lock (`config_patcher.py:568`). This is how identity/skills land (NOT the RPC).
- `Workspace.extract_tarball_to_workspace(*, user_id, agent_id, tar_bytes)` and `Workspace.write_file` / `write_file_if_missing` — EFS I/O (`core/containers/workspace.py`).
- `delete_paperclip_for_marketplace(*, owner_id, agent_id) -> None` — `paperclip_agent_lifecycle.py:223` (Paperclip-side unwind). `catalog_service.unwind_partial_deploy(owner_id, agent_id)` — EFS/config-side unwind. `is_seeded_agent(*, owner_id, agent_id) -> bool` — `paperclip_openclaw_pairing.py:173` (fails closed).
- Container reachability: `get_ecs_manager().resolve_running_container(owner_id)`; token = `container["gateway_token"]`. Helper pattern in `teams/agents.py:_container_is_reachable` (line 207).
- Background dispatch: `asyncio.create_task(...)` (pattern: `user_provision.py:211`, `container_provision.background_provision`).
- Admin gate: `require_platform_admin(auth)` — `core/auth.py:264` (email ends `@isol8.co` / allowlist).
- Deploy stub to replace: `routers/marketplace_listings.py:345-349`.

---

### Task 1: DeployPlan parser (extend company_package.py)

**Files:**
- Modify: `apps/backend/core/services/company_package.py`
- Test: `apps/backend/tests/unit/services/test_company_package_deployplan.py`

**Interfaces:**
- Consumes: existing `CompanyMeta`, `_frontmatter`, `_child_slugs`, `parse_company_meta`.
- Produces: `parse_deploy_plan(pkg_dir: Path) -> DeployPlan`; dataclasses `DeployPlan`, `PlannedAgent`, `PlannedRoutine`. `DeployPlan.agents` is topologically ordered (CEO/no-reportsTo first, then children). Consumed by Task 4.

`PlannedAgent` fields: `slug: str`, `name: str`, `role: str`, `title: str | None`, `body_md: str` (the AGENTS.md content below frontmatter), `reports_to_slug: str | None` (from AGENTS.md `reportsTo` or the team `manager`), `skills: list[str]`, `capabilities: str | None`. `PlannedRoutine` fields: `agent_slug: str`, `title: str`, `description: str`, `cron: str | None`. `DeployPlan`: `slug`, `agents: list[PlannedAgent]`, `routines: list[PlannedRoutine]`, `ceo_slug: str`.

- [ ] **Step 1: Write failing tests** (`test_company_package_deployplan.py`), against the vendored gstack fixture:

```python
"""DeployPlan parsing for the company deploy engine."""
from pathlib import Path

import pytest

from core.services.company_package import (
    DeployPlan,
    PlannedAgent,
    parse_deploy_plan,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"


def test_parse_gstack_deploy_plan():
    plan = parse_deploy_plan(FIXTURES / "gstack")
    assert plan.slug == "gstack"
    assert plan.ceo_slug == "ceo"
    slugs = [a.slug for a in plan.agents]
    assert set(slugs) == {"ceo", "cto", "qa-engineer", "release-engineer", "staff-engineer"}
    # CEO (no reportsTo) is first — topological order for reports_to wiring.
    assert slugs[0] == "ceo"
    ceo = next(a for a in plan.agents if a.slug == "ceo")
    assert ceo.role == "ceo" and ceo.reports_to_slug is None
    assert ceo.body_md.strip()  # AGENTS.md body is the identity content
    # A non-CEO reports to someone inside the package.
    non_ceo = [a for a in plan.agents if a.slug != "ceo"]
    assert all(a.reports_to_slug is not None for a in non_ceo)
    # reportsTo targets resolve to real package slugs.
    known = set(slugs)
    assert all(a.reports_to_slug in known for a in non_ceo)


def test_children_never_precede_their_manager():
    plan = parse_deploy_plan(FIXTURES / "gstack")
    seen: set[str] = set()
    for a in plan.agents:
        if a.reports_to_slug is not None:
            assert a.reports_to_slug in seen, f"{a.slug} before its manager"
        seen.add(a.slug)


def test_frontmatter_body_split_survives_dashes_in_body(tmp_path):
    # PR1 deferred finding: a body line starting with --- must not truncate
    # the frontmatter parse or leak into it.
    (tmp_path / "COMPANY.md").write_text(
        "---\nname: Dash\nslug: dash\ndescription: d\n---\nintro\n\n---\n\nmore body\n"
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "solo").mkdir()
    (tmp_path / "agents" / "solo" / "AGENTS.md").write_text(
        "---\nname: Solo\nrole: ceo\n---\nSolo body\n---\nsecond section\n"
    )
    plan = parse_deploy_plan(tmp_path)
    solo = next(a for a in plan.agents if a.slug == "solo")
    assert solo.role == "ceo"
    assert "second section" in solo.body_md  # body kept whole, dashes and all
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/unit/services/test_company_package_deployplan.py -v` → ImportError (`parse_deploy_plan` undefined).

- [ ] **Step 3: Implement.** Add to `company_package.py`:
  - A `_split_frontmatter(text) -> tuple[dict, str]` helper that returns `(frontmatter_dict, body)` — find the SECOND `---` line that is its own line starting from line 2 (not `text.find("\n---", 3)`, which the PR1 review flagged as truncating on a body `---`). Reuse it in `_frontmatter` so PR1's parse also gets the fix.
  - `PlannedAgent`, `PlannedRoutine`, `DeployPlan` dataclasses.
  - `parse_deploy_plan(pkg_dir)`: read each `agents/<slug>/AGENTS.md` (frontmatter + body); `reports_to_slug` from the agent frontmatter `reportsTo` (a slug or relative path → basename), else from the owning `teams/<team>/TEAM.md` `manager` (basename of its path), else None; `skills` from frontmatter `skills` (shortnames); `role`/`title`/`name`/`capabilities` from frontmatter. `ceo_slug` = the agent with `role == "ceo"` (fallback: the sole agent with no `reports_to_slug`). Topologically sort (Kahn: managers before reports; ties broken by slug for determinism). Routines from `tasks/<t>/TASK.md` with `recurring: true` and any `.paperclip.yaml` `routines` block, keyed to an agent slug via `assignee`.

- [ ] **Step 4: Run to verify pass** — same command → all pass. Also re-run `tests/unit/services/test_company_package.py` (PR1 tests must still pass after the `_frontmatter` refactor).

- [ ] **Step 5: Commit** — `feat(marketplace): DeployPlan parser for company deploy engine`.

---

### Task 2: Deploy-job table (CDK + repo + config)

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`, `apps/infra/lib/stacks/service-stack.ts`, `apps/backend/core/config.py`
- Create: `apps/backend/core/repositories/company_deploy_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_company_deploy_repo.py`

**Interfaces:**
- Produces: table `isol8-{env}-marketplace-company-deploys` (PK `job_id` S, no sort key); `settings.MARKETPLACE_COMPANY_DEPLOYS_TABLE`; repo funcs `create_job`, `get_job`, `record_agent`, `set_status`. Consumed by Tasks 4/5.

Job row shape: `{job_id, owner_id, listing_id, listing_version, company_slug, status: "running"|"failed"|"completed"|"rolled_back", agents: [{slug, paperclip_id, state: "created"|"paired"|"materialized"|"failed"}], routine_ids: [str], error: str|None, created_at, updated_at}`.

- [ ] **Step 1: CDK table.** In `database-stack.ts`, mirror the marketplace-listings block: add `public readonly marketplaceCompanyDeploysTable: dynamodb.Table;` and instantiate via `createTable("MarketplaceCompanyDeploysTable", "marketplace-company-deploys", { partitionKey: {name: "job_id", type: STRING} })` (PAY_PER_REQUEST, customer-managed KMS — the factory handles it; no GSI needed). In `service-stack.ts`: add the prop, `grantReadWriteData(this.taskRole)`, and the env var `MARKETPLACE_COMPANY_DEPLOYS_TABLE: props.database.marketplaceCompanyDeploysTable.tableName` in the marketplace env block (~line 1254). In `config.py` (~line 228): `MARKETPLACE_COMPANY_DEPLOYS_TABLE: str = os.getenv("MARKETPLACE_COMPANY_DEPLOYS_TABLE", "")`.
  (CDK isn't unit-tested here; correctness is verified by the deploy pipeline. Match the existing table's block exactly.)

- [ ] **Step 2: Write failing repo tests** (`test_company_deploy_repo.py`) — mock `boto3.resource("dynamodb").Table` (follow `marketplace_repo`'s resource-accessor idiom; monkeypatch the table accessor). Assert: `create_job` writes a `running` row with the agent list seeded from the plan slugs in `pending` state and returns the job_id; `record_agent` updates one agent's `paperclip_id`+`state`; `set_status` flips status + `error`; `get_job` round-trips.

- [ ] **Step 3: Run to verify fail** → ImportError.

- [ ] **Step 4: Implement `company_deploy_repo.py`** — accessor `def _table(): return boto3.resource("dynamodb").Table(settings.MARKETPLACE_COMPANY_DEPLOYS_TABLE)`; async funcs via `run_in_thread` like `marketplace_repo`. `create_job(*, owner_id, listing_id, listing_version, company_slug, agent_slugs, now_iso) -> str`; `get_job(job_id) -> dict | None`; `record_agent(*, job_id, slug, paperclip_id, state, now_iso)`; `set_status(*, job_id, status, now_iso, error=None)`.

- [ ] **Step 5: Run to verify pass**; **Commit** — `feat(marketplace): company-deploy job table + repo`.

---

### Task 3: Per-agent materializer (EFS identity + skills, delegation preserved)

**Files:**
- Create: `apps/backend/core/services/company_agent_materializer.py`
- Test: `apps/backend/tests/unit/services/test_company_agent_materializer.py`

**Interfaces:**
- Consumes: `PlannedAgent` (Task 1); `Workspace` (`get_workspace()`); `config_patcher.apply_deploy_aux_mutations`.
- Produces: `materialize_agent(*, owner_id, new_agent_id, planned: PlannedAgent, skill_assets: dict[str, bytes] | None, workspace, apply_aux) -> dict` — writes `workspaces/{new_agent_id}/AGENTS.md` (the planned body) + any skill dirs to EFS, then deep-merges an `agent_entry` into `openclaw.json` `agents.list` via `apply_aux`. Returns the agent_entry written. Consumed by Task 4.

The `agent_entry` MUST include `id=new_agent_id`, `workspace=".openclaw/workspaces/{new_agent_id}"`, `reasoningDefault="stream"`, `skills=<planned.skills>`, and MUST NOT set `subagents.allowAgents`/`tools.deny` containment (the delegation-preserving deviation from `deploy_from_artifact`).

- [ ] **Step 1: Write failing tests** — a fake `workspace` capturing `write_file` calls and a fake `apply_aux` capturing the `agent_entry`/`skills_patch`. Assert: `AGENTS.md` written to `workspaces/{id}/AGENTS.md` with the planned body; `agent_entry.id`/`workspace` correct; `skills` threaded; **no `allowAgents`/`tools.deny` containment keys present**; skill assets written under the agent workspace.

- [ ] **Step 2: Run to verify fail**; **Step 3: Implement**; **Step 4: pass**; **Step 5: Commit** — `feat(marketplace): per-agent company materializer (delegation-preserving)`.

---

### Task 4: Company deploy service (orchestration + resume + rollback)

**Files:**
- Create: `apps/backend/core/services/company_deploy_service.py`
- Test: `apps/backend/tests/unit/services/test_company_deploy_service.py`

**Interfaces:**
- Consumes: everything above + the verified live-create helpers.
- Produces: `async def run_company_deploy(*, job_id, owner_id, listing, tar_bytes) -> None` (background target; never raises — records failure on the job row). Consumed by Task 5 via `asyncio.create_task`.

Orchestration (assemble the verified helpers in this exact order — cite the signatures block):
1. `parse_deploy_plan` from the tarball (extract to a temp dir in-memory).
2. Resolve `company_id` (`resolve_company_id_for_owner`), `session_cookie` (owner's Paperclip admin/session), container reachability (`resolve_running_container` → ip, `gateway_token`). If unreachable → `set_status(failed, "container unreachable")` and return.
3. Resolve **Friday's id**: `list_agents(company_id=...)` → the one with `role == "ceo"`.
4. Build a `slug → new_agent_id` map as agents are created. For each `PlannedAgent` in `plan.agents` (topological):
   a. skip if the job row already has it `materialized` (resume-safety).
   b. `reports_to` = Friday's id if `planned.reports_to_slug == plan.ceo_slug` (the division head reports to Friday); else the already-created `new_agent_id` of `reports_to_slug`.
   c. `minted = service_token.mint(owner_id)`; `adapter_config = synthesize_openclaw_adapter(gateway_url=_gateway_url_for_env(), service_token=minted, user_id=owner_id)`.
   d. `pc = await admin.create_agent(session_cookie=..., company_id=..., name=planned.name, role=planned.role, adapter_type=OPENCLAW_GATEWAY_TYPE, adapter_config=adapter_config, title=planned.title, capabilities=planned.capabilities, reports_to=reports_to, budget_monthly_cents=COMPANY_IMPORT_AGENT_BUDGET_CENTS)`.
   e. `record_agent(state="created", paperclip_id=pc["id"])`.
   f. `await pair_with_openclaw(owner_id, paperclip_agent=pc, gateway_pool=get_gateway_pool(), ip=ip, token=gateway_token)`; `await ensure_operator_device_paired(owner_id, paperclip_agent=pc, gateway_token=gateway_token)`; `record_agent(state="paired")`.
   g. `materialize_agent(owner_id, new_agent_id=pc["id"], planned=planned, ...)`; `record_agent(state="materialized")`.
5. Routines: for each agent, `create_agent_routines(...)` with the planned routines (dormant/paused by construction). Record `routine_ids`.
6. `set_status("completed")`.

Failure/rollback: any exception in the loop → `set_status("failed", str(exc))` (leave created agents in place for **resume**). A separate `async def rollback_company_deploy(*, job_id) -> None`: for each agent with a `paperclip_id`, guard `is_seeded_agent` (must be False), then `delete_paperclip_for_marketplace` + `catalog_service.unwind_partial_deploy`; `set_status("rolled_back")`.

- [ ] **Step 1: Write failing tests** — mock `admin` (`create_agent`, `list_agents` returning a Friday `role=ceo`), `pair_with_openclaw`, `ensure_operator_device_paired`, `materialize_agent`, `create_agent_routines`, the repo, gateway pool, ecs manager. Cover:
  - every `create_agent` call carries our synthesized `adapter_type="openclaw_gateway"` + `budget_monthly_cents=1000` (never a package-supplied adapter);
  - creation order is topological and the CEO's `reports_to` == Friday's id; a child's `reports_to` == its manager's freshly-minted id;
  - routines created via `create_agent_routines` (paused path);
  - resume: pre-seed the job row with the CEO `materialized`; assert `create_agent` is NOT called for the CEO on re-run;
  - rollback deletes exactly the created set and never an `is_seeded_agent`;
  - container-unreachable → `failed` status, no `create_agent` calls.

- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `feat(marketplace): company deploy orchestration service (resume + rollback)`.

---

### Task 5: Deploy + job-status endpoints

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py`
- Test: `apps/backend/tests/unit/routers/test_company_deploy_endpoints.py`

**Interfaces:**
- Produces: `POST /marketplace/listings/{slug}/deploy` (company branch) now: `require_platform_admin` (admin flag) → resolve listing → reject if `manifest.company.min_plan != "standard"` (409 "Requires Ultra Pro") → reject if agent_count > 10 → fetch tarball from S3 → `create_job` → `asyncio.create_task(run_company_deploy(...))` → 202 `{job_id}`. `GET /marketplace/company-deploys/{job_id}` → the job row (owner-scoped). `POST /marketplace/company-deploys/{job_id}/rollback` → dispatches `rollback_company_deploy`.

- [ ] **Step 1: Write failing tests** — replace-the-stub behavior: an admin deploying a standard company gets 202 + a job_id and a background task is scheduled (patch `asyncio.create_task`); a non-admin gets 403; an `ultra_pro` company gets 409; the job-status GET returns the row for its owner and 404/403 for others. Follow the PR1 `test_marketplace_company_listings.py` client/auth idiom.

- [ ] **Step 2–4: fail → implement → pass** (existing marketplace router tests stay green: `-k marketplace`).
- [ ] **Step 5: Commit** — `feat(marketplace): company deploy + job-status endpoints (admin-flagged, standard-only)`.

---

### Task 6: Frontend — deploy action, progress, routines-enable screen

**Files:**
- Modify: `apps/frontend/src/app/marketplace/listing/[listingId]/page.tsx`, `apps/frontend/src/lib/marketplace/api.ts`
- Create: `apps/frontend/src/components/marketplace/company/DeployCompanyButton.tsx`, `.../DeployProgress.tsx`, `.../RoutinesEnableCard.tsx`
- Test: `apps/frontend/src/components/marketplace/__tests__/DeployCompanyButton.test.tsx`

**Interfaces:**
- Consumes: new `useApi()` methods `deployCompany(slug)`, `getCompanyDeploy(jobId)`, `rollbackCompanyDeploy(jobId)`.
- Produces: standard company → enabled "Deploy company" button that POSTs, then polls `getCompanyDeploy` and renders `DeployProgress` (per-agent ticks, failed→Resume/Rollback). On completion, `RoutinesEnableCard` lists the imported routines paused with per-item enable + enable-all. `ultra_pro` → unchanged upsell. **The button only appears when the admin flag is on** — read from a `deploy_enabled` field the backend adds to the listing detail for admins (or gate purely client-side behind the existing admin host until the final flag flip; simplest: render the active button only when `listing` detail includes `company.deploy_enabled === true`, which the backend sets for platform admins).

- [ ] **Step 1: Write failing test** — `DeployCompanyButton`: standard + deploy-enabled → renders an enabled button that calls `deployCompany`; standard + not-enabled → renders the disabled "Deploy — coming soon"; ultra_pro → "Requires Ultra Pro". Mock `useApi`.

- [ ] **Step 2–4: fail → implement → pass** (`tsc` + `lint` clean).
- [ ] **Step 5: Commit** — `feat(marketplace): company deploy UI — action, progress, routines-enable`.

---

### Task 7: Full suites + dev E2E, then flip the gate

**Files:** `apps/backend/routers/marketplace_listings.py` (final gate flip), verification only.

- [ ] **Step 1: Full suites** — `uv run pytest tests/ -q` (coverage off if disk tight); `pnpm exec tsc --noEmit` + `pnpm run lint`. All green.
- [ ] **Step 2: Deploy CDK to dev** — the new table must exist before the endpoint works: confirm the deploy pipeline (or a targeted `cdk deploy` of the database + service stacks to dev) created `isol8-dev-marketplace-company-deploys` and wired the env var. (This is a real infra step — coordinate with the user; do not hand-create the table.)
- [ ] **Step 3: Dev E2E** — as a platform admin, deploy **gstack** (5 agents, standard) into a dev owner. Verify: job reaches `completed`; all 5 agents appear in Teams + the `/chat` roster; the CEO reports to Friday; delegate a task through the CEO and watch it flow to a report; routines listed paused; enable one and watch it fire; then run a rollback on a second deploy and confirm the created agents are removed (Friday/Atlas untouched).
- [ ] **Step 4: Flip the gate** — once E2E passes, change the deploy endpoint gate from `require_platform_admin` to the standard buyer gate (still `min_plan == "standard"` only; `ultra_pro` stays 409 until PR3). Keep `require_org_admin` as the normal auth. Commit — `chore(marketplace): open standard company deploy to buyers after dev E2E`.
- [ ] **Step 5: Stop for PR approval** — present branch + E2E evidence. Do NOT push/PR without explicit approval.

---

## Self-Review Notes

- Spec coverage: DeployPlan (T1), job table (T2), materialization w/ delegation preserved (T3), orchestration + resume + rollback (T4), endpoints admin-flagged + standard-only + status/rollback (T5), UI progress + routines-enable (T6), suites + dev E2E + gate flip (T7). Cost model (paused routines + budget caps, no per-agent heartbeat touch) threads through T4. PR3 (Ultra Pro tier + container resize + plan-based gate for `ultra_pro`) is a separate plan.
- Deferred-from-PR1 folded in: the `_frontmatter` `---`-in-body edge is fixed in T1 (with a test).
- Two real infra/ordering notes called out, not hidden: the CDK table must deploy to dev before the endpoint works (T7.2), and CDK isn't unit-tested (T2).
- Non-goal stated explicitly: per-agent heartbeat-off (budget caps are the backstop).
