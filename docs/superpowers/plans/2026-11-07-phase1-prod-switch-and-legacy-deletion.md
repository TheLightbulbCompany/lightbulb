# Phase 1 — Super-agent to prod, legacy out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prod runs every workspace on the super-agent path (Router + Workers on Temporal), the existing chat UI stays as the manager surface, and Paperclip, the comms features and the dead product lines are deleted — code, stacks and data.

**Architecture:** Flip `superAgentEnabled(env)` to include prod, provision prod Temporal, run the two data moves, then retire the switch (live-config constant, delete branches), move the roster off Paperclip onto `agent_registry`, delete the Paperclip surfaces and stack, then the comms/product-line deletions. Deletions are driven by `~/Lightbulb/super-agent-design/legacy-deletion-inventory.md` (the authoritative file lists; cite its section in each PR).

**Tech stack:** Python/FastAPI, CDK (TypeScript), Next.js, Expo, Temporal Cloud (`tcld`), DynamoDB, AWS CLI (read-only except where a task says otherwise).

**Spec:** `docs/superpowers/specs/2026-11-07-voice-agents-design.md` §3 (constraints), §7 phase 1, §9 (keep the chat UI until Phase 4). Inventory: `~/Lightbulb/super-agent-design/legacy-deletion-inventory.md`.

## Global constraints

