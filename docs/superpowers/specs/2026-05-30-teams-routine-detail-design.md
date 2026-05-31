# Teams Routine Detail + Run-History — Design

**Roadmap:** the deferred piece of sub-project **H** (the "routine run-history tab"). It was deferred because there was no routine detail surface to host it; this builds that surface.

**Status:** Spec

## Goal

Add a **routine detail page** at `/teams/routines/{id}` with a **rich header** (title, status, schedule/triggers, assignee) and a read-only **run-history list** — the deferred H item. Reached by making `RoutinesPanel` rows navigable.

## Background

- `RoutinesPanel` is a flat `PanelDataTable` (name + cron + agent + enabled-toggle + Run-now/Delete); its rows **don't navigate** and there's no routine detail route/panel.
- The BFF already has `GET /routines/{id}/runs` (`work.py:98` → `list_routine_runs` → upstream `GET /api/routines/{id}/runs`, returns `RoutineRunSummary[]`). It has **no single-routine getter**.
- Upstream `GET /api/routines/{id}` returns the rich `RoutineDetail` (extends `Routine`: `title`, `status`, `description`, `assigneeAgentId`, plus `assignee`, `triggers[]`, `recentRuns`, `project`). We add a thin BFF passthrough for it.
- **Two routine shapes coexist:** the Isol8 `/routines` *list* presents a slim shape the local `RoutinesPanel.Routine` types (`{id, name, cron, agent_id, enabled}`); the *detail* uses the rich upstream `RoutineDetail` (camelCase `title`/`status`/`triggers`). They only share `id` (used by the "Open →" link). The detail panel uses the rich shape exclusively — no mixing.

## Decisions

1. **New BFF `GET /routines/{routine_id}`** — a 2-method copy of `get_project`: admin-client `get_routine(*, session_cookie, routine_id)` → `_get("/api/routines/{routine_id}")`, and a `work.py` route that calls it with `ctx.session_cookie`. **Scoping:** like its siblings, the call passes the *user's* `ctx.session_cookie`, so upstream enforces company access (no IDOR / admin-global access).
2. **New route + panel** — `app/teams/routines/[routineId]/page.tsx` (thin `useParams` wrapper, copy of `approvals/[approvalId]/page.tsx`) → `RoutineDetailPanel` in `components/teams/panels/` (alongside `ApprovalDetailPanel`). NOT registered in `TeamsPanelRouter.PANELS` (that map is sidebar-only).
3. **`RoutineDetailPanel`** reads `/routines/{id}` (rich header) + `/routines/{id}/runs` (history). **Header:** title + a status chip + description (if any) + assignee name (`assignee?.name`) + a **schedule** summary from `triggers[]` (per trigger: `kind`, `cronExpression`, `timezone`, next-run via `timeAgo(nextRunAt)`, and a paused marker when `!enabled`). **Run-history** below.
4. **`RoutinesPanel` "Open →" column** — add a right-aligned column linking `/teams/routines/{r.id}`, mirroring `IssuesPanel`. (Keep the existing Run-now/Delete/enabled actions.)
5. **Types** — add to `shared/types.ts`: `RoutineDetail`, `RoutineTriggerSummary`, `RoutineAgentSummary`, `RoutineRun` (rich camelCase subsets of upstream). The slim list `Routine` stays local in `RoutinesPanel` (unchanged).
6. **Run row rendering** (mirrors upstream `RoutineDetail` "runs" tab — the run-execution history, NOT the config "history" tab): `source` + a `status` badge (`failed`/`error`→danger, `succeeded`/`completed`/`done`→forest, else mute; underscores→spaces) + `trigger.label ?? trigger.kind` + a link to `linkedIssue` (`/teams/issues/{linkedIssue.id}`) when present + `timeAgo(triggeredAt)`. Newest-first by `triggeredAt`. Show `failureReason` on a failed run. "No runs yet." when empty.

## Architecture

| Piece | File | BFF |
|---|---|---|
| Admin method `get_routine` | `core/services/paperclip_admin_client.py` (after `list_routine_runs`) | → upstream `GET /api/routines/{id}` |
| Route `GET /routines/{routine_id}` | `routers/teams/work.py` (next to the other routine routes) | new |
| Types | `components/teams/shared/types.ts` (`RoutineDetail`/`RoutineTriggerSummary`/`RoutineAgentSummary`/`RoutineRun`) | — |
| `RoutineDetailPanel` | `components/teams/panels/RoutineDetailPanel.tsx` (new) | reads `/routines/{id}` + `/routines/{id}/runs` |
| Route page | `app/teams/routines/[routineId]/page.tsx` (new) | — |
| "Open →" link | `components/teams/panels/RoutinesPanel.tsx` (add a column) | — |

## Out of scope (deferred)
Trigger create/edit, secrets (webhook URLs/signing), routine revision history (the upstream "history" tab) + restore, the activity tab, variables/env editing, run-now / pause from the detail (the list already has Run-now/Delete/enabled), and `recentRuns` from the detail payload (we use the fuller `/routines/{id}/runs`, which returns up to 50).

## Testing
- **BFF:** `GET /routines/{id}` forwards to the admin `get_routine` with the issue id + session cookie (mirrors the `get_project`/`list_issue_runs` test pattern).
- **`RoutineDetailPanel`:** renders the header (title + status + a trigger's schedule + assignee) from a mocked `/routines/{id}` read; renders the run-history newest-first with status badge + trigger + a linked-issue link + relative time; "No runs yet." when the runs read is empty; a `failed` run shows its `failureReason`.
- **`RoutinesPanel`:** rows render an "Open →" link to `/teams/routines/{id}` (keep existing tests green).

## Convention
Plan: `docs/superpowers/plans/2026-05-30-teams-routine-detail.md`.
