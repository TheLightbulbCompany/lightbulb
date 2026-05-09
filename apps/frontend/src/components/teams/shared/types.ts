// apps/frontend/src/components/teams/shared/types.ts

// Ported from upstream Paperclip
// (https://github.com/Paperclip-AI/paperclip/tree/main/packages/shared/src/types)
// (MIT, © 2025 Paperclip AI). Subset retained for IssueRow / IssueColumns /
// IssueFiltersPopover. Full type lives in upstream packages/shared/src/types/.
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

export type IssueStatus =
  | "backlog"
  | "todo"
  | "in_progress"
  | "in_review"
  | "pending"
  | "review"
  | "done"
  | "cancelled"
  | "won_t_do"
  | "blocked"
  | "duplicate"
  | "open"
  | "closed";

export const ISSUE_STATUSES: readonly IssueStatus[] = [
  "todo",
  "in_progress",
  "in_review",
  "pending",
  "review",
  "done",
  "won_t_do",
  "blocked",
  "duplicate",
];

export type IssuePriority = "critical" | "high" | "medium" | "low";

export interface IssueLabel {
  id: string;
  name: string;
  color?: string | null;
}

export interface IssueProject {
  id: string;
  name: string;
  color?: string | null;
}

// Rich `blockerAttention` shape — hoisted from upstream Paperclip's StatusIcon
// so it can live alongside the slim `Issue` interface without creating an
// import cycle through the components/ subtree. Re-exported from
// `components/StatusIcon.tsx` for back-compat with existing call sites.
export type IssueBlockerAttentionState =
  | "none"
  | "covered"
  | "stalled"
  | "needs_attention";

export type IssueBlockerAttentionReason =
  | "active_child"
  | "active_dependency"
  | "stalled_review"
  | "attention_required"
  | null;

export interface IssueBlockerAttention {
  state: IssueBlockerAttentionState;
  reason: IssueBlockerAttentionReason;
  unresolvedBlockerCount: number;
  coveredBlockerCount: number;
  stalledBlockerCount: number;
  attentionBlockerCount: number;
  sampleBlockerIdentifier: string | null;
  sampleStalledBlockerIdentifier: string | null;
}

// Rich productivity-review payload — hoisted from upstream Paperclip's
// ProductivityReviewBadge so the `Issue.productivityReview` field can carry
// the full review metadata that StatusIcon / IssueRow consume. Re-exported
// from `components/ProductivityReviewBadge.tsx` for back-compat.
export type IssueProductivityReviewTrigger =
  | "no_comment_streak"
  | "long_active_duration"
  | "high_churn";

export interface IssueProductivityReview {
  reviewIssueId: string;
  reviewIdentifier: string | null;
  status: IssueStatus | string;
  trigger: IssueProductivityReviewTrigger | null;
  noCommentStreak: number | null;
}

/** Single comment on an issue. Returned from GET /teams/issues/{id}/comments. */
export interface IssueComment {
  id: string;
  body: string;
  createdAt: string;
  /** When the comment was authored by a human. Mutually exclusive with authorAgentId. */
  authorUserId?: string | null;
  /** When the comment was authored by an agent. */
  authorAgentId?: string | null;
  /** Display hint derived by callers from authorUserId/authorAgentId. */
  authorKind?: "user" | "agent" | null;
}

/** Body for POST /teams/issues. Slim shape for v1 NewIssueDialog.
 *
 * NOTE: `status` is intentionally absent. The Teams BFF `CreateIssueBody`
 * schema (apps/backend/routers/teams/schemas.py) sets ``extra="forbid"`` and
 * does NOT whitelist ``status`` — POSTs that include it 422. Issue starting
 * status is owned by upstream Paperclip's create handler. Use ``IssueUpdateInput``
 * via PATCH if you need to change status post-creation.
 */
export interface IssueCreateInput {
  title: string;
  description?: string;
  priority?: IssuePriority;
  projectId?: string;
  assigneeAgentId?: string;
}

/** Body for PATCH /teams/issues/{id}. */
export interface IssueUpdateInput {
  title?: string;
  description?: string;
  status?: IssueStatus;
  priority?: IssuePriority;
  /** ``null`` explicitly clears the project (sent as ``projectId: null`` in the
   * PATCH body). ``undefined`` leaves the project unchanged. */
  projectId?: string | null;
  /** ``null`` explicitly unassigns (sent as ``assigneeAgentId: null`` in the
   * PATCH body). ``undefined`` leaves the assignee unchanged. */
  assigneeAgentId?: string | null;
}

export interface Issue {
  id: string;
  identifier?: string | null;
  title: string;
  description?: string | null;
  status: IssueStatus;
  priority?: IssuePriority | null;
  labels?: IssueLabel[] | null;
  project?: IssueProject | null;
  parentId?: string | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  createdByUserId?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  lastActivityAt?: string | null;
  lastExternalCommentAt?: string | null;
  blockerAttention?: boolean | IssueBlockerAttention | null;
  productivityReview?: IssueProductivityReview | null;
  unread?: boolean | null;
  archivedAt?: string | null;
}

export interface Approval {
  id: string;
  issueId?: string | null;
  title?: string | null;
  status: "pending" | "approved" | "rejected";
  createdAt?: string | null;
  decidedAt?: string | null;
}

export interface HeartbeatRun {
  id: string;
  agentId?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  startedAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
}

export interface CompanyMember {
  userId: string;
  name?: string | null;
  email?: string | null;
  imageUrl?: string | null;
}

export interface CompanyAgent {
  id: string;
  name: string;
  iconUrl?: string | null;
}

// ─── Dashboard ────────────────────────────────────────────────────────────
//
// Mirrors upstream Paperclip's dashboard summary
// (paperclip/server/src/services/dashboard.ts:summary). Returned by
// `GET /teams/dashboard` since the BFF un-flatten in PR #2 task 1.

export interface DashboardRunActivityDay {
  date: string; // YYYY-MM-DD
  succeeded: number;
  failed: number;
  other: number;
  total: number;
}

export interface DashboardAgentBuckets {
  active?: number;
  running?: number;
  paused?: number;
  error?: number;
}

export interface DashboardTaskBuckets {
  open?: number;
  inProgress?: number;
  blocked?: number;
  done?: number;
}

export interface DashboardCosts {
  monthSpendCents: number;
  monthBudgetCents: number;
  monthUtilizationPercent: number;
}

export interface DashboardBudgets {
  pendingApprovals: number;
}

export interface DashboardSummary {
  agents: DashboardAgentBuckets;
  tasks: DashboardTaskBuckets;
  runActivity: DashboardRunActivityDay[];
  costs: DashboardCosts;
  pendingApprovals: number;
  budgets: DashboardBudgets;
}

export interface DashboardResponse {
  dashboard: DashboardSummary;
  sidebar_badges?: Record<string, number>;
}
