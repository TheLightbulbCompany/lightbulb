// apps/frontend/src/components/teams/shared/types.ts

// Ported from upstream Paperclip
// (https://github.com/Paperclip-AI/paperclip/tree/main/packages/shared/src/types)
// (MIT, © 2025 Paperclip AI). Subset retained for IssueRow / IssueColumns /
// IssueFiltersPopover. Full type lives in upstream packages/shared/src/types/.

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

// How a task executes once an agent picks it up. Mirrors upstream Paperclip's
// createIssueSchema work-mode enum: ``standard`` runs autonomously, ``ask``
// pauses for input, ``planning`` plans before acting.
export type IssueWorkMode = "standard" | "ask" | "planning";

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

/** Body for POST /teams/issues. Composer shape for NewIssueDialog.
 *
 * The Teams BFF `CreateIssueBody` schema (apps/backend/routers/teams/schemas.py)
 * whitelists ``status`` and ``workMode`` (camelCase aliases) and forwards both
 * to upstream Paperclip's createIssueSchema, so the composer can preset the
 * starting status + execution mode without a follow-up PATCH. Both are
 * optional: omit them and the server defaults to backlog/standard.
 */
export interface IssueCreateInput {
  title: string;
  description?: string;
  priority?: IssuePriority;
  projectId?: string;
  assigneeAgentId?: string;
  status?: IssueStatus;
  workMode?: IssueWorkMode;
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
  /** Full desired set of blocker issue ids (replace semantics — the whole
   * array is sent, matching upstream Paperclip's "Blocked by" picker). The
   * BFF aliases this to ``blockedByIssueIds`` on the wire. ``undefined``
   * leaves the relations unchanged. */
  blockedByIssueIds?: string[];
  /** Reparent (a parent issue id) or ``null`` to clear the parent. The BFF
   * camelCase-aliases this to ``parentId``. ``undefined`` leaves the parent
   * unchanged. */
  parentId?: string | null;
  /** Assign a human company member by Clerk user id, or ``null`` to clear it.
   * Mutually exclusive with ``assigneeAgentId`` — the picker sends the other
   * as ``null`` so only one assignee kind is ever set. ``undefined`` leaves
   * the user-assignee unchanged. */
  assigneeUserId?: string | null;
  /** Full desired set of label ids (replace semantics — the whole array is
   * sent, matching the multi-select rail control). The BFF camelCase-aliases
   * this to ``labelIds``. ``undefined`` leaves the labels unchanged. */
  labelIds?: string[];
  /** An ISO timestamp hides the task (it drops out of the Tasks list);
   * ``null`` restores it. ``undefined`` leaves the hidden state unchanged.
   * The BFF whitelists ``hiddenAt`` and forwards it to upstream Paperclip. */
  hiddenAt?: string | null;
  /** Execution policy carrying the monitor schedule AND the reviewer/approver
   * stages. The wire PATCH has REPLACE semantics — Paperclip overwrites the
   * whole executionPolicy with what is sent — so monitor and stages would clobber
   * each other if patched in isolation. Callers MUST therefore build the full
   * desired policy (preserving the sub-fields they are not editing) via the
   * read-modify-write helper in ``issue-execution-policy.ts`` before sending it.
   * An explicit ``null`` clears the policy entirely. The BFF camelCase-aliases
   * the outer key to ``executionPolicy`` and forwards the nested dict verbatim.
   * ``undefined`` leaves it unchanged. */
  executionPolicy?: IssueExecutionPolicy | null;
}

/** A slim issue summary used by the "Blocked by" / "Blocking" relation rows.
 *
 * Ported from upstream Paperclip's ``IssueRelationIssueSummary``
 * (packages/shared/src/types/issue.ts) (MIT, © 2025 Paperclip AI). Trimmed to
 * the fields the Teams relation chips render — upstream's recovery/terminal-
 * blocker fields are deferred. */
export interface IssueRelationIssueSummary {
  id: string;
  identifier?: string | null;
  title: string;
  status: IssueStatus;
}

/** The monitor schedule that rides on an issue's executionPolicy. Mirrors
 * upstream Paperclip's executionPolicy.monitor — an agent re-checks the task at
 * ``nextCheckAt``, optionally naming an external service it is watching. Only
 * the fields the rail renders/edits are modeled; upstream's scheduledBy/kind
 * are carried for round-trip fidelity but not surfaced. */
