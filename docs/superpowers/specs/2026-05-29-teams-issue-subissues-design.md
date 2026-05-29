# Teams Issue Sub-issues — Design

**Roadmap:** sub-project **D** (Issue detail depth) of the [Teams UI Parity Roadmap](./2026-05-04-teams-ui-parity-roadmap.md), **piece D1 of ~5**. D was decomposed (see roadmap) into: **D1 sub-issues** (this spec), D2 linked approvals, D3 run ledger, D4 documents, D5 richer comments, with document-annotations / typed-agent-interactions / work-products deferred (agent-emission-dependent). This spec covers **D1 only**.

**Status:** Spec

## Goal

Add a **Sub-issues section** to the `/teams` issue-detail page: list an issue's descendant issues, create a sub-issue, and navigate up to the parent — matching upstream Paperclip's IssueDetail behavior, built on our existing SWR-native components.

## Background — current state

- **Our issue detail** (`apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`) renders `IssueHeader` + `IssueComments` + `IssueProperties`. No sub-issues.
- **Our `Issue` type** already has `parentId?: string | null` (`shared/types.ts:146`).
- **Our BFF** (`routers/teams/issues.py`) has list/get/create/patch issue, labels, archive/unarchive, mark-read/unread, comments. The admin client's `list_issues(params=…)` already forwards an arbitrary query string to upstream; `create_issue` maps to `POST /api/companies/{companyId}/issues`. No children endpoints.
- **Upstream** (`paperclip/ui/src/pages/IssueDetail.tsx:3690-3723`) renders a "Sub-issues" section via its `IssuesList` with `searchFilters={{ descendantOf: issue.id, includeBlockedBy: true }}` (the full **descendant** set, flat workflow-sorted), a progress summary (≥2 children), and a "New Sub-issue" creator. Children are created via `POST /issues/:id/children`. The `descendantOf`/`parentId` query filters exist on the upstream issues-list route.

## Decisions

