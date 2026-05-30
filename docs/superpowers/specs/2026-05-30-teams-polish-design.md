# Teams Polish — Design

**Roadmap:** sub-project **H** (Polish) of the Teams UI parity roadmap. Scoped to the "clean trio" — three small, frontend-only parity gaps whose BFF endpoints already exist.

**Status:** Spec

## Goal

Close three small `/teams` parity gaps in one pass: edit existing **Goals**, a standalone **approval-detail** page, and a type **filter** on the Activity feed. All three are frontend-only — the BFF endpoints are already wired (the BFF has been consistently ahead of the UI here).

## Background / scope decisions

A 5-item "Polish" bucket was triaged down to three by research:

- **Dropped — Dashboard date-range.** The 14-day window is hardcoded all the way down, *including upstream Paperclip's own* `dashboardService` (`DASHBOARD_RUN_ACTIVITY_DAYS = 14`, route takes no param). A real selector needs an upstream change — outside the "never modify upstream" lane. Out of scope.
- **Deferred — Routine run-history tab.** The BFF `GET /routines/{id}/runs` exists, but there is no routine *detail* surface to host a tab (RoutinesPanel is a flat table with no detail route). Building a RoutineDetailPanel is a separate small sub-project, not polish.
- **Corrected premise — Goals.** The roadmap's "Goals are folded into Issues; no separate Goal entity" note is **stale**. Goal is a first-class entity; `GoalsPanel` lists goals but cannot edit them.

## The three items

### H1 · Goal edit (`panels/GoalsPanel.tsx`)

- **Current:** `Goal = { id, title, description?, parent_id?, status? }`. `GoalRow` shows title + description only (status is fetched but never displayed). `CreateGoalDialog` (inline) `post`s `/goals`. No edit path.
- **BFF:** `PATCH /goals/{id}` is live; `PatchGoalBody` whitelists `title`, `description`, `parent_id`, `status`. `useTeamsApi().patch` exists.
- **Design:**
  1. **Status chip** — `GoalRow` renders `node.status` as a small chip when present, via a status→color map (`active` → forest, `achieved` → forest/dim, `cancelled` → mute, `planned` → mute/default). Status vocabulary = upstream `GOAL_STATUSES = ["planned","active","achieved","cancelled"]`.
  2. **Edit affordance** — `GoalRow` gets an Edit button that calls an `onEdit(goal)` callback; `GoalsPanel` lifts an `editing: Goal | null` state (mirroring its existing `creating` state) and renders an `EditGoalDialog` for it.
  3. **`EditGoalDialog`** — mirrors `CreateGoalDialog` (same `EditorialDialog` + fields) but pre-fills title/description and adds a **status `<select>`** (the four `GOAL_STATUSES`); submit `patch`es `/goals/{id}` with `{ title, description, status }`; on success `mutate()`s the `/goals` list. (Drops `parent_id` re-parenting and `level` — out of clean-trio scope.)
- **Out of scope:** `level` editing (needs a `PatchGoalBody` change), re-parenting, owner assignment.

### H2 · Approval-detail page (new route `app/teams/approvals/[approvalId]/page.tsx`)

