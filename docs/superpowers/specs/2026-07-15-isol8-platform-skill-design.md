# Isol8 Platform Skill — Design

**Date:** 2026-07-15
**Status:** Approved (brainstorm complete)

## Problem

Agents running on Isol8 have fragmentary, uneven, and stale knowledge of the
platform they run on:

- **Friday** gets a rich-but-static picture from her seeds
  (`core/services/friday_identity.py`): SOUL.md describes where she lives and
  mentions the marketplace as one word; AGENTS.md says "recommend the
  marketplace" with no mechanics. USER.md hardcodes plan + provider at
  provision time and goes stale on provider switch.
- **Atlas** knows publish-side marketplace rules and gestures at "your isol8
  settings" without naming the Integrations tab.
- **No agent** knows the Integrations tab exists. Connected integrations
  appear to them as unexplained MCP tools (merged in
  `core/containers/config.py:1534`).
- **Atlas-built specialists and marketplace hires** get zero platform
  context — their knowledge of Isol8 is whatever their author wrote.
- **The Paperclip roster misdescribes the seed team.** Atlas registers as
  `role="general"` with no title, no capabilities, and no `reports_to`
  (`core/services/paperclip_provisioning.py:414`). Friday is instructed to
  treat the roster as "the only source of truth" — so she assigns Atlas
  domain research and prototypes (observed live in prod: Friday assigned
  Atlas an "MVP denial scorer prototype" and "FHIR API research" during an
  onboarding kickoff).

## Goal

One standard, uniform, integrated way to give **every** agent on the platform
correct, current knowledge of Isol8 — with per-owner dynamic state kept fresh,
and the Paperclip org model reconciled so the roster tells the same story.

## Design

Five components.

### 1. Baked `isol8` platform skill

`apps/infra/openclaw/skills/isol8/SKILL.md`, `COPY`'d to `/app/skills/isol8`
in the extended-image Dockerfile — the same mechanism as the bundled
`paperclip` skill (Dockerfile Layer 15). OpenClaw auto-discovers bundled
skills at `/app/skills/<name>/SKILL.md`, so every agent on every container
gets it with progressive disclosure: only the frontmatter description sits in
context until an agent opens the skill.

**Frontmatter description** tuned for routing: use when a question involves
the Isol8 platform itself — its UI, integrations, marketplace, billing,
plans, team structure, capabilities — rather than the user's domain work.

**Content (full platform manual):**

- **What Isol8 is** and how the owner experiences it: web app (isol8.co),
  desktop app (Tauri; browser pairing + exec approvals), channels
  (Telegram/Discord/Slack/WhatsApp).
- **Surface map:** `/chat` (chat + control panel; tabs enumerated from the
  live `ControlSidebar.NAV_ITEMS` at authoring time, including
  **Integrations**), `/teams` (Paperclip project management), `/marketplace`,
  `/settings`, `/onboarding`.