1. **Descendants, flat list.** Match upstream: the section shows ALL descendants (`descendantOf`), workflow-sorted, in a flat list (not a nested indented tree).
2. **Reuse our ported components, not upstream's `IssuesList`.** Upstream `IssuesList` is ~1,973 lines on `@tanstack/react-query` (we use SWR) with a large transitive tree — it is precisely the component we re-implemented as our inbox during #3. We render sub-issues with our existing **`IssueRow`** (rich, prop-driven, read-only here) + a ported **progress summary**. (A list⇄board toggle via our prop-driven `KanbanBoard` is a cheap future add; not in v1.)
3. **Create via the children endpoint, through `NewIssueDialog`.** Reuse `NewIssueDialog` with a new optional `parentId` prop; when set it creates via `POST /issues/{parentId}/children` (which does upstream's parent/blocker wiring) rather than the plain create endpoint.
4. **Parent breadcrumb.** When the current issue has a `parentId`, show a link up to the parent (with the parent's title).
5. **Whitelisted passthrough.** The BFF child-create body is whitelisted (no raw client adapter/policy fields), mirroring `CreateIssueBody`.

## UX & behavior

- **Placement:** a "Sub-issues" section in `IssueDetailPage`, above the comments (upstream renders sub-issues before the chat/activity tabs).
- **When the issue has descendants:** header "Sub-issues", the **progress summary** (only when ≥2: total / done / in-progress / blocked + a "next"/"blocked" target), then the flat workflow-sorted list of `IssueRow`s (each links to the child's own detail page), then a "New Sub-issue" button.
- **When it has none:** just the "New Sub-issue" button (upstream's `shouldRenderRichSubIssuesSection` = render rich list iff loading or count > 0).
- **New Sub-issue:** opens `NewIssueDialog` (parent prefilled, shows "Parent: <identifier/title>"); on success the dialog closes and the section revalidates so the new child appears.
- **Parent breadcrumb:** when `issue.parentId` is set, a "↑ <parent title>" link near the header routing to `/teams/issues/{parentId}`.

## Architecture

### BFF (`routers/teams/issues.py`) — thin passthrough, whitelisted

- `GET /issues/{issue_id}/children` → `_admin().list_issues(company_id, session_cookie, params={"descendantOf": issue_id})`, wrapped `{"issues": [...]}` via `envelope_list`. (Distinct path from `/issues/{issue_id}` — extra segment, no matcher collision.)
- `POST /issues/{issue_id}/children` → `_admin().create_child_issue(...)` with a whitelisted `CreateChildIssueBody` (see Schemas); returns the created child (the upstream response includes the new `Issue`).

### admin client (`core/services/paperclip_admin_client.py`)

- New `create_child_issue(*, session_cookie, company_id, issue_id, body)` → `POST /api/companies/{company_id}/issues/{issue_id}/children` (mirrors `create_issue`'s path/shape).
- Children listing reuses the existing `list_issues(params=…)` (no new list method).

### Schemas (`routers/teams/schemas.py`)

- New `CreateChildIssueBody(_Strict)` mirroring `CreateIssueBody`: `title: str` (required); optional `assignee_agent_id`, `assignee_user_id`, `project_id` (camelCased via the existing `to_camel` alias generator; `extra="forbid"`). No status/adapter/policy fields.

### Frontend

- **`components/teams/issues/SubIssuesSection.tsx`** (new): `useTeamsApi().read<{issues: Issue[]}>("/issues/{id}/children")`; sorts via the ported `workflowSort`; computes the progress summary; renders the `IssueRow` list (read-only — no `onArchive`/`onMarkRead`) + the progress strip + "New Sub-issue" button (opens `NewIssueDialog`). Renders just the button when there are no descendants.
- **`components/teams/issues/hooks/useIssueMutations.ts`**: add `createChild(parentId, input)` → `post("/issues/{parentId}/children", input)`, then revalidate `/teams/issues/{parentId}/children` (+ the issue). Reuses the existing `IssueCreateInput` shape.
- **`components/teams/issues/NewIssueDialog.tsx`**: add optional `parentId?: string` (+ optional `parentLabel?: string` for the "Parent: …" line). When `parentId` is set, submit via `createChild(parentId, …)` instead of `create(…)`; otherwise unchanged.
- **`components/teams/shared/lib/subIssues.ts`** (new): port upstream's pure `buildSubIssueProgressSummary` (+ its `isActionableStatus`/`isTerminalStatus` helpers) and `workflowSort` (from `paperclip/ui/src/lib/workflow-sort.ts` + `issue-detail-subissues.ts`), adapted to our `Issue`/`IssueStatus` types. Pure functions, unit-tested.
- **Parent breadcrumb:** in `IssueDetailPage`, when `issue.parentId` is set, render a link to the parent above the header. Fetch the parent's title via the existing `read("/issues/{parentId}")` (SWR-cached); fall back to "Parent issue" if it hasn't loaded.
- **Mount:** `IssueDetailPage` renders `<SubIssuesSection issueId={issue.id} parentId={issue.parentId} />` above the comments.

### Data flow / SWR / realtime

```
IssueDetailPage(issue)
 ├─ parent breadcrumb: read("/issues/{issue.parentId}") when parentId set
 └─ SubIssuesSection(issue.id)
      ├─ read("/issues/{id}/children")  -> { issues: Issue[] }  (descendants)
      ├─ workflowSort + buildSubIssueProgressSummary
      ├─ IssueRow[]  (read-only, link to /teams/issues/{child.id})
      └─ New Sub-issue -> NewIssueDialog(parentId=id) -> useIssueMutations.createChild
                                   -> POST /issues/{id}/children -> revalidate /teams/issues/{id}/children
```

- SWR key: `/teams/issues/{id}/children`. **Revalidated explicitly after `createChild`** (and on the dialog's success). Live cross-client invalidation of the children key via gateway events is **deferred** (the existing `teams.activity.logged` → `/teams/issues` mapping doesn't match the per-issue children key; adding a predicate is a small future enhancement).

## Types

No new shared types needed beyond the ported lib's own (`SubIssueProgressSummary`). `Issue` already carries `parentId`; descendants are `Issue[]`.

## Error handling

- Children load failure → the section shows an inline "Couldn't load sub-issues" message and still offers "New Sub-issue"; it never blocks the rest of the detail page.
- `createChild` failure → surfaced inline in `NewIssueDialog` (its existing error path), dialog stays open for retry.
- Missing/blank parent title → breadcrumb falls back to "Parent issue".

## Out of scope (deferred)

- **Sibling prev/next navigation** (upstream's chat-tab-footer concern).
- **Tree cost rollup** (`/issues/{id}/cost-summary`) → sub-project F (Costs).
- **`includeBlockedBy` blocker badges** + the issue-relations graph (blocks/relates-to beyond parent/child).
- **List⇄board toggle** for sub-issues (cheap later add — `KanbanBoard` is already prop-driven).
- **Inline status-edit / live badges / view-state persistence** inside sub-issue rows (act on the child's own detail page instead).
- D2–D5 and the agent-emission-dependent pieces (separate specs).

## Testing

- **`subIssues.ts`** (pure): `buildSubIssueProgressSummary` (counts, "next" vs "blocked" target, ≥2 gate, ignores cancelled) and `workflowSort` ordering — unit tests.
- **`SubIssuesSection`**: renders descendants as `IssueRow`s; shows progress summary only at ≥2; empty state shows just the button; "New Sub-issue" opens the dialog; row links target `/teams/issues/{child.id}`.
- **`useIssueMutations.createChild`**: POSTs `/issues/{parent}/children` with the body and revalidates the children key.
- **`NewIssueDialog`**: with `parentId` set, submit routes through `createChild` (not `create`) and shows the parent line.
- **BFF**: `GET /issues/{id}/children` forwards `descendantOf` + envelopes; `POST /issues/{id}/children` whitelists the body (extra keys → 422) and calls `create_child_issue`.
- **Parent breadcrumb**: renders a link to `parentId` when set; absent when not.

## Convention

Plan: `docs/superpowers/plans/2026-05-29-teams-issue-subissues.md`. Update roadmap row **D** when this enters Plan / In-progress / Done (note it's piece D1).