- **Current:** approvals appear only as inline rows (`inbox/ApprovalRow.tsx`, `issues/IssueApprovalsSection.tsx`, the inbox "Needs attention" band). The standalone Approvals panel is retired (redirects to inbox). No detail route. `Approval = { id, issueId?, title?, status, createdAt?, decidedAt? }`.
- **BFF:** `GET /approvals/{id}` (`routers/teams/approvals.py`) is live — its docstring says it was built "for the approval detail page that Inbox approval rows link into." It returns the full upstream approval object (richer than the row type). `useApprovalMutations` (approve/reject) already exists.
- **Design:**
  1. **`ApprovalDetail` type** — extend the row `Approval` with the optional richer fields `get_approval` returns: an approval `kind`/`type`, a `payload` (free JSON), `requestedByAgentId`, and `decisionNote`. (Exact upstream field names are pinned in the plan after inspecting a live `get_approval` response / the upstream Approval shape; the panel renders each defensively with `?.`.)
  2. **`panels/ApprovalDetailPanel.tsx`** — `read<ApprovalDetail>("/approvals/{id}")`; renders: a header (type/label + `id` + a status badge reusing the existing approval-status styling), the **requested-by** agent (resolved to a name from the `agents` list when available), the request **payload** (pretty-printed JSON in a `<pre>` with a "show raw" toggle), the **decision note** if present, and — when `status === "pending"` — **Approve / Reject** actions via `useApprovalMutations` (reject reuses the inline-reason pattern from `ApprovalRow`). On decision, revalidate and reflect the new status.
  3. **Route** — `app/teams/approvals/[approvalId]/page.tsx` (a thin `"use client"` page reading the `approvalId` param and rendering `<ApprovalDetailPanel approvalId={…} />`), mirroring the existing run-detail route `app/teams/agents/[agentId]/runs/[runId]/page.tsx`.
  4. **Links in** — `ApprovalRow` and `IssueApprovalsSection` rows get a "View" link to `/teams/approvals/{id}` (same pattern as the run "View" links), so the band + issue section both reach the detail.
- **Out of scope (deferred — need extra BFF endpoints):** the comments thread (`listComments`/`addComment`) and the linked-issues list (`approvalsApi.listIssues`); request-revision / mark-resubmitted actions.

### H3 · Activity type-filter (`panels/ActivityPanel.tsx`)

- **Current:** `ActivityEvent = { id, type, actor?, target?, createdAt?, description? }`; reads `/activity` → `{ events }`; renders a flat list with `e.type` as an overline. No filter.
- **Design (frontend-only, mirrors upstream `Activity.tsx`):** add a type `<select>` above the list whose options are `"all"` plus the **distinct `e.type` values present in the loaded feed** (computed with `useMemo`); a `useState` holds the selection; the rendered list is filtered to the chosen type (all when `"all"`). No BFF change — filters the already-returned list. Empty-after-filter shows a "No activity of this type" message; the existing "No activity yet" empty state is preserved for a genuinely empty feed.
- **Out of scope:** server-side filters (agent/entityId/limit) — upstream's route supports them but the Isol8 BFF `/activity` forwards no params; deferred.

## Architecture summary

| Item | New files | Modified files | BFF |
|---|---|---|---|
| H1 Goal edit | — | `panels/GoalsPanel.tsx` (status chip + `EditGoalDialog` + edit wiring) | `PATCH /goals/{id}` (existing) |
| H2 Approval detail | `app/teams/approvals/[approvalId]/page.tsx`, `panels/ApprovalDetailPanel.tsx` | `shared/types.ts` (`ApprovalDetail`), `inbox/ApprovalRow.tsx` + `issues/IssueApprovalsSection.tsx` ("View" links) | `GET /approvals/{id}` (existing) |
| H3 Activity filter | — | `panels/ActivityPanel.tsx` (type `<select>` + client filter) | none |

No backend/BFF changes. All three ride existing endpoints.

## Packaging

One PR (`H — teams polish`), three per-item commits (bundled — they are all small, independent polish in the same surface; `feedback_bundle_related_fixes`). Per-item targeted tests; full suite/tsc/build in a final verification task.

## Testing

- **H1:** `GoalRow` shows a status chip when `status` is set (and none when absent); opening `EditGoalDialog` pre-fills the goal and a status-`<select>` change → `patch` is called with `{title, description, status}` and the list revalidates.
- **H2:** `ApprovalDetailPanel` renders header/status/payload/requested-by from a mocked `read`; pending → Approve/Reject visible and wired to `useApprovalMutations`; resolved → actions hidden, status shown; raw-JSON toggle reveals the payload. `ApprovalRow`/`IssueApprovalsSection` render a "View" link to `/teams/approvals/{id}`.
- **H3:** `ActivityPanel` renders the type `<select>` with distinct types; selecting one filters the list; "all" shows everything; filtered-empty shows the per-type empty message.

## Convention

Plan: `docs/superpowers/plans/2026-05-30-teams-polish.md`.