- Prod flips are their own PRs and wait for the founder's go in chat at that moment; the lead never dispatches the prod deploy without it.
- Keep: rooms, tasks, memory, network, A2A routes (`routers/a2a.py`), `/messaging/rooms*`, `/local-sessions/*`, `/memory/*`, `/sessions`, `/network/*` (the CLI uses them), the chat UI (rooms list, messages, composer, task pane, Needs you), AppSync Events and the messaging projector (the chat UI's live channel), `routers/paperclip_proxy.py` only until task 8.
- Delete only what is dead on both paths at the time of the PR (the inventory's rule); after task 4 the legacy path no longer exists in prod, so "after the switch" items become deletable.
- Old mobile builds (external TestFlight 0.4.8) break — accepted by the founder (7 Nov). Internal build 0.4.12 is on the super-agent path.
- Every deletion PR: `git grep` proves zero references, the build and the unit suites pass, the PR body cites the inventory section.
- Ponytail on; `/simplify` on each task's diff; reviewer PASS before merge.

## Review focus

1. A prod workspace row with `containers.super_agent = False` after the flip — task 3's test asserts it is listed and migrated by hand, not left on a deleted path.
2. Old mobile builds reading `features.superAgent` — task 4's test asserts the live-config constant `true` and that `/teams/agents` still answers (task 6).
3. The first prod Temporal worker with zero workflows — task 2's smoke asserts the worker polls the prod queue and the watchdog digest posts to the founder's workspace.
4. Destructive Apps migrations approved only through a Paperclip card — task 10 deletes Apps before task 8 deletes the card, so no approval path is lost while something still needs it.
5. `workspace_export` exports only Paperclip issues — task 7 replaces it with a tasks export before task 8 deletes it.

---

### Task 0: The AWS standard the phase gate reviews against

**Files:**
- Create: `docs/standards/aws-agentic-lens.md`

**Interfaces:** Produces the checklist `/code-review` uses at every phase gate: the Well-Architected Agentic AI Lens items that apply to this codebase (tenant isolation per session, least-privilege task roles, no long-lived secrets in env, idempotent effects, observability per task, cost per session), each with the repo location it is checked at.

- [ ] **Step 1: Write the checklist** — 25–40 items, one line each: `[pillar] rule — where in this repo — how to check`. Source: `~/Lightbulb/pivot-research-2026-11-07/11-aws-golden-path.md` §1 and §3, `09-agentcore-golden-path.md` §3.
- [ ] **Step 2: Verify every "where" exists on main** — `git grep -n <symbol>` for each; drop items that have no location.
- [ ] **Step 3: Commit** — `git commit -m "docs: AWS agentic lens checklist for phase-gate reviews"`.

### Task 1: Prod Temporal namespace and secret

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts:1917-1925` (env block), `:2138-2144` (secret), `:2631-2700` (worker + watchdog services)
- Modify: `apps/infra/lib/super-agent.ts:6-8`
- Modify/Delete: `apps/infra/test/super-agent-prod-inert.test.ts`
- Test: `apps/infra/test/super-agent-prod-enabled.test.ts`

**Interfaces:**
- Consumes: Temporal Cloud namespace `lightbulb-prod.nv5xx` (created by the lead with `tcld` using the founder's Temporal login; **not** by a subagent), Secrets Manager secret `lightbulb/prod/temporal_api_key` (minted by `tcld` straight into Secrets Manager, never printed).
- Produces: `superAgentEnabled(env)` → `true` for `"dev"` and `"prod"`; prod task definition carries `SUPER_AGENT_ENV_ENABLED=true`, `TEMPORAL_ADDRESS=lightbulb-prod.nv5xx.tmprl.cloud:7233`, `TEMPORAL_NAMESPACE=lightbulb-prod.nv5xx`, `TEMPORAL_TASK_QUEUE=lightbulb`; services `lightbulb-prod-temporal-worker` (on-demand, not `DEV_SPOT`, desired 1) and `lightbulb-prod-temporal-watchdog`.

- [ ] **Step 1: Write the failing synth test** `apps/infra/test/super-agent-prod-enabled.test.ts`: synth the prod stage; assert the backend task definition environment has `SUPER_AGENT_ENV_ENABLED: "true"` and `TEMPORAL_NAMESPACE: "lightbulb-prod.nv5xx"`; assert a Fargate service named `lightbulb-prod-temporal-worker` exists with `CapacityProviderStrategy` not containing `FARGATE_SPOT`.
- [ ] **Step 2: Run it** — `cd apps/infra && npx jest test/super-agent-prod-enabled.test.ts` — expected FAIL (gate returns false for prod).
- [ ] **Step 3: Flip the gate** in `super-agent.ts`: `return env === "dev" || env === "prod";` and make the worker/watchdog capacity strategy `env === "dev" ? DEV_SPOT : undefined` at `:2650` and the watchdog equivalent.
- [ ] **Step 4: Delete `super-agent-prod-inert.test.ts`** (its premise is gone).
- [ ] **Step 5: Run the infra suite** — `npx jest` — expected PASS.
- [ ] **Step 6: Commit** on a branch `phase1/prod-temporal` — `git commit -m "infra: super-agent gate on for prod; prod Temporal worker + watchdog"`. **Do not merge until task 2's namespace and secret exist** (the deploy would fail on the missing secret).

### Task 2: Create the prod namespace and secret, deploy dev, smoke

**Files:** none in the repo (operations), evidence in `super-agent-design/evidence/phase1/01-prod-temporal.md`.

- [ ] **Step 1 (lead, not a subagent):** `tcld namespace create --namespace lightbulb-prod --region aws-us-east-1 --retention-days 30` and `tcld apikey create … ` written straight into Secrets Manager as `lightbulb/prod/temporal_api_key` (same procedure as dev's; the dev secret name is the template). Record the namespace and key id (never the key) in the evidence file.
- [ ] **Step 2:** merge task 1's PR after reviewer PASS; the push deploys dev (unchanged behaviour on dev).
- [ ] **Step 3 (founder's go required):** dispatch the prod deploy. Expected: CloudFormation updates `lightbulb-prod` service stack; two new services reach `RUNNING 1/1`.
- [ ] **Step 4: Smoke** — `aws ecs describe-services --cluster lightbulb-prod-container --services lightbulb-prod-temporal-worker lightbulb-prod-temporal-watchdog` shows running 1; `aws logs tail /ecs/lightbulb-prod --filter-pattern "temporal-worker" --since 10m` shows the worker polling `lightbulb`; the watchdog digest posts to the founder's workspace room (observe mode).
- [ ] **Step 5:** append the smoke output to the evidence file; commit.

### Task 3: Data moves and the opt-out audit before the switch bites

**Files:**
- Modify (run, not edit): `apps/backend/scripts/copy_workspace_meta.py`, `apps/backend/scripts/backfill_agent_registry.py`
- Create: `apps/backend/scripts/list_super_agent_optouts.py`
- Test: `apps/backend/tests/unit/scripts/test_list_super_agent_optouts.py`

**Interfaces:**
- Produces: `list_super_agent_optouts.main(argv) -> int` printing one line per container row where `super_agent is False`, exit 0; the two existing scripts run with `--all --apply` then `--verify` inside the prod backend task (ECS exec, output to `/tmp/*.log`, read back with `head -c 9000`/`tail -c 9000` — the exec channel truncates).

- [ ] **Step 1: Write the failing test** — a container row with `super_agent: False` and one without → output lists exactly the first owner id.
- [ ] **Step 2: Run it** — `cd apps/backend && uv run pytest tests/unit/scripts/test_list_super_agent_optouts.py -v` — FAIL.
- [ ] **Step 3: Implement** the script with `container_repo.scan_all()`; print `owner_id` for rows whose `super_agent` is exactly `False`.
- [ ] **Step 4: Run it** — PASS. Commit.
- [ ] **Step 5 (lead, prod, founder's go):** run in the prod backend task: `copy_workspace_meta --all --apply` then `--verify`; `backfill_agent_registry --all --apply` then `--verify`; `list_super_agent_optouts`. Expected: verify steps report 0 mismatches; the opt-out list is empty or every listed owner is set to `True` by hand with the founder's word (they are workspaces someone opted out on purpose). Evidence to `evidence/phase1/02-data-moves.md`.

### Task 4: Retire the switch — live-config constant, then delete the branches

**Files:**
- Modify: `apps/backend/routers/messaging.py:836` (`features: {superAgent: true}` always)
- Modify: `apps/backend/core/services/super_agent_flags.py` (`flags_for` → always on; `list_on()` → all workspaces query), `apps/backend/core/config.py:296` (remove `SUPER_AGENT_ENV_ENABLED`)
- Modify: `apps/frontend/src/hooks/useLiveEvents.tsx:56-59,115-122,163,307,329,357-361` and the 30 importers of `useSuperAgent()`; `apps/mobile/src/providers/live-events.tsx:50-55,191-209,255,275,307-310` and its 9 importers — delete the false branches (inventory §1b, §1c)
- Delete: backend/Hermes legacy branches in inventory §1d A, B, D, E and §3 1a/2a
- Test: `apps/backend/tests/unit/super_agent/test_flags.py` (rewrite: flags always on), `apps/frontend` and `apps/mobile` unit suites

**Interfaces:**
- Produces: `flags_for(owner_id) -> SuperAgentFlags` always enabled; `useSuperAgent()` removed (callers assume true); live-config `features.superAgent === true` constant.

- [ ] **Step 1: Write the failing tests** — `test_flags.py`: `flags_for("org_x")` enabled with no DB read; frontend: a test that renders `WorkspaceShell` with `features: null` in live-config and asserts the super-agent Home renders.
- [ ] **Step 2: Run them** — FAIL.
- [ ] **Step 3: Implement** the three constants/removals above, then delete the branches file by file per inventory §1b/§1c/§1d, grepping `useSuperAgent\|superAgent ?\|flags_for(` to zero after each.
- [ ] **Step 4: Run** `uv run pytest tests/unit` (backend), `pnpm test` (frontend, mobile) — PASS; `pnpm build` (frontend) — PASS.
- [ ] **Step 5: Commit** in two PRs (backend+infra; web+mobile) — `git commit -m "feat: retire the super-agent switch (always on)"`.
- [ ] **Step 6:** merge after PASS → dev deploys; walk dev (Home, a room, a task, Needs you); evidence `evidence/phase1/03-switch-retired.md`. Prod deploy with the founder's go.

### Task 5: Replace `workspace_export` with a tasks export

**Files:**
- Modify: `apps/backend/routers/teams/workspace_export.py` → move the route to `apps/backend/routers/tasks.py` as `GET /tasks/export` (JSON lines of tasks + attempts + outputs for the caller's workspace)
- Test: `apps/backend/tests/unit/routers/test_tasks_export.py`

**Interfaces:**
- Produces: `GET /tasks/export` → `application/x-ndjson`, one task per line with `taskId, status, title, createdAt, resolvedAt, outputs[]`; replaces the Paperclip-issues export (`workspace_export.py:69`).

- [ ] **Step 1: Failing test** — two tasks in the test workspace → two lines, fields present, no Paperclip calls.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** over `tasks_repo` (page through `list_tasks`). **Step 4: Run** — PASS. **Step 5: Commit** `feat: tasks export replaces Paperclip workspace export`.

### Task 6: Roster off Paperclip — `/teams/agents` and members from `agent_registry`

**Files:**
- Modify: `apps/backend/routers/teams/agents.py` (870 lines; reads move to `agent_registry`; heartbeat/wake/arm endpoints deleted)
- Modify: `apps/backend/core/services/agent_registry.py` (the source of truth; remove the Paperclip mirror on write, `:117+`)
- Modify: `apps/backend/core/services/paperclip_agent_lifecycle.py` callers (`user_provision.arm_nightly_ceo`, `trial_lifecycle.py:95`, `provision_runner.py:121`, `hermes_provisioning.py:1341`) — delete the arm/pause calls
- Test: `apps/backend/tests/unit/routers/test_teams_agents_registry.py`

**Interfaces:**
- Consumes: `agent_registry.list_agents(owner_id) -> list[AgentRow]`, `get_agent(owner_id, agent_id)`.
- Produces: `GET /teams/agents` and `GET /teams/members` answer from `agent_registry` with the same response shape the web roster, Activity, the members section and "Hire a specialist" read today (the shape is pinned by the existing frontend types in `packages/shared`); `POST /teams/agents/{id}/heartbeat`, `/wake` → 410 Gone.

- [ ] **Step 1: Failing test** — seed two registry rows; `GET /teams/agents` returns both with `id, name, title, capabilities, specialist` and never calls `PaperclipAdminClient` (patch it to raise).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; delete the heartbeat/wake/arm code and the `NIGHTLY_CEO_*` settings. **Step 4: Run** backend unit suite + `pnpm test` (frontend roster tests) — PASS. **Step 5: Commit** `feat: roster served from agent_registry; Paperclip heartbeats and wakes removed`.
- [ ] **Step 6:** merge → dev; walk Team/roster, Activity, members, Hire a specialist on dev; evidence `evidence/phase1/04-roster.md`.

### Task 7: Delete the legacy-only Paperclip surfaces (web, mobile, backend Group 2 + 3)

**Files (per inventory):**
- Delete web: §4c B/C/D (Issues, task-chat, cases, goals, Paperclip projects, routines viewer, `ArtifactsPanel`, `WorkTabs`, `BudgetsPanel`, `MarketplacePanel`, legacy `DESTINATIONS`, `StartBox`, legacy `RoomHeader` block, `LegacySettingsPage`, `ideasFirstOpen`, "Create a channel" path) and the entry points in §4b (`HomeWork.tsx:102`, `RoomPanel.tsx:1138`, `MessageTaskCard.tsx:72-79,95`, `appLinks.ts:50-51`, `shellNav.ts:94-97`, `ArtifactDockPanel.tsx:204`, `LocalSessionWork.tsx:53`, `RunNotifications.tsx:211,225`, `AgentInspectorPanel.tsx:235`, `RoutinesPanel.tsx`, `commandPaletteActions.ts:68`, `companySections.ts:44`, `SUPER_AGENT_ALIASES` in `panelRegistry.ts:228-232`)
- Delete mobile: §4d (issue and run screens + components), `mobile/src/lib/activity.ts:32-35`
- Delete backend: §4e Group 2 (`routers/teams/issues.py`, `work.py`, `attention.py`, `interactions.py`, `cases.py`, `search.py`, `inbox.py`, `feed.py`, `tree_control.py`, `runs.py`, `approvals.py`, `session_work.py`, `decisions.py`, `feedback.py`, `budgets.py`, `settings.py`, `workspace_export.py` (after task 5), `paperclip_trigger_proxy.py`, `paperclip_events.py`, `paperclip_escalation.py`, `teams_event_broker.py`, `paperclip_event_client.py`, `agent_routine_provisioning.py`) and Group 3; `needs_you_email.py:176` and `ideas.py:523,568` links → task links
- Move: `routers/teams/_guards.py` → `core/services/entitlement_guards.py` (used by `apps.py`, `automations.py`, `company_stripe.py`, `company_provisioning.py`)
- Test: the deleted files' tests go with them; add `apps/backend/tests/unit/test_no_paperclip_routes.py` asserting no route path starts with `/teams/issues`, `/teams/inbox`, `/teams/runs`, `/teams/approvals`, `/teams/routines`, `/teams/goals`, `/teams/cases`

**Interfaces:** Consumes task 5 (`GET /tasks/export`) and task 6 (roster from registry). Produces nothing new; `require_always_on_entitlement` keeps its name at the new path.

- [ ] **Step 1: Failing test** `test_no_paperclip_routes.py` — FAIL (routes exist).
- [ ] **Step 2: Delete** in three PRs (backend; web; mobile), each with `git grep -n` of every deleted symbol returning nothing, unit suites + builds PASS. Pre-switch triggers (inventory §1d C) are cancelled, not migrated: list them with `paperclip_trigger_proxy`'s reader before deletion, post the count in the PR, and confirm with the founder that none is a customer's.
- [ ] **Step 3: Run** `test_no_paperclip_routes.py` — PASS. Commit per PR: `chore: delete legacy Paperclip surfaces (inventory §4c/§4d/§4e group 2)`.
- [ ] **Step 4:** merge → dev; walk dev: Home, a room, tasks, Needs you, Activity, settings, mobile internal build; evidence `evidence/phase1/05-paperclip-surfaces.md`.

### Task 8: The Paperclip exit — Group 1 code, the stack, both Aurora clusters, secrets

**Files:**
- Delete backend: inventory §4e Group 1 (`paperclip_admin_client.py`, `paperclip_repo.py`, `paperclip_provisioning.py`, the lifecycle, `routers/paperclip_proxy.py`, the `paperclip_*` key/session/identity/mail/wipe helpers, `paperclip_cost.py` + its router, `routers/company_board.py`, `teams/deps.py`/`schemas.py`/`__init__.py`, `task_board.py`), `apps/backend/scripts/*paperclip*.py`, `pause_unlive_paperclip_automation.py`, `disable_paperclip_timers.py`
- Delete infra: `apps/infra/lib/stacks/paperclip-stack.ts`; the Aurora cluster in `database-stack.ts:579-600`; the Paperclip secrets and KMS grants in `auth-stack.ts` (`paperclipServiceTokenKey`, `paperclipEventsWebhookSecret`); `lightbulb-stage.ts` wiring (`:203-207`, `:241-242`, `:315+`); the settlement Lambda's Paperclip budget posting (`openrouter-gate-stack.ts` — keep the gate, drop the posting)
- Modify: `apps/backend/core/services/entitlement_teardown.py:120-170` (drop the Paperclip disarm), `durable_activity.py:62`, `hermes_provisioning.py:751-761`
- Test: `apps/infra/test/no-paperclip-stack.test.ts` (prod and dev synth contain no `AWS::RDS::DBCluster` and no resource named `*Paperclip*`); backend `tests/unit/test_no_paperclip_imports.py` (`git grep -l "paperclip" apps/backend/core apps/backend/routers` is empty except `known_services.yaml`)

**Interfaces:** Consumes tasks 6 and 7 (nothing on the super-agent path reads Paperclip). Produces the DynamoDB-only backend.

- [ ] **Step 1: Failing tests** (both) — FAIL.
- [ ] **Step 2: Delete** the code (one PR) and the stacks (one PR, after the code PR is on dev). The stack PR's `cdk diff` on dev must show only removals; post it in the PR.
- [ ] **Step 3: Run** tests — PASS. Commit `chore: Paperclip exit — code` and `infra: remove Paperclip stack and Aurora`.
- [ ] **Step 4:** merge code → dev; walk; merge infra → dev; `aws rds describe-db-clusters` shows no `lightbulb-dev-paperclip-db`. Prod with the founder's go; after prod: `aws rds describe-db-clusters` empty; delete the Paperclip secrets (`lightbulb/*/paperclip-agent-keys/*`, `paperclip*`) with the founder's go; evidence `evidence/phase1/06-paperclip-exit.md`.

### Task 9: Delete the comms features (keep the chat UI as the manager surface)

**Files:**
- Delete web: channels + `#` creation, threads, reactions, mentions UI, pins, links panel, formatting bar, Ideas (`ideas/*`), Feed (`activity/TeamFeed`), Daily look-around, Brief, per-agent `voice_persona` controls, Creations/Canvas/Library nav, Browser pane, Sessions UI, Skills/MCP/Local MCP/Desktop/Model settings rows — the lists in `~/Lightbulb/pivot-research-2026-11-07/06-product-and-ux.md` §4
- Keep web: rooms list, DM/room messages, the composer (plain text), task cards + pane, Needs you, Settings (Billing, Members, Connections, Approvals, Notifications), **and the in-app Live session UI (`huddle/LiveStrip`, mobile `voice/LiveSheet`) — it becomes the hold-to-talk orb in Phase 4 (spec §5), so it is re-pointed, not rebuilt**
- Delete backend: reactions/threads/mentions/pins/links routes in `routers/messaging.py` (only those the deleted UI calls — verify each with `git grep` across web, mobile, CLI), `proactive.py` builtins (Ideas), `ideas.py`, the OpenSearch message search route + `core/services/message_search*`, the search indexer Lambda and its queues (`api-stack.ts:900-912` OpenSearch Serverless, indexer, three queues, `aoss-data` endpoint)
- Keep backend: `/messaging/rooms*` CRUD, `/local-sessions/*`, `/memory/*`, `/sessions`, `/network/*`, `routers/a2a.py`, AppSync Events + projector, the huddle/voice pipeline (`huddle-agent`, `livekit_huddles`, `meet_bridge`, `voice_calls`, `voice_billing`) as plumbing for Phase 3/5
- Test: `apps/infra/test/no-opensearch.test.ts`; frontend tests for the kept surfaces pass; `apps/backend/tests/unit/test_cli_routes_present.py` asserting the CLI's routes above still exist

**Interfaces:** Produces the chat UI reduced to rooms + messages + tasks + Needs you; no search; no comms extras.

- [ ] **Step 1: Failing tests** (`no-opensearch`, `cli_routes_present` — the latter PASSES now and must stay green) — run.
- [ ] **Step 2: Delete** in PRs per area (web comms; mobile; backend routes; infra OpenSearch), each with zero-reference greps and green suites.
- [ ] **Step 3: Run** all — PASS. Commit `chore: delete comms features; chat UI kept as manager surface`.
- [ ] **Step 4:** merge → dev; walk: open a room, send a message, start a task, answer a Needs-you card, settings; the CLI's `lightbulb` MCP tools against dev (find_rooms, send_message, find_tasks) still work; evidence `evidence/phase1/07-comms.md`.

### Task 10: Delete Apps, Stripe Projects, marketplace, sessions/desktop

**Files:**
- Delete: the Apps backend (`routers/apps.py`, `core/services/apps_*`, `apps_sweeper.py`, the Vercel/Neon integration code, `stripe_webhook_handlers.py` Apps branches), the Stripe Projects platform integration, `routers/marketplace_*`, `core/services/marketplace/*` (keep `known_services.yaml` only if `onboarding_brief` reads it — check), web `app/admin/marketplace`, `canvas/*`, `creations/*`; sessions/desktop (`session/*` on mobile, `NativeSessionTranscript`, the Rust desktop app under `apps/desktop` if present) — counts per `08-verification.md` (Paperclip ≈ 81k already gone in task 8; marketplace ≈ 17.5k; sessions ≈ 17k + 8k + 62k Rust)
- Delete infra: the Apps secrets (`lightbulb/prod/integrations/*/apps/*` — 60 secrets, founder's go), the Lambda provisioned concurrency on dev `openclaw-work-consumer`
- Test: backend `test_no_apps_routes.py`; web build; mobile build

**Interfaces:** Consumes task 8 (the Paperclip approval card that guarded Apps migrations is gone; Apps goes first — order inside this task: Apps code, then its secrets).

- [ ] **Step 1: Failing test** — FAIL. **Step 2: Delete** per area with zero-reference greps. **Step 3: Run** — PASS. **Step 4: Commit** `chore: delete Apps, Stripe Projects, marketplace, sessions/desktop`. **Step 5:** merge → dev; walk; secrets deletion with the founder's go; evidence `evidence/phase1/08-product-lines.md`.

### Task 11: Phase gate

**Files:** `super-agent-design/evidence/phase1/09-gate.md`

- [ ] **Step 1:** `/ponytail-audit` on `apps/backend`, `apps/frontend`, `apps/mobile`, `apps/infra` — apply the deletions it finds that are dead on main (one PR).
- [ ] **Step 2:** `/code-review` of `main` since the phase's first commit against `docs/standards/aws-agentic-lens.md` + the repo's standards; fix findings; `/security-review` on the same range; fix.
- [ ] **Step 3:** founder walks dev (Home, room, task, Needs you, settings, mobile) and the CLI; says go.
- [ ] **Step 4 (founder's go):** prod deploy; walk prod; `aws ce get-cost-and-usage` for the last 7 days by service shows no RDS, no OpenSearch, no Paperclip Fargate task; record in the evidence file. Phase 1 closes.
