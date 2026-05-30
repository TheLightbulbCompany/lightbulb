# Teams Issue Linked-Approvals — Design

**Roadmap:** sub-project **D**, piece **D2** (after D1 sub-issues). Companion to D1/D3/D4/D5.

**Status:** Spec

## Goal

Add a "Linked approvals" section to the `/teams` issue-detail page: list the approvals linked to an issue and approve/reject the pending ones inline — matching upstream Paperclip's IssueDetail Activity-tab approval list.

## Background

- Upstream `GET /api/issues/:id/approvals` (`paperclip issues.ts:3430`) returns the issue's linked approvals (`listApprovalsForIssue(id)`), issue-scoped. Approve/reject go through the approvals router (`POST /approvals/:id/approve|reject`).
- Isol8 already has: the approval primitives in the admin client (`list_approvals`/`approve_approval`/`reject_approval`/`get_approval`), and — from sub-project E — `ApprovalRow` (one-click approve + inline reject reason) and `useApprovalMutations` (approve/reject, revalidating `/teams/approvals` + `/teams/dashboard`). Missing: the per-issue approvals list endpoint + a section that renders it.
- `Approval` type: `{ id; issueId?; title?; status: "pending"|"approved"|"rejected"; createdAt?; decidedAt? }`.

## Decisions

1. **Reuse `ApprovalRow`** (from E) to render each linked approval.
2. **Gate actions on status.** The issue's linked approvals can include resolved ones; `ApprovalRow` currently always shows Approve/Reject (E only ever fed it pending). Add a `status === "pending"` gate: pending → Approve/Reject; resolved → a small status chip ("Approved"/"Rejected"). The E band is unaffected (all its rows are pending).
3. **Per-issue revalidation.** `useApprovalMutations` additionally invalidates any `/teams/issues/{*}/approvals` SWR keys (predicate) so the reused `ApprovalRow` refreshes the issue section after approve/reject. Harmless for the band (no such keys there).
4. **Read + act only.** Link/unlink (`POST`/`DELETE /issues/:id/approvals`) are agent/automated operations — **deferred**; not needed for the read+act UX.
5. **Hide-when-empty.** Render nothing when the issue has no linked approvals (matches upstream's conditional Activity-tab list).

## Architecture

### BFF (`routers/teams/issues.py`) — issue-scoped passthrough
- `GET /issues/{issue_id}/approvals` → `_admin().list_issue_approvals(issue_id)`, wrapped `envelope_list(rows, "approvals")`.

### admin client
- `list_issue_approvals(*, session_cookie, issue_id)` → `GET /api/issues/{issue_id}/approvals` (issue-scoped, mirrors `get_issue`).

### Frontend
- `ApprovalRow.tsx` — add the `status === "pending"` gate (+ a resolved status chip). One small change; preserves the band.
- `hooks/useApprovalMutations.ts` — add the `/teams/issues/{*}/approvals` predicate invalidation in `revalidate`.
- `issues/IssueApprovalsSection.tsx` (new) — `read<{approvals: Approval[]}>("/issues/{id}/approvals")`; render an "Approvals" header + `ApprovalRow` list; render nothing when there are no approvals.
- `IssueDetailPage.tsx` — mount `<IssueApprovalsSection issueId={issueId} />` after `SubIssuesSection`, before the Conversation block.

### SWR / realtime
- Key `/teams/issues/{id}/approvals`. Refreshed after approve/reject (decision 3) and via the existing `teams.activity.logged` event mapping if desired (explicit revalidation is the main path).

## Out of scope (deferred)
- Approval link/unlink UI; approval **detail** page (D-later); the broader Activity tab (run ledger = D3).

## Testing
- BFF: `GET /issues/{id}/approvals` forwards + envelopes (admin `list_issue_approvals`).
- `useApprovalMutations`: approve/reject also invalidate an issue-approvals key (predicate).
- `ApprovalRow`: pending → shows Approve/Reject; resolved (approved/rejected) → shows a status chip, no action buttons. (Keep the existing band tests green.)
- `IssueApprovalsSection`: renders linked approvals; nothing when empty.

## Convention
Plan: `docs/superpowers/plans/2026-05-29-teams-issue-approvals.md`.
