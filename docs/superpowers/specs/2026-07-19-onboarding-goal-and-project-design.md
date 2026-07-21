# Onboarding creates the company's goal and project

**Date:** 2026-07-19
**Status:** approved, not yet implemented

## Problem

A freshly provisioned Isol8 company has a Paperclip company row, an owner, Friday,
and Atlas. It has **no goal and no project**. The Goals and Projects panels — both
fully built and wired (`TeamsPanelRouter.tsx:27-28`, `:48-49`; full CRUD in
`routers/teams/work.py:449-559`) — render empty on day one and usually stay that way.

The first-run funnel already asks the right question. `FirstRunFunnel.tsx:105-110`
prompts "Tell your CEO about your business," stages `BRIEF.md`, and Friday researches
it and puts three next moves on the board (`onboarding_brief.py:77-112`). Those three
tasks are created with no project and no goal, so they are never attributed to
anything.

### This is a bypass, not a missing feature

Paperclip's own `OnboardingWizard.tsx` has six steps, and step 2 is "Define your
mission." Advancing from it calls (`OnboardingWizard.tsx:542`):

```ts
const goal = await goalsApi.create(company.id, {
  title: parsedGoal.title,
  description: parsedGoal.description,
  level: "company",
  status: "active"
});
```

Upstream's north star *is* the company-level goal, surfaced to users as "mission,"
stored in `goals` rather than on `companies.description`.

Isol8 creates companies through the admin API (`paperclip_provisioning.py:326`), not
through that wizard, so we skip the step that produces the goal. Our companies are
goal-less because we route around upstream's onboarding, not because the concept is
absent.

### What is not available to reuse

- **`defaultInstall: true` is inert.** Nothing reads it to trigger an install; it is a
  sort hint for a manual picker grid (`TeamCatalog.tsx:2095`). `installCatalogTeam`
  has exactly one caller — the explicit install route. Upstream's own TEAM.md says so:
  *"once onboarding actually switches to the catalog service."*
- **The portability importer cannot create goals.** `grep goals
  server/src/services/company-portability.ts` returns nothing.
- **The fork already dropped upstream's project seeding.** It keeps the wizard's goal
  creation but removed the project/issue calls, leaving `ui/src/lib/onboarding-launch.ts`
  as dead code with no consumers. Re-adding a project is a deliberate departure.

## What we build

At brief-submit, before staging the instruction files, create two Paperclip rows and
record them. Friday refines them during her first-run session.

| Row | Value at seed | Refined by Friday |
|---|---|---|
| Goal | `level=company`, `status=active`, `parentId=null`, `ownerAgentId=Friday`, title = provisional (below) | title + description rewritten from her research |
| Project | `goal_ids=[goal.id]`, `leadAgentId=Friday`, `status=in_progress`, **no `targetDate`**, name = provisional (below) | renamed to the business name she generates |

A mission and a name are different kinds of string, so the two rows get different
provisional values rather than sharing one. Friday replaces both.

**Project name is always `{FirstName}'s Company`**, on every path. `_first_name(auth)`
is already resolved in this route (`onboarding.py:101`) and is always available, so
there is never a case where the project has no name or carries a visible placeholder.
Using the description as a name would produce things like "A social media agency for
small restaurants" as a company name; using the owner's first name is always readable
and always true.

**Goal title is a real mission statement on every path**, derived from what the funnel
collected:

| Path | Funnel input | Provisional goal title |
|---|---|---|
| Start something new | a description, optional "who's it for" | the description, trimmed to 120 chars |
| Grow my business | a URL | `Grow {domain}` — e.g. "Grow ferrosocial.com" |
| "I don't know yet — you pick" | nothing | `Find the right business to build` |

None of these reads as filler. The third is an accurate description of that user's
actual situation, and it stops being true the moment Friday picks something — at which
point she rewrites it.

The goal is the mission and never completes. The project is the container for all
work and also never completes — it is a bucket, deliberately, which is why it carries
no `targetDate` and why its `status` is not treated as meaningful.

### Two verified constraints

