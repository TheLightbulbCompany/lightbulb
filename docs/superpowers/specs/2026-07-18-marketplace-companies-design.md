# Marketplace Companies — Design

**Date:** 2026-07-18
**Status:** Approved (user-reviewed in session)
**Disposable:** delete when the feature ships (CLAUDE.md docs-lifecycle policy).

## Summary

Bring companies.sh company packages into the Isol8 marketplace: mirror the
`paperclipai/companies` registry as curated `kind="company"` listings under a
new Companies tab, add a one-click deploy that instantiates a whole company
(agents + hierarchy + skills + routines) into the owner's Paperclip org and
OpenClaw container, and introduce an **Ultra Pro** tier ($150/mo, 2 vCPU/8 GB
container) that unlocks large companies.

Investigation record: `project_companies_sh_marketplace_investigation` memory
(2026-07-18). Key facts: companies.sh is a discovery layer over GitHub repos
implementing the `agentcompanies/v1` spec (full spec:
`paperclip/docs/companies/companies-spec.md` in the vanilla clone); the
registry is MIT with pinned-commit provenance; most companies are 4–7 agents,
a middle band is 28–54, one outlier is 167.

## Locked product decisions

1. **Size-gated tiering** — companies with ≤10 agents deploy on the standard
   $50 plan; >10 agents requires Ultra Pro. Backend-enforced.
2. **Ultra Pro** — $150/mo, container resized to 2 vCPU / 8 GB
   (Fargate infra ≈ $85/mo, vs ≈ $21/mo for the standard 0.5 vCPU/2 GB box).
3. **Hierarchy** — an imported company attaches as a **division under
   Friday**: the package's CEO becomes a division head with
   `reports_to = <Friday's Paperclip agent id>`; package-internal `reportsTo`
   structure is preserved below that point. Friday stays the single apex.
4. **Automation starts paused** — all imported routines/recurring tasks are
   created non-active, and every imported agent's `openclaw.json` entry gets
   `heartbeat: {"every": "0m"}` (VERIFIED mechanism — upstream heartbeat
   defaults ON at 30m even when the key is absent; only `every: "0m"`
   disables; see `project_trial_heartbeat_burn_root_cause`. `HEARTBEAT.md`
   is only prompt content and is written as-is). A post-deploy screen lists
   automation with per-item enable + enable-all. Agents still respond to
   direct messages/delegation while automation is paused.

## Architecture decision: backend-orchestrated deploy loop ("Approach A")

The deploy engine parses the package **in our backend** and creates agents
one at a time through Paperclip's ordinary `create_agent` API — the same path
`routers/teams/agents.py:create_agent` uses today. Paperclip's own bulk
importer (`/api/companies/.../imports/apply`, present in our deployed fork
branch `isol8/2026.6-circuit-breaker` but unexposed) is **not** used: it
honors package-declared adapters (`claude_local` etc.), violating our
invariant that `adapter_type`/`adapter_config` are synthesized server-side
(`synthesize_openclaw_adapter`, openclaw_gateway). Post-fixing importer
output would create a born-broken window and widen the proxy security
surface, and the OpenClaw-side pairing loop has to run in our backend
regardless.

## Ship shape: three PRs, in order

Deploy risk and tier risk never stack: PR2 is proven end-to-end on a 5-agent
company (gstack) on the current box before PR3 starts.

### PR1 — Catalog (ingestion + tab)

- **Ingestion script** (one-shot; archive to `apps/backend/scripts/archive/`
  after running, per policy): clone `paperclipai/companies` at a pinned
  commit; for each directory containing a `COMPANY.md`:
  - build the deterministic tarball of the package **as-is** (spec format
    preserved — artifacts stay valid independent of our internal model);
  - run `marketplace_safety.scan_tarball` (third-party prompt content gets
    the same scrutiny as seller uploads);
  - upload to the marketplace artifacts S3 bucket
    (`listings/{id}/v{n}/` layout, same as agent listings);
  - create a published listing row **directly via the marketplace repo with
    a system seller id (`__isol8__`)** — the existing service surface is
    seller-centric (draft → submit → review), so curated ingestion writes
    published rows itself rather than bending the seller state machine.
  Skip anything without a root `COMPANY.md` (`skills/`, `*.skill`,
  `.agents`, `default`, dotfiles).