export interface IssueMonitorPolicy {
  nextCheckAt: string;
  notes?: string | null;
  serviceName?: string | null;
  scheduledBy?: string | null;
  kind?: string | null;
  // Upstream-managed fields the rail editor never sets but must round-trip
  // unchanged: a reschedule from the rail merges the three editable fields onto
  // the existing monitor (see executionPolicy.patchExecutionPolicy) so these
  // are preserved rather than reset to their schema defaults.
  externalRef?: string | null;
  timeoutAt?: string | null;
  maxAttempts?: number | null;
  recoveryPolicy?: string | null;
}

/** A single participant in a reviewer/approver stage. Mirrors upstream
 * Paperclip's issueExecutionStageParticipantSchema
 * (packages/shared/src/validators/issue.ts). A participant is EITHER an agent
 * (``type: "agent"`` + ``agentId``) OR a human user (``type: "user"`` +
 * ``userId``) — never both. Upstream's zod superRefine rejects a participant
 * that sets the wrong id for its kind, so the builder always emits ``null`` for
 * the unused field. ``id`` is the stable per-participant uuid upstream assigns;
 * it round-trips so re-selecting an existing participant keeps its identity. */
export interface IssueExecutionStageParticipant {
  type: "agent" | "user";
  agentId?: string | null;
  userId?: string | null;
  id?: string;
}

/** A reviewer or approver stage on an issue's executionPolicy. Mirrors
 * upstream's issueExecutionStageSchema. ``type`` is the upstream
 * ISSUE_EXECUTION_STAGE_TYPES enum (``"review"`` | ``"approval"``).
 * ``approvalsNeeded`` is a literal ``1`` upstream (the only value the schema
 * accepts today); it is carried so the emitted stage matches upstream exactly.
 * An empty-participant stage is never emitted — the builder drops a stage with
 * no participants rather than sending an empty one. */
export interface IssueExecutionStage {
  id?: string;
  type: "review" | "approval";
  approvalsNeeded?: 1;
  participants: IssueExecutionStageParticipant[];
}

export interface IssueExecutionPolicy {
  /** Upstream ISSUE_EXECUTION_POLICY_MODES ("normal" | "auto"). Preserved
   * round-trip; the rail does not edit it, so the builder keeps whatever the
   * issue already carried (defaulting to "normal"). */
  mode?: "normal" | "auto";
  monitor?: IssueMonitorPolicy | null;
  /** Reviewer + approver stages. Each ``type`` appears at most once. Absent or
   * empty means no review/approval gate. */
  stages?: IssueExecutionStage[];
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
  /** Issues that block this one (the editable "Blocked by" relation row). */
  blockedBy?: IssueRelationIssueSummary[] | null;
  /** Issues blocked by this one (the read-only "Blocking" relation row). */
  blocks?: IssueRelationIssueSummary[] | null;
  productivityReview?: IssueProductivityReview | null;
  unread?: boolean | null;
  archivedAt?: string | null;
  /** Identifiers of issues this one references (e.g. ["ISO-5","ISO-7"]).
   * Surfaced flat by upstream alongside a richer relatedWork; only the flat
   * identifier list is rendered as read-only "Related Tasks" chips. */
  referencedIssueIdentifiers?: string[] | null;
  /** Execution policy carrying the monitor schedule (the Monitor rail row) and
   * the reviewer/approver stages (the Reviewers/Approvers rail rows). The BFF's
   * get_issue passthrough returns upstream's executionPolicy shape verbatim.
   * ``null``/absent means no monitor and no review/approval gate. */
  executionPolicy?: IssueExecutionPolicy | null;
}

export interface Approval {
  id: string;
  issueId?: string | null;
  /** Upstream never sends a top-level `title` on the list row — the label is
   *  synthesized from `type` + `payload` (see `approvalLabel`). Kept optional
   *  for forward-compat but should not be relied on. */
  title?: string | null;
  /** Upstream approval `type` (e.g. "hire_agent", "request_board_approval");
   *  drives the synthesized row label. */
  type?: string | null;
  /** Redacted payload subset upstream surfaces on the list row; the label
   *  reads `title` / `name` / `summary` / `recommendedAction` from it. */
  payload?: Record<string, unknown> | null;
  /** Upstream APPROVAL_STATUSES. `revision_requested` is actionable (the agent
   *  asked for changes) and must NOT be hidden from the triage band. */
  status:
    | "pending"
    | "revision_requested"
    | "approved"
    | "rejected"
    | "cancelled";
  createdAt?: string | null;
  decidedAt?: string | null;
}

/** Full approval object from GET /approvals/{id} (upstream Approval subset). `status` is a free string (more values than the row union). */
export interface ApprovalDetail {
  id: string;
  type?: string | null;
  status: string;
  requestedByAgentId?: string | null;
  requestedByUserId?: string | null;
  payload?: Record<string, unknown> | null;
  decisionNote?: string | null;
  decidedAt?: string | null;
  createdAt?: string | null;
}

