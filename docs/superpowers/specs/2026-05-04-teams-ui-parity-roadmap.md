# Teams UI Parity Roadmap

**Status:** Active
**Owner:** prasiddha
**Started:** 2026-05-04

## Background

The native `/teams` UI shipped in PR #509 as a "tier-1 minimal" port of the upstream Paperclip UI it replaced (the previous transparent reverse proxy of `dev.company.isol8.co`). Tier-1 was 17 panels covering the core CRUD surfaces. Upstream Paperclip has ~67 page files. After landing the lazy-provisioning fix in PR #514 (so existing personal users actually reach `/teams`), it became visible that several core panels feel sparse vs. what upstream provides — particularly Inbox, Dashboard, and AgentDetail — and that the whole UI lacks live updates.

A gap audit produced this prioritized list of sub-projects to close the most-visible gaps. Each gets its own brainstorm → spec → plan → PR cycle. This file is the single source of truth for what's next; individual specs link back here.

## Sub-projects

| # | Sub-project | Effort | Status | Spec | Plan | PR |
|---|---|---|---|---|---|---|
| 1 | Realtime updates (BFF WS subscriptions + frontend hook for live badges, agent status dots, run progress, dashboard counts) | L | Done | [2026-05-04-teams-realtime-design.md](./2026-05-04-teams-realtime-design.md) | [2026-05-04-teams-realtime.md](../plans/2026-05-04-teams-realtime.md) | [#518](https://github.com/Isol8AI/isol8/pull/518) |
| 2 | Dashboard charts (4 live charts: run activity, success rate, issue status, priority breakdown; recent activity panel) | M | Done | — | [2026-05-06-teams-dashboard-charts.md](../plans/2026-05-06-teams-dashboard-charts.md) | [#539](https://github.com/Isol8AI/isol8/pull/539) |
| 3 | Inbox **deep port** (full Paperclip parity: tabs, filters, search, keyboard, archive/undo + detail pages) (3a auth-fix follow-up: PR #529 — switched /teams/inbox from agent-only inbox-lite to board-user /companies/{co}/issues) | XL | In progress (3a ✅, 3b ✅, 3c ✅, 3d ✅; ApprovalDetail + RunDetail deferred to #3d.2) | [2026-05-04-teams-inbox-deep-port-design.md](./2026-05-04-teams-inbox-deep-port-design.md) | [#3a BFF plan](../plans/2026-05-04-teams-inbox-bff.md) [#3b plan](../plans/2026-05-04-teams-inbox-shared-components.md) [#3c plan](../plans/2026-05-05-teams-inbox-page.md) | [#524](https://github.com/Isol8AI/isol8/pull/524) (#3a) [#533](https://github.com/Isol8AI/isol8/pull/533) (#3b) [#535](https://github.com/Isol8AI/isol8/pull/535) (#3c) [#536](https://github.com/Isol8AI/isol8/pull/536) (#3d) |
| 4 | Command palette (cmd+k for fast nav/search/create across agents/issues/projects) | S | Done | — | [2026-05-05-teams-command-palette.md](../plans/2026-05-05-teams-command-palette.md) | [#537](https://github.com/Isol8AI/isol8/pull/537) |
| 5 | Agent org chart (new panel: agent hierarchy viz with reports_to + live status dots) | L | Done | — | [2026-05-05-teams-org-chart.md](../plans/2026-05-05-teams-org-chart.md) | [#538](https://github.com/Isol8AI/isol8/pull/538) |
| 6 | Issue header + properties pickers v2 (assignee + project pickers editable post-creation; recent-assignee bubble-up via localStorage) | M | In progress | — | [2026-05-09-teams-issue-pickers-v2.md](../plans/2026-05-09-teams-issue-pickers-v2.md) | TBD |

## Dependency graph

```
#1 Realtime  ──┬──> #2 Dashboard charts (live counters)
               ├──> #3 Inbox depth (live badges)
               └──> #5 Org chart (live status dots)

#4 Command palette: independent, can ship anytime
```

Recommended execution order: **#1 first** (foundational), then **#2 + #3 in parallel** (different panels, different reviewers), then **#4** and **#5** in either order.

## Status legend

- **Pending** — backlog; no spec or plan yet.
- **Brainstorming** — design discussion in progress; spec not yet committed.
- **Spec** — design committed; plan not yet written.
- **Plan** — implementation plan committed; PR not yet open.
- **In progress** — PR open; subagent execution in flight.
- **Done** — PR merged.

## Out of scope (for now)

The gap audit identified pages that we deliberately won't port:

- **Admin/operator surfaces:** AdapterManager, PluginManager, InstanceSettings, InstanceGeneralSettings, InstanceExperimentalSettings, CompanyImport/Export, CompanyEnvironments — Paperclip exposes these to instance admins; they're operator concerns, not tenant concerns.
- **Already covered elsewhere:** ProfileSettings (Clerk UserButton + `/settings` in Isol8), UserProfile, Auth/CliAuth/BoardClaim/InviteLanding (handled by Clerk + `/onboarding`).
- **Doesn't fit our model:** Workspaces, ExecutionWorkspaceDetail, ProjectWorkspaceDetail (Isol8 maps workspace → per-user container; no multi-workspace concept), JoinRequestQueue (Isol8 is single-tenant per user, not multi-tenant), CompanyEnvironments (single per-user container).
- **Folded into existing entities:** Goals/GoalDetail (Isol8 folds goal-like work into Issues; no separate entity).

If priorities shift and one of these does become important, add it as a new row above with status `Pending` rather than starting a parallel roadmap.

## Convention

- Each sub-project's spec filename: `docs/superpowers/specs/YYYY-MM-DD-teams-<short-name>-design.md` (e.g. `2026-05-04-teams-realtime-design.md`).
- Each sub-project's plan filename: `docs/superpowers/plans/YYYY-MM-DD-teams-<short-name>.md`.
- When a sub-project enters Brainstorming/Spec/Plan/In-progress, update its row above (status + link).