**Goal before project.** `getProjectDefaultGoalId` reads the singular `projects.goalId`
column (`issues.ts:784`), not the `project_goals` join table. Passing `goal_ids:[id]`
is correct — the service mirrors it into the legacy column on create
(`projects.ts:560-565`) and keeps it synced on update (`:803-809`) — but the goal must
exist first.

**A project with no goal is worse than no project.** `resolveIssueGoalId` returns
`projectGoalId ?? null` when the issue has a project, and does **not** fall through to
the company default. So a later PATCH setting `goal_ids: []` would null the legacy
column and silently orphan every task in the project. This is doctrine, not enforcement.

## Flow

```
User completes FirstRunFunnel  (inputs unchanged)
   │
   ▼
POST /onboarding/brief
   │
   ├─ resolve company for owner        (personal OR org admin)
   ├─ claim_onboarding(company_id)     PK conditional write on the sentinel
   │     └─ lost the claim  ─────────► return {status:"already_submitted"}
   ├─ list_agents → find role=ceo      (not ready → 409, release claim, retry)
   │
   ├─ create Goal                       level=company, status=active, parentId=null
   ├─ create Project                    goal_ids=[goal.id], lead=Friday, no targetDate
   ├─ record ids on the sentinel row    onboarding_goal_id, onboarding_project_id
   │
   ├─ stage BRIEF.md            (write-if-missing)
   ├─ stage BOOTSTRAP.md        (overwrite, interpolating the goal + project ids)
   └─ return {status:"staged"}
   │
   ▼
useFirstRunKickoff fires the kickoff message
   │
   ▼
Friday's first-run session  (revised BOOTSTRAP runbook)
   1. greet, one line
   2. read BRIEF.md
   3. name it     ─► PATCH company name, PATCH project name   ← first visible act
   4. research      (web_search; grow path fetches the site first)
   5. write research/market-research.md + MISSION.md
   6. PATCH goal    title + description ← refined mission
   7. create 3 next moves WITH projectId = the onboarding project
   8. close in chat
```

Committing the rows before writing the instruction files is the point of doing this
synchronously: there is no window in which Friday can wake and create tasks into a
company that has no project.

### Naming

The funnel collects a description, a URL, or nothing — never a company name, and
`paperclip_provisioning.py:326` names the Paperclip company after the owner's email.
Rather than adding a form field, Friday generates the name from the brief as her first
visible action and PATCHes both the company and the project. The user never types it,
and it is editable in Teams afterward.

Renaming the company is available to her but **role-gated**: `PATCH /companies/:id`
admits agent principals only when `actorAgent.role === "ceo"`
(`routes/companies.ts:355-363`), and restricts them to `updateCompanyBrandingSchema`,
which does include `name` (`validators/company.ts:44`). Friday is seeded `role="ceo"`
(`paperclip_provisioning.py:409`) so this works — but if her role ever changes, the
rename silently 403s and the company stays named after the owner's email.

The name is **not** generated at brief-submit. The backend makes no inference calls
today — every Bedrock reference in it configures the *container's* path, and
`bedrock_aip.py:301` deliberately uses the control-plane client, "not 'bedrock-runtime'
(which is the invoke client)." Adding one here would create a second inference door
outside the bedrock-gate, which the billing design exists to prevent. Friday is already
on the metered path.

## Scope: personal and org

Both. Two guards change:

- `routers/onboarding.py:76` `403 personal_only` — removed.
- `routers/onboarding.py:82` `403 trial_only` — removed. It is already a near no-op:
  `user_provision.py:104` writes the `trial_local` row during `ensure_user_ready`, so
  every personal signup is `trial_local` before reaching chat. A "paid" personal owner
  converted from trial and already ran the funnel.

Both are replaced by one company-scoped check on `onboarded_at`.

**This is required for correctness, not just coverage.** The current marker is
`user.unsafeMetadata.firstRun.briefSubmitted` — per user, in Clerk (`lib/firstRun.ts:27-32`).
With orgs in scope, member #2 joins a company that already has a goal and project, their
Clerk metadata is empty, and they would be served the funnel and seed a duplicate pair.

### The marker is a sentinel row, not a field