export interface HeartbeatRun {
  id: string;
  agentId?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  startedAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
}

/** A run for an issue (upstream RunForIssue subset). `status` is a free string. */
export interface IssueRun {
  runId: string;
  status: string;
  agentId?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
}

/**
 * A value a run-now caller can supply for a routine variable. Mirrors
 * upstream `routineVariableValueSchema`
 * (paperclip/packages/shared/src/validators/routine.ts) — a string, finite
 * number, or boolean.
 */
export type RoutineVariableValue = string | number | boolean;

/** The variable types a routine can declare (upstream ROUTINE_VARIABLE_TYPES). */
export type RoutineVariableType =
  | "text"
  | "textarea"
  | "number"
  | "boolean"
  | "select";

/**
 * One variable a routine DEFINES (upstream `RoutineVariable`). The run-now
 * editor reads these to build its inputs: `select` renders a dropdown over
 * `options`, `required` drives validation. The Run button collects one
 * {@link RoutineVariableValue} per name into the run body's `variables` map.
 */
export interface RoutineVariable {
  name: string;
  label?: string | null;
  type?: RoutineVariableType;
  defaultValue?: RoutineVariableValue | null;
  required?: boolean;
  options?: string[];
}

/** Assignee summary on a routine (upstream RoutineAgentSummary subset). */
export interface RoutineAgentSummary {
  id: string;
  name: string;
  role?: string;
  title?: string | null;
}

/** A routine trigger / schedule (upstream RoutineTrigger subset). */
export interface RoutineTriggerSummary {
  id: string;
  kind: string;
  label?: string | null;
  enabled?: boolean;
  cronExpression?: string | null;
  timezone?: string | null;
  nextRunAt?: string | null;
}