- **Listing row additions**: `kind` field (`"agent"` default, `"company"`),
  `agent_count`, `team_count`, `skill_count`, `min_plan` (derived:
  `standard` if agent_count ≤ 10 else `ultra_pro`), env requirements
  surfaced from `.paperclip.yaml` (e.g. optional `GH_TOKEN`), and provenance
  (`source_repo`, `source_commit`, license, authors). Publisher displays as
  "Isol8 (mirrored from paperclipai/companies)". Free listings; curated, so
  no seller/review flow.
- **Companies tab** on `/marketplace` filtering `kind=company`. Cards show
  agent count, teams, skills, requirements, attribution. Deploy button
  renders in one of three states: Deploy (≤10 agents, PR2 shipped),
  "Requires Ultra Pro" upsell (>10 agents), or disabled "Coming soon"
  (until PR2 flips on).
- Search (`marketplace_search`) indexes company listings like any other.

### PR2 — Deploy engine

**Entry point:** the existing `POST /marketplace/listings/{slug}/deploy`
branches on `kind == "company"`. Server-side gate checks (never trust UI):
agent_count vs owner plan, Paperclip company row active, container
provisioned. Creates a **deploy-job row** and dispatches a background task
(same pattern as `background_provision`).

**Deploy-job table** (new small DDB table in `database-stack.ts`,
e.g. `marketplace-company-deploys`): keyed by `owner_id` +
`listing_id@version`; fields: status
(`running | failed | completed | rolled_back`), per-agent progress entries
(package slug → created Paperclip agent id, pairing state), created routine
ids, timestamps, error detail. This row is both the idempotency anchor and
the UI polling surface.

**Pipeline:**

1. **Parse** — fetch tarball from S3; a pure-function parser reads the
   `agentcompanies/v1` subset we deploy (`COMPANY.md`, `teams/*/TEAM.md`,
   `agents/*/AGENTS.md` + sibling docs (SOUL/TOOLS/HEARTBEAT), `skills/*`,
   `tasks/*/TASK.md`, `.paperclip.yaml` routines/env) into a `DeployPlan`:
   topologically-ordered agent list with `reportsTo` edges, per-agent
   skills, routines, requirements. No side effects; fully unit-testable.
