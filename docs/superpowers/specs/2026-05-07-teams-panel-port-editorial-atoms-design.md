# Teams Panel Port + Editorial Atom Library — Design

**Status:** Draft for review
**Owner:** prabuddha + Claude
**Date:** 2026-05-07

## Problem

The /teams surface (PR #509 native UI + PR #542 editorial reskin) ships a thin port of upstream Paperclip's pages. LOC ratios make the gap concrete:

| Upstream page | Ours | Ratio |
|---|---|---|
| `Inbox.tsx` 2583 | `InboxPanel.tsx` 31 | 1% |
| `Costs.tsx` 1102 | `CostsPanel.tsx` 88 | 8% |
| `CompanySkills.tsx` 1296 | `SkillsPanel.tsx` 61 | 5% |
| `Routines.tsx` 1086 | `RoutinesPanel.tsx` 226 | 21% |
| `Agents.tsx` 427 | `AgentsListPanel.tsx` 160 | 37% |
| `Approvals.tsx` 135 | `ApprovalsPanel.tsx` 81 | 60% |
| `Dashboard.tsx` 396 | `DashboardPanel.tsx` 305 | 77% (charts already shipped) |

The reskin (PR #542) was a class-only swap — kept the thin data flow, restyled tokens. Most panels never received the structural shell upstream has: tabs, filter/search toolbar, collapsible section groups with counts, advanced settings drawers, sort/group toggles, action menus, modal flows. The result, in the user's words: **"Paperclip's UI is way more full even when things are empty"**, **"each tab doesn't look consistent"**, **"buttons and text look bad — not the right shape"**.

The /teams editorial brand (cream/forest tokens, Lora-italic mastheads) is not the problem. It should stay.

## Goals

1. **Functional parity with upstream Paperclip's panels** — every tab, filter, sort, group, action menu, advanced settings, modal flow, and empty-state CTA upstream ships, we ship.
2. **Visually consistent across panels** — same atoms used everywhere; no panel-specific divergence in button shape, input affordance, or empty-state treatment.
3. **Editorial language preserved** — cream/forest/ink tokens, Lora-italic mastheads, the `agent-monogram` / `agent-pill` / `section-label` atoms from PR #542 stay.
4. **BFF coverage matches** — every upstream-page network call has an equivalent `/api/v1/teams/*` route, with route-filter security maintained.

## Non-goals

- Public DNS for `company.isol8.co` / direct browser→Paperclip access (separate question, see PR #552 review).
- Multi-tenant Paperclip-level role distinctions (upstream's `provision_member` invites all members as `owner` for v1; we keep that simplification).
- **Mobile / narrow-viewport optimization.** Explicit non-goal for v1; can land panel-by-panel later.
- **Dark mode.** Explicit non-goal for v1; PR #542's editorial tokens are light-theme only and adding dark would multiply atom variants.
- Accessibility audit beyond what shadcn primitives ship with (deferred to a focused a11y PR).
- **Visual lab / atom showcase route.** Skipped — design review happens on the dev environment, which is what we maintain it for.
- **Cost budgets UI.** Upstream has it but our usage-control story is owned by Stripe meters / Bedrock credits, not Paperclip. Surfacing budget config from Paperclip would be either dead UI or a misleading second source of truth. Defer until usage architecture is reconciled.

## Approach: A + B (port upstream, using a shared editorial atom layer)

**B (atom layer) on its own** would give us consistency but leave us feature-poor. **A (port upstream) on its own** would give us features but a pile of "vendor + rename classnames" diffs that look unmaintainable and inconsistent at the seams. **A + B together**: port upstream's full feature set, but each panel composes a small set of editorial atoms instead of inlining shadcn primitives. The atom layer absorbs cross-panel consistency; the port discipline absorbs feature parity.

### B — The atom layer (`apps/frontend/src/components/teams/shell/`)

A small set of editorial-styled atoms split into two groups: **panel scaffolding** (`<PanelMasthead>`, `<PanelToolbar>`, `<PanelTabs>`, `<PanelSection>`, `<PanelEmptyState>`, `<PanelDataTable>`) and **primitive elements** (`<EditorialButton>`, `<EditorialInput>`, `<EditorialSelect>`, `<EditorialDialog>`, `<EditorialCard>`). Each is cream/forest/ink-tokenized, Lora-italic where it makes sense, and uses the `editorial-card` / `editorial-btn` class hooks already in `globals.css`. Together they replace ad-hoc structure currently scattered across panels.

| Atom | Replaces today | Ports upstream's |
|---|---|---|
| `<PanelMasthead>` | per-panel `<header>` blocks in DashboardPanel et al. | section-header pattern (overline + Lora-italic title + actions slot) |
| `<PanelToolbar>` | none — most panels have no toolbar | search input + filter chips + view-toggle buttons + primary CTA |
| `<PanelTabs>` | none — only Inbox has tabs today, ad hoc | tabbed sub-views (Inbox: default/unread/recent/archived; Routines: routines/runs; etc.) |
| `<PanelSection>` | none | collapsible group with chevron + label + count badge |
| `<PanelEmptyState>` | bare `"Inbox zero."` text in InboxPanel et al. | upstream `EmptyState` component (icon + headline + supporting copy + optional CTA), restyled |
| `<PanelDataTable>` | hand-rolled `<table>` in IssuesPanel + RoutinesPanel | consistent row/header treatment, hover states, sortable columns, selection |
| `<EditorialButton>` | `editorial-btn` class on raw `<button>` | shadcn `<Button>` — variants: primary/secondary/ghost/destructive; sizes: sm/md/lg/icon |
| `<EditorialInput>` / `<EditorialSelect>` / `<EditorialDialog>` / `<EditorialCard>` | ad-hoc | shadcn primitives — restyled with our tokens |

A visual-lab page at `/teams/_atoms` (dev-only, gated like `/admin/health`) renders every atom + every variant for review during PR 1.

### A — Port discipline (per-panel)

For each panel, the port produces a panel file ~70-90% the LOC of upstream's equivalent. The remaining 10-30% reduction comes from the atoms absorbing boilerplate (e.g., upstream's inline shadcn `<Tabs/><TabsList/><TabsTrigger/>` → our `<PanelTabs>`). The shape:

1. Read upstream `paperclip/ui/src/pages/<Panel>.tsx` end-to-end.
2. Catalog its features: tabs, filters, sorts, action menus, modals, forms, hover states.
3. Cross-check our BFF routes for any endpoint upstream calls that we don't proxy. **If gaps exist, the port PR ships a backend PR first** that adds the missing routes.
4. Re-implement the panel using our atoms + our existing `useTeamsApi` hook. Keep upstream's IA exactly; restyle every primitive.
5. Verify on dev (in a worktree, with the full Paperclip company seeded) before merging.

## Atom layer (PR 1) — detail

```
apps/frontend/src/components/teams/shell/
├── PanelMasthead.tsx          # editorial header w/ overline, Lora title, date, actions slot
├── PanelToolbar.tsx           # search + filter chips + view toggles + primary CTA + secondary
├── PanelTabs.tsx              # tabs row, count badges, active underline matches forest token
├── PanelSection.tsx           # collapsible group: chevron + label + count + content
├── PanelEmptyState.tsx        # icon + Lora headline + supporting line + CTA button
├── PanelDataTable.tsx         # hover row, sortable header, selection checkbox, density toggle
├── EditorialButton.tsx        # variants × sizes; honors editorial-btn class hooks
├── EditorialInput.tsx         # input + label + helptext + error pattern; matches forest focus ring
├── EditorialSelect.tsx        # shadcn Select w/ token override
├── EditorialDialog.tsx        # shadcn Dialog w/ editorial-card body + masthead-style title
├── EditorialCard.tsx          # editorial-card class wrapper w/ optional hover variant
└── index.ts                   # barrel re-export
```

Each atom is < 100 LOC, < 6 props, no internal state beyond what shadcn already manages. Atoms accept `className` for one-off overrides but their default styling is the canonical editorial treatment.

**Token cleanup:** `apps/frontend/src/app/globals.css` already exports the editorial tokens (`--cream`, `--forest`, `--ink`, `--mute`, `--edge`, `--warn`, `--danger`). PR 1 audits any panel-specific custom tokens that should be promoted (or deleted) to keep the surface area small.

**Design review:** the dev environment is the canonical surface for reviewing atom + panel changes during the port. No separate /teams/_atoms route — atoms are reviewed in-context as they're used by panels.

**Testing:** every atom gets a vitest snapshot test + a basic interaction test where applicable (button click, dialog open/close, tab switch, table sort). No e2e for atoms.

## Per-panel port plan (PRs 2-N)

Each port lists: upstream features to bring over, BFF endpoint coverage status, and any dependent backend PR.

> **Notation:** ✅ already proxied; ❌ missing BFF route → backend PR needed; ⚠️ partial.

### Inbox (highest priority — biggest gap, highest user traffic)

Upstream `Inbox.tsx` 2583 LOC. Features:
- Tabs: Default / Unread / Recent / Archived / Issues / Approvals
- Toolbar: search, filter pills (assignee, project, status, priority), sort, group, view toggle (list/board), keyboard shortcuts cheatsheet
- Issue rows: priority dot + status dot + title + project pill + assignee monogram + relative timestamp + action menu (archive, snooze, assign, comment, dismiss)
- Live-run badges: dot indicator when an agent is actively running on the issue
- Swipe-to-archive on touch; optimistic updates
- Empty states: per-tab (Inbox zero / No new items / etc.)

BFF coverage:
- `GET /inbox` ✅
- `GET /inbox/runs` ✅ (run history per inbox item)
- `GET /inbox/live-runs` ✅
- `POST /inbox/{item_id}/dismiss` ✅
- ❌ `POST /inbox/{item_id}/snooze` — needs BFF route + Paperclip endpoint (upstream uses `/api/companies/{co}/inbox/{id}/snooze`)
- ❌ `POST /inbox/mark-read` (bulk) — upstream supports it
- ❌ `GET /inbox/keyboard-shortcuts-config` — small, may be hardcoded client-side; verify

**Backend PR (Inbox-BE):** add the snooze + bulk-mark-read routes. ~80 LOC + tests.
**Frontend PR (Inbox-FE):** the port. Estimated ~1800-2000 LOC including atom usage.

### Issues (high priority — entry point for issue creation)

Upstream `Issues.tsx` 201 LOC. Features:
- Toolbar: status filter, priority sort, "+ New issue" CTA
- Table: priority + status + title + project + assignee + age
- "New issue" dialog (multi-step: title → description → project → assignee → priority)
- Bulk actions on selected rows (archive, assign)

BFF coverage:
- `GET /issues` ✅
- `GET /issues/{id}` ✅
- `POST /issues` ✅
- `PATCH /issues/{id}` ✅
- `POST /issues/{id}/archive` ✅
- Comments ✅
- ❌ `POST /issues/bulk-archive` — could be loop in client; upstream may have it
- Issue detail page is shipped (`IssueDetailPanel.tsx`) but uses ad-hoc structure — re-style with atoms.

**Backend PR:** none if we keep loops client-side.
**Frontend PR (Issues-FE):** port + IssueDetailPanel restyle. Estimated ~700 LOC.

### Routines (high priority — currently feature-poor)

Upstream `Routines.tsx` 1086 LOC. Features:
- Tabs: routines / runs (run history)
- Toolbar: search, sort (title/last-run/next-run), group (by agent / project / none), "+ Create routine" CTA
- Routine list: collapsible groups with count, row → trigger + concurrency + catch-up + agent + status toggle
- Create-routine dialog: multi-step (title, description, schedule, default agent, advanced: concurrency policy + catch-up policy)
- Routine detail (separate page upstream, can be a panel): trigger config, run history, edit, delete
- Run-now / pause-routine actions
- Empty: "No routines yet" + CTA

BFF coverage:
- `GET /routines` ✅
- `PATCH /routines/{id}` ✅
- `DELETE /routines/{id}` ✅
- ❌ `POST /routines` (create) — **must add**
- ❌ `POST /routines/{id}/run-now` — **must add** (upstream has it)
- ❌ `POST /routines/{id}/pause`, `/resume` — **must add**
- ❌ `GET /routines/{id}/runs` — **must add** (run history per routine)

**Backend PR (Routines-BE):** ~150 LOC + tests.
**Frontend PR (Routines-FE):** port. Estimated ~900 LOC.

### Goals + Projects (medium priority — currently very thin)

Upstream `Goals.tsx` (63 LOC) and `Projects.tsx` (87 LOC) are deceptively short — they delegate to richer detail components and reuse upstream's company-wide goal-setting flow. Features:
- List view with progress bars
- Goal/Project detail page (sub-tasks, linked issues, owner, target date)
- Create + edit dialogs
- Status transitions

BFF coverage:
- Goals: `GET /goals`, `POST /goals`, `PATCH /goals/{id}` ✅; ❌ `DELETE /goals/{id}`, ❌ `GET /goals/{id}` (single goal detail)
- Projects: `GET /projects`, `GET /projects/{id}`, `PATCH /projects/{id}` ✅; ❌ `POST /projects`, `DELETE /projects/{id}`

**Backend PR (Goals-Projects-BE):** ~120 LOC + tests.
**Frontend PR (Goals-Projects-FE):** ports for both. Estimated ~500 LOC combined.

### Dashboard (medium priority — already 77% LOC parity)

Upstream `Dashboard.tsx` 396 LOC. Our `DashboardPanel.tsx` 305 LOC. Charts + activity feed shipped (PR #539). Gap is mostly cosmetic — wrap in `<PanelMasthead>` + `<PanelToolbar>` (date-range picker), and replace the bespoke `<MetricCard>` with `<EditorialCard>` consistent with the rest.

**Backend PR:** none — `GET /dashboard` already returns everything needed.
**Frontend PR (Dashboard-FE):** restyle, no logic changes. Estimated ~150 LOC of changes.

### Agents + Org chart (medium priority)

Upstream `Agents.tsx` (427 LOC) + `Org.tsx` (132 LOC). Features:
- Agents: card grid (monogram + name + role + last-run + status), filter chips, "+ Hire agent" → marketplace deeplink, agent detail panel
- Org chart: hierarchical tree visualization (rendered via D3 or react-flow upstream), click → agent detail

BFF coverage:
- Agents: `GET /agents`, `GET /agents/{id}`, `POST /agents`, `PATCH /agents/{id}`, `DELETE /agents/{id}`, `GET /agents/{id}/runs` ✅
- Org chart: ⚠️ uses agent list with parent/child relationships — verify `parentAgentId` is in the BFF response shape (likely yes via upstream passthrough).

**Backend PR:** likely none.
**Frontend PR (Agents-Org-FE):** Agents card grid restyle + Org chart polish (better SVG/react-flow integration). Estimated ~600 LOC.

### Approvals (low priority — currently 60% LOC parity)

Upstream `Approvals.tsx` 135 LOC. Mostly there — restyle with atoms.

**Backend PR:** none — approve/reject already proxied.
**Frontend PR:** ~80 LOC changes.

### Costs (medium priority — heavy upstream LOC, low ours)

Upstream `Costs.tsx` 1102 LOC. Features: time-range selector, cost breakdown by agent/project/model, charts (LineChart + StackedBar), CSV export. **Budget alerts UI deliberately scoped out** — see Non-goals; usage control is Stripe-meter / Bedrock-credit-driven on Isol8, not Paperclip-driven. Surfacing Paperclip's budget config would be a misleading second source of truth.

BFF coverage:
- `GET /costs` ✅ (single endpoint)
- ⚠️ verify the response shape supports time-range filtering and group-by

**Backend PR (Costs-BE):** extend `/costs` with `?range=7d|30d|90d&group_by=agent|project|model` query params. ~120 LOC + tests (down from ~200 since budgets are out).
**Frontend PR (Costs-FE):** port without budget UI. Estimated ~550 LOC (down from ~700).

### Skills (high priority for marketplace alignment)

Upstream `CompanySkills.tsx` 1296 LOC. Features: skill catalog, install/uninstall, version pinning, configuration (per-skill settings UI generated from JSON Schema), version history, dependency graph.

BFF coverage:
- `GET /skills` ✅
- ❌ `POST /skills/{id}/install`, `DELETE /skills/{id}/uninstall`
- ❌ `GET /skills/{id}/versions`
- ❌ `PATCH /skills/{id}/config`

**Backend PR (Skills-BE):** ~250 LOC + tests.
**Frontend PR (Skills-FE):** port. Estimated ~900 LOC.

### Members (low priority — currently 64 LOC)

Upstream `CompanyAccess.tsx` 678 LOC. Features: member list, invite via email, role management, revoke access, audit log.

BFF coverage:
- `GET /members` ✅
- ❌ `POST /members/invite`
- ❌ `DELETE /members/{user_id}`
- ❌ `PATCH /members/{user_id}/role`

**Decision:** Members panel uses a **Clerk deeplink** for invite/role/revoke flows ("Manage members in Clerk →"). Clerk owns identity; we just visualize who's in the workspace. Avoids duplicating Clerk's UX and keeps invite/role mutations in one place.

**Backend PR:** minimal — ~50 LOC to enrich `GET /members` with the data needed for the read-only display (last seen, last activity, role display name). No invite/role mutation routes.
**Frontend PR (Members-FE):** ~300 LOC. Read-only list + Clerk deeplink CTA.

## Empty-state pattern

Every panel renders the FULL shell (masthead + toolbar + tabs + section headers) even when zero data. The body of the data region uses `<PanelEmptyState>`:

```
┌─────────────────────────────────┐
│   [Lora-italic icon, 48px]      │  ← editorial-warm icon, not a generic gray box
│                                  │
│   No routines yet.               │  ← Lora-italic, ink-color, 22px
│   Create your first to schedule  │  ← supporting copy, mute-color, 14px
│   a recurring agent run.         │
│                                  │
│   ┌─────────────────┐            │
│   │ + Create routine │           │  ← editorial-btn--primary, forest fill
│   └─────────────────┘            │
└─────────────────────────────────┘
```

Per-panel empty-state copy is co-located with the panel (not the atom) so each panel curates its own voice while sharing the structure.

## BFF coverage summary

| Panel | Backend PR needed? | Approx LOC |
|---|---|---|
| Inbox | yes — snooze + bulk mark-read | ~80 |
| Issues | no | 0 |
| Routines | yes — POST + run-now + pause/resume + run history | ~150 |
| Goals + Projects | yes — DELETE + GET-detail + POST | ~120 |
| Dashboard | no | 0 |
| Agents + Org | no | 0 |
| Approvals | no | 0 |
| Costs | yes — query params + budgets endpoint | ~200 |
| Skills | yes — install/uninstall/versions/config | ~250 |
| Costs | yes — query params (no budgets) | ~120 |
| Members | small — read enrichment only (no mutations) | ~50 |
| **Total backend** | | **~770 LOC** |

Each backend PR ships independently of the frontend port, so they can land in parallel. We don't block frontend ports on backend PRs that don't materially gate the panel — e.g., Members can ship the panel with a Clerk deeplink first, then add server-side invite later.

## Sequencing

10 PRs total, roughly:

1. **Atom library + visual lab** — foundation; ~600 LOC frontend.
2. **Inbox-BE + Inbox-FE** (paired) — biggest user-visible win.
3. **Routines-BE + Routines-FE** — next biggest functional gap.
4. **Issues-FE + IssueDetail restyle** — already-shipped panel, polish.
5. **Skills-BE + Skills-FE** — marketplace alignment.
6. **Costs-BE + Costs-FE** — billing transparency.
7. **Goals-Projects-BE + Goals-Projects-FE** — bundle since both are thin.
8. **Dashboard-FE + Approvals-FE** — bundled cosmetic.
9. **Agents-Org-FE** — moderate.
10. **Members-FE** (deeplink first; backend later if needed).

PRs 2-10 can run partially in parallel after PR 1 lands. Each is independently mergeable.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Atom layer choices don't survive contact with the first port (PR 1's design is wrong) | medium | Visual lab page + first port (Inbox) shipped together as a stress-test before atoms are stamped as v1 |
| Upstream Paperclip drifts during the port (we re-port-from a moving target) | high | Pin upstream commit per port PR; track in `openclaw-version.json`-adjacent file or PR description |
| BFF route additions break route-filter security (`docs/audit-2026-05-02-paperclip-routes.md`) | low | Each backend PR includes a route-filter audit checkbox in its PR template; enforced via existing test |
| Editorial tokens don't compose well with shadcn primitives (focus rings, hover states fight Tailwind defaults) | medium | Atom layer absorbs this via explicit `data-state=*` overrides; documented in `globals.css` |
| Cross-org permissions misalignment (a member sees data they shouldn't) | low | Paperclip's authz already filters per-company; we just pass through. Verify each panel respects the BFF's company scoping. |
| LOC growth slows page load | low | Code-split per panel via Next.js dynamic imports; atoms are tiny |

## Resolved decisions

All five open questions answered before implementation:

1. ~~Visual lab in production?~~ → **No visual lab.** Design review happens on dev.
2. ~~Members — Clerk deeplink or BFF-proxied invite?~~ → **Clerk deeplink.** Identity stays one place.
3. ~~Cost budgets — UI now or defer?~~ → **Defer.** Usage control is Stripe / Bedrock-credit-driven on Isol8, not Paperclip; surfacing Paperclip's budget UI would be a misleading second source of truth.
4. ~~Dark mode?~~ → **Non-goal v1.**
5. ~~Mobile?~~ → **Non-goal v1.**

No remaining open questions before plan-writing.

## Acceptance criteria (per PR)

For each frontend port PR:

- [ ] Visual lab still renders all atoms without regression.
- [ ] Panel matches upstream's IA: every tab, filter, sort, group, action menu present.
- [ ] All BFF endpoints the panel uses exist + have tests.
- [ ] Empty state is implemented + has its own copy.
- [ ] Manual smoke against dev's seeded Paperclip company, with both populated and empty states verified.
- [ ] Codex review clean (no re-flagged issues from prior PRs in the same series).
- [ ] No regressions on /chat, /marketplace, /admin (CI catches; manual verify on the most-changed atoms).

## Implementation plan

After this spec is approved, the implementation plan goes in `docs/superpowers/plans/2026-05-07-teams-panel-port-editorial-atoms.md` via the `superpowers:writing-plans` skill, broken into the 10 PRs above with task-level steps for PR 1 (the atom layer).