There is no per-company row to hang a field on. `PaperclipCompany` is keyed on
`user_id` (`paperclip_repo.py:71-77`) — one row per user, with `company_id` repeated
across an org's rows. Writing `onboarded_at` to the submitting user's row would leave
member #2's own row unmarked, reproducing the exact bug.

Instead, follow the idiom already in that file. `_ORG_CREATE_CLAIM_PREFIX`
(`paperclip_repo.py:63`) stores org-scoped state as a row with a synthetic `user_id`,
claimed via `ConditionExpression="attribute_not_exists(user_id)"` — a PK conditional
write, which the code notes is "strongly consistent and cross-instance-safe," unlike
the eventually-consistent `by-org-id` GSI.

The onboarding marker is the same shape: `user_id = "__onboarded__:{company_id}"`,
`kind = "onboarding_claim"`, holding `onboarding_goal_id` and `onboarding_project_id`
once seeding succeeds.

Claiming rather than checking also closes a race that a read-then-write would not: two
browser tabs submitting the brief simultaneously both pass a `get`-based check and both
seed a goal and project. With the conditional write, exactly one wins and the other
gets `already_submitted`.

On any failure after the claim — Paperclip not ready, goal or project create failing —
the claim is **released** so the client's retry can re-enter. This mirrors the org-claim
contract: "Callers MUST release on create FAILURE so a retry can proceed."

Care is needed at `user_provision.py:96-101`, which refuses the personal trial default
for a user with a pending org invitation — an invited member must not end up with both
a personal trial row and the funnel.

## Friday's authority

She may create projects and sub-goals freely, and PATCH the two seeded rows.

She may not create another `level=company` goal, and may not retire or delete the
seeded one. `getDefaultCompanyGoal` picks the **oldest** active root goal, so the risk
is asymmetric: a sibling company-level goal is nearly harmless because the seeded one
is older and keeps winning, but flipping the seeded goal to `achieved`/`cancelled` or
deleting it silently moves the default and re-attributes everything created afterward.

**Not enforced, and not yet written down.** Friday calls Paperclip's `/api/*` directly
with her per-agent bearer key — `friday_identity.py:297` sets `PAPERCLIP_API_URL` from
`paperclip-claimed-api-key.json` — so guards in `routers/teams/work.py` sit on the
frontend path and are not on hers. A real fence would require a carried fork patch, and
the blast radius is attribution drift on future issues, recoverable by PATCHing the goal
back to `active`. Not worth a patch to maintain across rebases.

Nor does this ship as instruction text. Friday's only occasion to touch the goal is the
title/description PATCH the first-run runbook asks for, which is not a retirement. This
section records the policy so the answer exists when she does get such an occasion —
creating projects for real work, or a future surface that lets her close goals out. It
is not a change to make now.

## Surfaces touched

| Surface | Change |
|---|---|
| `onboarding_brief.py` | Create the goal + project before staging files; extend the existing rollback |
| `paperclip_admin_client.py` | Has `create_goal` (:2387) and `create_project` (:2474). **Add `delete_project`** — Paperclip has `DELETE /projects/:id` (`routes/projects.ts:677`), never wired |
| `paperclip_repo.py` | Add `claim_onboarding` / `release_onboarding_claim` / `record_onboarding_ids` / `get_onboarding` on the `__onboarded__:{company_id}` sentinel, modelled on the existing org-claim methods (:378, :443) |
| `routers/onboarding.py:76-83` | Drop both guards; claim the sentinel instead |
| `routers/teams/work.py:517-559` | Add `DELETE /projects/{id}` — the user cannot otherwise remove a seeded project |
| `onboarding_brief.py:60-124` BOOTSTRAP | Revised runbook: name first, then research, then tasks **into the project**. Carries the goal + project ids and the two PATCH shapes she needs |
| `paperclip/references/workflows.md:7-18` | Project Setup workflow must link the company goal — see below |

### Instruction changes, and why there are only two

Everything Friday does differently during onboarding — naming, refining the goal,
filing tasks into the project — happens once, in the first-run session. The first-run
`BOOTSTRAP.md` is per-owner, overwritten on every brief-submit, and already the runbook
for exactly that session, so it absorbs all of it.