2. **Create + pair, topologically** — division head first, `reports_to`
   set to Friday's Paperclip agent id (VERIFIED resolvable: Friday is the
   `PaperclipCompany.seeded_agent_ids` member with no `reportsTo` — Atlas
   is seeded with `reports_to=friday_agent_id`), then children. Per agent,
   exactly the existing machinery: `synthesize_openclaw_adapter` →
   `_admin().create_agent` (name/role/title from frontmatter; every
   imported agent gets a default `budget_monthly_cents=1000` ($10/mo)
   safety floor, user-adjustable after) → `pair_with_openclaw` (threading
   identity/skills via `extra_update_fields`, the marketplace-proven path)
   → `ensure_operator_device_paired`. `AGENTS.md` bodies become the agent's
   IDENTITY/SOUL content via the `friday_identity.py` mechanism (VERIFIED:
   it pre-stages IDENTITY/SOUL/USER markdown + `workspace-state.json` with
   `setupCompletedAt` in the agent's EFS workspace dir; the state file
   suppresses OpenClaw's bootstrap wizard).
3. **Skills** — package-level `skills/*` are copied into each referencing
   agent's workspace skills dir on EFS (duplication across agents is fine)
   and bound via the existing marketplace `skills_patch` path.
4. **Routines** — created via `PaperclipAdminClient.create_routine` in a
   **non-active status**, with the schedule **trigger deferred** until the
   user enables (VERIFIED: the cron lives on a separate schedule trigger,
   not the routine row — fork `routines.ts` — and the fork's patch route
   explicitly detects status transitions to `active`, so paused-by-default
   has two independent enforcement points). Per-agent heartbeat disable is
   in the `openclaw.json` agent entry (decision 4).

**Failure model:** each completed agent is recorded in the job row before
moving on; a retried/resumed job skips recorded agents
(`pair_with_openclaw` already swallows "already exists"). Terminal failure
leaves the job `failed` with per-agent status; the UI offers **Resume**
(re-dispatch, skips completed) or **Rollback** (`delete_agent` loop over
created ids — the agent-delete routines-FK fix is already in our fork, and
the fail-closed `is_seeded_agent` guard means Friday/Atlas can never be
swept up in a rollback). Deploys are idempotent per (owner, listing,
version).

**PR2 frontend surface:** deploy-progress state on the listing detail page
(polling the job row: per-agent ticks, failed state with Resume/Rollback),
and the **post-deploy screen** listing imported routines/heartbeats paused
with per-item enable + enable-all.

**Rollout guard:** the deploy endpoint ships behind an admin-only flag;
user-visible Deploy flips on only after the dev E2E (below) passes.

### PR3 — Ultra Pro tier

- New Stripe price (`STRIPE_ULTRA_PRICE_ID`, $150/mo flat), `plan` field on
  the billing account row (`standard` default, `ultra_pro`).
- Upgrade via checkout/portal; on subscription-updated webhook confirming
  Ultra Pro: resize container through the **existing**
  `new_cpu`/`new_memory` force-redeploy path (`ecs_manager`, values
  `2048` / `8192`) and flip the deploy gate. (Webhook handling must respect
  the provider-clobber lesson: `project_stripe_metadata_provider_clobber_fixed`
  — never derive unrelated state from checkout-session metadata.)
- Deploy gate reads the plan field; listings with `min_plan=ultra_pro` show
  the upsell to standard owners.
- **Downgrade guard:** self-serve downgrade is blocked while the owner has
  >10 agents (a large org on a 2 GB box OOMs — see
  `project_per_user_container_2gb_not_1gb`); admin override exists. On
  allowed downgrade, resize back down. Deployed companies are never clawed
  back.

## Testing / verification ("make sure we do it right")

- **Parser fixtures are real packages**: vendor gstack (5 agents) and one
  49-agent company from the registry as test fixtures (MIT permits; keep
  LICENSE + attribution in the fixture dir). Parser unit tests assert the
  full DeployPlan shape against them.
- **Engine integration tests** with mocked Paperclip admin client + gateway
  pool assert: every created agent has our synthesized openclaw_gateway
  adapter (never the package's), creation order respects hierarchy, the
  division head reports to Friday, all routines are created disabled,
  heartbeats are `0m`, resume-after-partial-failure skips completed agents,
  rollback deletes exactly the created set.
- **Dev E2E before exposure** (admin flag): deploy gstack into a dev owner;
  verify all 5 agents in Teams UI and the /chat roster; delegate a task
  through the division head and watch it flow down; confirm routines listed
  paused; enable one, watch it fire; then rollback a second deploy to prove
  the delete path.
- **Soak watch items** during dev validation: per-agent gateway relay
  connections while the company is active; memory headroom on a standard
  box running a 7-agent company; Paperclip Aurora growth (expected fine).

## Out of scope

- The 167-agent `agency-agents` package (out of scope entirely; not
  deployable on any current tier).
- User-submitted company packages (sellers publishing their own companies)
  — future follow-up; ingestion is Isol8-curated for now.
- Automatic re-sync of the upstream registry (start with the pinned
  snapshot; re-run the ingestion script manually if upstream moves).
- Exporting an owner's org as a company package.