/** Single routine from GET /routines/{id} (upstream RoutineDetail subset). */
export interface RoutineDetail {
  id: string;
  title: string;
  status?: string | null;
  description?: string | null;
  assigneeAgentId?: string | null;
  assignee?: RoutineAgentSummary | null;
  triggers?: RoutineTriggerSummary[];
  // Variable definitions the routine declares. A run-now caller must collect a
  // value per variable; empty/absent means the routine takes no input.
  variables?: RoutineVariable[];
  // Upstream Routine carries createdAt/updatedAt verbatim through the BFF;
  // surfaced in the detail page's Properties aside.
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** A routine run (upstream RoutineRunSummary subset). `status`/`source` are free strings. */
export interface RoutineRun {
  id: string;
  source: string;
  status: string;
  triggeredAt?: string | null;
  startedAt?: string | null;
  createdAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
  linkedIssue?: {
    id: string;
    identifier?: string | null;
    title?: string | null;
  } | null;
  trigger?: { id: string; kind: string; label?: string | null } | null;
}

/** A document attached to an issue (upstream IssueDocument subset). `body` is inline markdown. */
export interface IssueDocument {
  id: string;
  key: string;
  title?: string | null;
  format?: string | null;
  body?: string | null;
  updatedByAgentId?: string | null;
  updatedByUserId?: string | null;
  updatedAt?: string | null;
  createdAt?: string | null;
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
  /** Paperclip agent role e.g. "ceo" | "engineer" | "general". */
  role?: string | null;
  /** Short one-liner shown in the roster. */
  title?: string | null;
  /** Longer freeform description Friday reads when orchestrating. */
  capabilities?: string | null;
  /** Paperclip icon-picker value (opaque AGENT_ICON_NAMES string) — NOT a URL.
   *  There is no avatar-URL field on the agent wire shape; the org chart and
   *  roster render a generic Bot glyph rather than an image. */
  icon?: string | null;
  /** Runtime status: "idle" | "running" | "error" | "paused" | "terminated". */
  status?: string | null;
  /** ID of the agent this one reports to (null/absent = root). */
  reportsTo?: string | null;
  /** Server-stamped: this is an Isol8-seeded agent (Friday/Atlas) and is
   *  locked — undeletable, meta read-only. Determined from persisted
   *  seeded-agent IDs by the BFF, never from role/name. */
  seeded?: boolean;
}

// ─── Dashboard ────────────────────────────────────────────────────────────
//
// Mirrors upstream Paperclip's dashboard summary
// (paperclip/server/src/services/dashboard.ts:summary). Returned by
// `GET /teams/dashboard`.

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

// ─── Goals ──────────────────────────────────────────────────────────────────
//
// Ported from upstream Paperclip's Goal shape (paperclip/packages/shared/
// src/types/goal.ts + constants.ts) (MIT, © 2025 Paperclip AI). The BFF
// proxies upstream verbatim, so the wire shape is upstream camelCase.

/** Upstream GOAL_STATUSES. */
export type GoalStatus = "planned" | "active" | "achieved" | "cancelled";

/** Upstream GOAL_LEVELS. */
export type GoalLevel = "company" | "team" | "agent" | "task";

export const GOAL_STATUSES: readonly GoalStatus[] = [
  "planned",
  "active",
  "achieved",
  "cancelled",
];

export const GOAL_LEVELS: readonly GoalLevel[] = [
  "company",
  "team",
  "agent",
  "task",
];

/** Full goal row from GET /goals/{id} (upstream Goal). */
export interface Goal {
  id: string;
  companyId?: string | null;
  title: string;
  description?: string | null;
  level: GoalLevel;
  status: GoalStatus;
  parentId?: string | null;
  ownerAgentId?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** Body for POST /goals (BFF whitelist: title, description?, parent_id?,
 * level?, status?). `level`/`status` let the create form preset the upstream
 * createGoalSchema defaults (level="task", status="planned") so a goal can be
 * filed at the right rung without a follow-up PATCH. Both single-word so the
 * BFF's snake_case/camelCase alias generator forwards them either way. */
export interface GoalCreateInput {
  title: string;
  description?: string;
  parent_id?: string;
  level?: GoalLevel;
  status?: GoalStatus;
}

/** Body for PATCH /goals/{id} (BFF whitelist: title?, description?, parent_id?, status?, level?). */
export interface GoalUpdateInput {
  title?: string;
  description?: string | null;
  parent_id?: string | null;
  status?: GoalStatus;
  level?: GoalLevel;
}

// ─── Projects ─────────────────────────────────────────────────────────────────
//
// Ported from upstream Paperclip's Project shape. The Teams BFF proxies the
// upstream project row verbatim (GET /teams/projects/{id} → admin get_project →
// GET /api/projects/{id}), so the wire shape is upstream camelCase. The slim
// `IssueProject` above (id/name/color) is the denormalized form embedded on
// issues; this is the full detail row the Project-detail page consumes.

/** Upstream PROJECT_STATUSES (updateProjectSchema). */
export type ProjectStatus =
  | "backlog"
  | "planned"
  | "in_progress"
  | "completed"
  | "cancelled";

export const PROJECT_STATUSES: readonly ProjectStatus[] = [
  "backlog",
  "planned",
  "in_progress",
  "completed",
  "cancelled",
];

/** A goal link embedded on the project row. Upstream's project service
 * enriches every project with `{ id, title }` refs for its linked goals
 * (`attachGoals` in server/src/services/projects.ts), so the detail payload
 * carries titles — the Goals row renders chips without a second goals fetch. */
export interface ProjectGoalRef {
  id: string;
  title: string;
}

/** Full project row from GET /teams/projects/{id} (upstream Project).
 *
 * NOTE: no `budgetMonthlyCents` — upstream Paperclip has no budget field on
 * the project row (project budgets live behind a separate `budget_policies`
 * API the slim BFF does not proxy), so it would always be absent. */
export interface Project {
  id: string;
  companyId?: string | null;
  name: string;
  description?: string | null;
  status?: string | null;
  color?: string | null;
  icon?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  /** Ids of linked company goals. Upstream enriches the row via the
   * `project_goals` join table; PATCH `{ goal_ids }` replaces the whole set. */
  goalIds?: string[];
  /** Linked goals as `{ id, title }` refs — the title source for the Goals
   * chips (avoids resolving ids against a separate goals list for display). */
  goals?: ProjectGoalRef[];
}

/**
 * Body for PATCH /teams/projects/{id}.
 *
 * The BFF's `PatchProjectBody` whitelists snake_case attribute names
 * (`name`, `description`) and re-emits camelCase to Paperclip via its alias
 * generator — so this snake_case shape IS the wire shape the hook sends
 * (mirrors `GoalUpdateInput`). No budget field (see `Project` above).
 */
export interface ProjectUpdateInput {
  name?: string;
  description?: string | null;
  /** Upstream PROJECT_STATUSES — sets the project lifecycle stage. */
  status?: ProjectStatus;
  /** Full desired set of linked company-goal ids (replace semantics — the
   * whole array is sent, matching upstream updateProjectSchema). The BFF
   * accepts `goal_ids` (snake_case) and aliases it to `goalIds` on the wire.
   * `undefined` leaves the links unchanged. */
  goal_ids?: string[];
}