- **Integrations tab:** what it does (connect MCP-backed services); that
  connected services appear to agents automatically as MCP tools; exact
  phrasing for directing users there ("the Integrations tab in the control
  panel at isol8.co/chat"). Click-to-connect skills (CRM etc.) follow the
  same "connect it in Integrations" path.
- **Marketplace:** buyer flow (browse/search at `/marketplace` → deploy →
  agent appears in the Paperclip roster and openclaw.json, optionally
  parented at hire time); seller flow (the OWNER publishes via the seller
  wizard; Atlas builds but never publishes; Friday is unpublishable). Paid
  listings exist (Stripe checkout).
- **The seed team:** Friday (CEO, orchestrator — delegates, never does
  specialist work) and Atlas (agent recruiter — scaffolds new specialist
  agents through a research-first build methodology; does NOT perform domain
  research, prototypes, or general tasks; the correct ask to Atlas is a role
  spec, not a work item).
- **Paperclip org model** (written against the fork `Isol8AI/paperclip` at
  `~/Desktop/paperclip-fork`, NOT vanilla upstream):
  - `role`: behaviorally load-bearing. `ceo` gates `canCreateAgents`
    (`server/src/services/agent-permissions.ts:7`), a broad authorization
    pass (`authorization.ts:126`), company ops (`routes/companies.ts`), and
    assignment rights (`routes/agents.ts:2465`). Other agents are
    `general`/typed roles with no special powers unless granted
    `permissions.canCreateAgents` / `canAssignTasks`.
  - `chainOfCommand`: the walk up `reportsTo` links
    (`services/agents.ts:774`). Titles + capabilities are what the roster
    projects and what Friday should use for delegation decisions.
  - Budgets: per-agent monthly budgets exist (`services/budgets.ts`),
    owner-set. Exact zero-value semantics pinned at authoring time.
  - How agent creation actually works on Isol8: Atlas registers built agents
    via the Isol8 agent-builder bridge (backend-minted through the admin
    client — `routers/agent_builder.py`); Paperclip's own CEO-only create
    path is what Friday / the owner use from `/teams`; marketplace hires
    arrive Paperclip-first with title/capabilities derived from listing
    metadata (`routers/marketplace_listings.py:482`).
- **Plans and providers, generically:** $50/mo Starter flat fee; free tier
  (Bedrock Sonnet, $3/day cap, scale-to-zero); provider paths (Bedrock
  managed / ChatGPT OAuth / BYO key) — with a pointer to the facts file
  (component 2) for THIS owner's current state. The skill never states the
  owner's own plan/provider.
- **Hard boundaries:** agents cannot deploy marketplace listings, change
  billing/plan, connect integrations, or override exec approvals — with the
  exact user-facing click path to offer instead.

**Fleet reach:** image-baked, so the entire existing fleet gets it on the
next extended-image promotion + Track-2 apply. No reprovision, no per-agent
seeding.

### 2. Refreshable owner-facts file

`isol8-facts.json` at the EFS user root (in-container:
`~/.openclaw/isol8-facts.json`, readable by every agent; exact path pinned in
the plan against the container mount map).

- **Contents:** plan, provider label (human-readable, same mapping as
  `friday_identity._format_provider_label`), trial status, owner kind
  (personal/org), environment, `generated_at`.
- **Writers:** `ecs_manager.write_user_configs` at provision; rewritten by
  the same paths that already rewrite `openclaw.json` on provider switch /
  plan change. The backend is the sole writer.
- `USER.md` seed drops its hardcoded plan/provider lines and points at the
  facts file instead.

### 3. Roster enrichment (the Atlas fix)

`paperclip_provisioning.provision_org` passes the already-supported
`title` + `capabilities` params (`paperclip_admin_client.create_agent:596`)
for both seeds:

- **Friday:** `role="ceo"`, title "CEO", capabilities describing
  orchestration (delegates, triages, coordinates; does not execute
  specialist work).
- **Atlas:** `role="general"` (enum has no "recruiter"), title
  "Agent Recruiter", capabilities stating what he does — scaffolds new
  specialist agents via the build methodology — and explicitly what he does
  NOT do (domain research, prototypes, general tasks).

### 4. Seed pointer + dedupe

Light-touch edits to `friday_identity.py` templates and
`atlas_seed/files/`:

- Add one pointer line to Friday's and Atlas's seeds: for platform questions
  (UI surfaces, integrations, marketplace, billing, team structure), run
  `skill isol8`.
- Fix only fragments the skill supersedes or that are wrong: Friday's
  one-clause Atlas description gets the do/does-NOT framing; Atlas's vague
  "your isol8 settings" names the Integrations tab.
- No personality or orchestration-doctrine rewrites.

New provisions get the updated seeds; existing containers keep old seeds
until reprovision (accepted model) — but they still get the skill via the
image (component 1) and the roster fix via backfill (component 5).

### 5. Paperclip org-model reconciliation

- **Atlas reports to Friday:** pass `reports_to=<Friday's Paperclip id>` at
  Atlas seed time so the org chart and `chainOfCommand` reflect the real
  structure. Friday stays root.
- **Backfill script** (`apps/backend/scripts/`, dry-run by default,
  `--apply` to run — same pattern as existing backfills): for every existing
  company, set Atlas's title/capabilities/`reports_to`, Friday's
  title/capabilities, and write the owner's `isol8-facts.json`.
- **Authoring-time consistency check:** read Paperclip's injected role-based
  default instructions (`server/src/services/default-agent-instructions.ts`,
  `server/src/onboarding-assets/ceo/HEARTBEAT.md` in the fork) and verify
  the skill + our seeds do not contradict what Paperclip itself injects into
  runs. Conflicts found get resolved in this build (fix the seed, the skill,
  or flag the fork file), not papered over.

## Correctness discipline

The user's bar: "it has to be good and correct."

- Every factual claim in SKILL.md is verified against code at authoring time.
  A maintenance header comment (same pattern as the paperclip skill's
  vendoring header) cites the source anchors per section.
- Claims about Paperclip behavior are verified against the **fork**
  (`~/Desktop/paperclip-fork`), never the vanilla `paperclip/` clone.
- CLAUDE.md gets a one-liner: when platform surfaces / marketplace / billing
  / Paperclip seeding change, update `apps/infra/openclaw/skills/isol8/`.
- The user reviews the SKILL.md prose before ship — it is effectively
  user-facing.

## Out of scope

- Friday web-searching the owner's email to build a dossier during
  onboarding kickoff — that behavior belongs to the kickoff prompt
  (onboarding aha first-run), a separate fix.
- Full slim-down/rewrite of Friday's and Atlas's seed personalities.
- A live owner-facts API endpoint (file is sufficient; facts change rarely).
- Retrofitting per-agent seeds onto existing marketplace-deployed agents
  (the baked skill covers them).

## Testing

- Unit: facts-file writer (shape, rewrite triggers), provision_org passes
  title/capabilities/reports_to (assert against admin-client call), backfill
  script dry-run/apply behavior.
- Authoring verification: each SKILL.md claim traced to a code anchor
  (checklist in the PR description).
- Manual: dev-account smoke — ask Friday "what can Atlas do?" and "how do I
  connect an integration?" pre/post image promotion and roster backfill.
