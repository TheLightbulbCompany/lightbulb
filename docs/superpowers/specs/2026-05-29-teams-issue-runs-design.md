# Teams Issue Run List — Design

**Roadmap:** sub-project **D**, piece **D3** (run ledger). After D1 sub-issues + D2 linked approvals.

**Status:** Spec

## Goal

Add a "Runs" section to the `/teams` issue-detail page: a read-only, newest-first list of the agent runs for an issue, each linking to its run-detail page. A pragmatic Isol8 take on upstream Paperclip's `IssueRunLedger`.

## Background

- Upstream `GET /api/issues/:id/runs` (`paperclip activity.ts:74`) → `runsForIssue(companyId, issueId)` → `RunForIssue[]` (issue-scoped). The upstream `IssueRunLedger` (854 lines) merges runs + live-runs + activity with liveness states, watchdog decisions, output-silence, model profiles, and environment/lease detail.
- Isol8 has run primitives but no per-issue run list (`list_runs` is **agent**-scoped). The run-detail route `/teams/agents/{agentId}/runs/{runId}` already exists (shipped in B).
- `RunForIssue` fields we use: `runId`, `status` (string), `agentId`, `startedAt`, `finishedAt`, `createdAt`. The rest (liveness, environment, lease, retry, usage/result JSON) is operator-flavored.

## Decisions

1. **Read-only run list, not the full ledger.** Render a flat, newest-first list of runs. **Drop** the live-run merge, watchdog decision controls, output-silence, liveness copy, model profiles, and environment/lease detail — operator concerns / out of scope (consistent with the roadmap's out-of-scope flags).
2. **Each run row:** a status chip, the agent name (resolved from the `agents` prop), relative time (`startedAt ?? createdAt`), and a "View" link to `/teams/agents/{agentId}/runs/{runId}` (hidden when `agentId` is absent).
3. **Newest first** (by `startedAt ?? createdAt` desc) — ledger ordering.
4. **Hide-when-empty.**

## Architecture

### BFF (`routers/teams/issues.py`) — issue-scoped passthrough
- `GET /issues/{issue_id}/runs` → `_admin().list_issue_runs(issue_id)`, wrapped `envelope_list(rows, "runs")`.

### admin client
- `list_issue_runs(*, session_cookie, issue_id)` → `GET /api/issues/{issue_id}/runs` (issue-scoped, mirrors `get_issue`).

### Frontend
- `shared/types.ts` — add a minimal `IssueRun` type: `{ runId; status; agentId?; startedAt?; finishedAt?; createdAt? }` (field names match upstream `RunForIssue`; `status` is a free string).
- `issues/IssueRunsSection.tsx` (new) — `read<{runs: IssueRun[]}>("/issues/{id}/runs")`; sort newest-first; render a "Runs" header + a row per run (status chip + agent name + age + View link); render nothing when empty. Takes `{ issueId, agents }`.
- `IssueDetailPage.tsx` — mount `<IssueRunsSection issueId={issueId} agents={agents} />` after `IssueApprovalsSection`, before the Conversation block.

### Status chip
A tiny inline mapping: `succeeded`/`done` → forest; `failed`/`error` → danger; everything else → mute. (Run status is a free upstream string.)

## Out of scope (deferred)
Live-run merge + watchdog controls + output-silence + liveness/model-profile detail + environment/lease info; the per-issue **activity** feed (`/issues/:id/activity`); work products.

## Testing
- BFF: `GET /issues/{id}/runs` forwards + envelopes (admin `list_issue_runs`).
- `IssueRunsSection`: renders runs newest-first with agent name + status + View link; View hidden when no `agentId`; renders nothing when empty.

## Convention
Plan: `docs/superpowers/plans/2026-05-29-teams-issue-runs.md`.