The second change is not about onboarding at all; it is a latent bug this design
activates. `SKILL.md:305` advertises "Set up a new project + workspace (CEO/Manager)"
and points at `references/workflows.md:7-18`, which says to `POST
/api/companies/{companyId}/projects` "with project fields" and never mentions linking a
goal. Because `resolveIssueGoalId` returns `projectGoalId ?? null` for any issue that
has a project — with no fall-through to the company default — a project created that
way silently orphans every task filed into it.

That is harmless today, since nothing inherits anyway. It stops being harmless the
moment goal attribution starts working. `references/api-reference.md:598` already shows
the correct shape (`"goalIds": ["{goalId}"]`); `workflows.md` needs to match it and to
say which goal and why.

Not changed, and why:

- **`isol8/SKILL.md` org-model section** — explanatory only; nothing depends on it.
- **`friday_identity.py` `_AGENTS_TEMPLATE`** — AGENTS.md is the weakest lever and
  drifts. The authority policy above is real, but Friday's only occasion to touch the
  goal is the PATCH the runbook asks for, and PATCHing a title is not retiring a goal.
  Writing doctrine for a risk she has no occasion to run into is speculative; add it
  when she does.
- **Goal creation endpoints** — she creates no goals; the backend does. Project
  creation is already documented and stays as-is apart from the goal link above.
- **Setting `goalId` on the three tasks** — unnecessary. She sets `projectId` and the
  server resolves the goal through `getProjectDefaultGoalId`.

`paperclip/SKILL.md:189` orders "Always set `parentId` and `goalId`" while documenting
no way to obtain a goalId. That is a real bug, but it predates this change and does not
block it — fix it separately.

## Error handling

**Atomic with rollback**, extending `onboarding_brief.py:159-185`. If the project
create fails after the goal succeeded, delete the goal, release the claim, and return
`409 not_ready`; the client's existing 5×-at-3s retry covers it. If recording the ids
fails after both rows exist, delete both and release.

The claim is taken **first** and the ids are recorded **last**, so the two states a
retry can observe are both safe: claim-without-ids means seeding was interrupted and
the claim has been released, and claim-with-ids means both rows exist and are linked.

**Friday failing to refine** degrades to the user seeing their own words as the goal
and project name. Unpolished, never broken, editable in Teams. No recovery path.

**Friday retiring the root goal** — recover by PATCHing it back to `active`. Because
it is the oldest root it reclaims `getDefaultCompanyGoal` automatically.

## Testing

- **`onboarding_brief.py` unit** — happy path; goal-create failure; project-create
  failure rolls back the goal; DDB failure rolls back both; asserts `goal_ids` is
  passed so the legacy column populates.
- **`routers/onboarding.py` unit** — second call returns `already_submitted` and
  creates nothing; org admin passes where they previously got `403 personal_only`;
  org member #2 is suppressed by the sentinel even though their own row is unmarked;
  two concurrent submits produce exactly one goal and one project; a failure after the
  claim releases it so the next attempt re-enters.
- **Contract** — a task created with `projectId` = the onboarding project resolves
  `goalId` to the onboarding goal. This is the test that catches a `goalId`/`goalIds`
  regression.
- **Frontend** — funnel inputs unchanged, so existing tests hold; the gating change
  needs org-path coverage.

## Deliberately out of scope

- **Measurable goals.** Paperclip's `goals` table has no target, current value, unit,
  metric, deadline, or progress — a goal is a labeled tree node with an owner. `okr`,
  `kpi`, `milestone`, `objective`, and `initiative` return zero hits across the fork.
  Anything OKR-shaped is net-new schema.
- **Teams as data.** `GOAL_LEVELS` includes `"team"` and the catalog ships `TEAM.md`
  bundles, but nothing persists a team; grouping is inferred from `agents.reports_to`
  subtrees only.
- **A fork-side fence** on the root goal — see "Friday's authority."
- **Backfilling existing companies.** There are no paid users; every company that
  matters is created after this ships.
</content>
