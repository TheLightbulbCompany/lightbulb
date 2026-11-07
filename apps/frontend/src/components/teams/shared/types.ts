// apps/frontend/src/components/teams/shared/types.ts

// Ported from upstream Paperclip
// (https://github.com/Paperclip-AI/paperclip/tree/main/packages/shared/src/types)
// (MIT, © 2025 Paperclip AI). Subset retained for IssueRow /
// IssueFiltersPopover. Full type lives in upstream packages/shared/src/types/.

import type { RunOutputSilence } from "@/components/teams/shared/lib/liveRuns";

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
// createIssueSchema work-mode enum: ``standard`` does the work, ``ask`` answers
// the question in the thread and writes no code, ``planning`` writes the plan to
// the task's ``plan`` document and waits for it to be accepted.
export type IssueWorkMode = "standard" | "ask" | "planning";

/** The active task watchdog on an issue (upstream ``IssueWatchdogSummary``):
 * a named agent that reviews the issue's subtree when it goes quiet. One per
 * issue. Not the run-silence watchdog on a run. */
export interface IssueWatchdog {
  id: string;
  issueId: string;
  watchdogAgentId: string;
  instructions?: string | null;
  status: "active" | "disabled";
  /** The "Watchdog review for X" child issue, once the watchdog has fired. */
  watchdogIssueId?: string | null;
  lastTriggeredAt?: string | null;
  triggerCount?: number;
}

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
  /** BFF-enriched display name for a human author (authorUserId is a
   * Paperclip principal id the frontend can't resolve itself). */
  authorName?: string | null;
  /** BFF-enriched Clerk id of a human author — compare against the viewer's
   * Clerk id to render "You". */
  authorClerkUserId?: string | null;
  /** Upstream authorship class ("user" | "agent" | "system"). */
  authorType?: string | null;
  /** Upstream tombstone stamp — a deleted comment stays in the list. */
  deletedAt?: string | null;
  /** Server-authored presentation contract (system notices). */
  presentation?: { kind?: string | null } | null;
  /** Upstream: the heartbeat run that wrote this comment (the timeline's run chip). */
  createdByRunId?: string | null;
}

/** A queued/running run on an issue (upstream `GET /issues/{id}/live-runs`
 *  row, and the `GET /issues/{id}/active-run` body). Optional-defensive. */
export interface IssueLiveRun {
  id: string;
  status: string;
  agentId?: string | null;
  agentName?: string | null;
  adapterType?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  /** Upstream decorateActiveRunStatus: human copy for the current phase. */
  currentStatusMessage?: string | null;
  invocationSource?: string | null;
  /** Output-silence watchdog (upstream buildRunOutputSilence). */
  outputSilence?: IssueRunOutputSilence | null;
}

/** Upstream ActiveRunForIssue["outputSilence"] — the fields the run ledger reads. */
export interface IssueRunOutputSilence {
  level: "ok" | "suspicious" | "critical" | "snoozed" | "not_applicable";
  silenceAgeMs?: number | null;
  evaluationIssueId?: string | null;
  evaluationIssueIdentifier?: string | null;
}

/** Thumbs up/down value for a feedback vote on an agent output. */
export type FeedbackVoteValue = "up" | "down";

/** A single feedback vote cast on an agent output (comment / doc revision).
 * Returned from GET /teams/issues/{id}/feedback-votes. The UI keys these by
 * targetId to render the active vote state per agent comment. */
export interface FeedbackVote {
  id: string;
  /** "issue_comment" | "issue_document_revision" — the kind of output voted on. */
  targetType: string;
  /** Id of the voted output (e.g. the comment id). */
  targetId: string;
  vote: FeedbackVoteValue;
  reason?: string | null;
}

/** Body for POST /teams/issues. Composer shape for NewIssueDialog.
 *
 * The Teams BFF `CreateIssueBody` schema (apps/backend/routers/teams/schemas.py)
 * whitelists ``status`` and ``workMode`` (camelCase aliases) and forwards both
 * to upstream Paperclip's createIssueSchema, so the composer can preset the
 * starting status + execution mode without a follow-up PATCH. Both are
 * optional, but omitting `status` is NOT "defaults to backlog": upstream's
 * resolveCreateIssueStatusDefault picks `todo` when the body carries an
 * assignee and `backlog` only when it does not — and `todo` + an assignee
 * DISPATCHES a run. Omit `status` on an assigned create and you have started
 * an agent, not filed a task (this is what caused #1426). `workMode` does
 * default to standard.
 */
export interface IssueCreateInput {
  title: string;
  description?: string;
  priority?: IssuePriority;
  projectId?: string;
  /** Goal the new task rolls up to. Omitted on the children endpoint means
   * upstream inherits the parent task's goal rather than leaving it unset. */
  goalId?: string;
  assigneeAgentId?: string;
  /** A human owner's Paperclip user id (`CompanyMember.paperclipUserId`). */
  assigneeUserId?: string;
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
  /** Assign a human company member by Paperclip user id
   * (``CompanyMember.paperclipUserId``), or ``null`` to clear it.
   * Mutually exclusive with ``assigneeAgentId`` — the picker sends the other
   * as ``null`` so only one assignee kind is ever set. ``undefined`` leaves
   * the user-assignee unchanged. */
  assigneeUserId?: string | null;
  /** Full desired set of label ids (replace semantics — the whole array is
   * sent, matching the multi-select rail control). The BFF camelCase-aliases
   * this to ``labelIds``. ``undefined`` leaves the labels unchanged. */
  labelIds?: string[];
  /** Link the task to a goal, or ``null`` to detach it. ``undefined`` leaves
   * the link unchanged. The BFF camelCase-aliases this to ``goalId``. */
  goalId?: string | null;
  /** An ISO timestamp hides the task (it drops out of the Tasks list);
   * ``null`` restores it. ``undefined`` leaves the hidden state unchanged.
   * The BFF whitelists ``hiddenAt`` and forwards it to upstream Paperclip. */
  hiddenAt?: string | null;
  /** Switch how the task runs. ``undefined`` leaves the mode unchanged. */
  workMode?: IssueWorkMode;
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

/** A slim issue summary used by the "Blocked by" / "Blocking" relation rows
 * and the ported IssueBlockedNotice.
 *
 * Ported from upstream Paperclip's ``IssueRelationIssueSummary``
 * (packages/shared/src/types/issue.ts @ v2026.1012.0-isol8.4) (MIT, © 2025
 * Paperclip AI). The fields past ``status`` are optional here because older
 * payloads and hand-built summaries omit them. */
export interface IssueRelationIssueSummary {
  id: string;
  identifier?: string | null;
  title: string;
  status: IssueStatus;
  priority?: IssuePriority | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  terminalBlockers?: IssueRelationIssueSummary[];
  activeRecoveryAction?: IssueRecoveryAction | null;
  scheduledRetry?: IssueScheduledRetry | null;
}

/** Upstream ``SuccessfulRunHandoffState`` (packages/shared/src/types/issue.ts
 * @ v2026.1012.0-isol8.4): a run finished but the task still needs a next step. */
export interface SuccessfulRunHandoffState {
  state: "required" | "resolved" | "escalated";
  required: boolean;
  hasLiveContinuation: boolean;
  liveRunId?: string | null;
  sourceRunId: string | null;
  correctiveRunId: string | null;
  assigneeAgentId: string | null;
  detectedProgressSummary: string | null;
  createdAt: Date | string | null;
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

/** LIVE monitor state, mirroring upstream's issueExecutionMonitorStateSchema.
 * Distinct from ``IssueMonitorPolicy``: the policy is the schedule someone
 * asked for, this is what actually happened to it.
 *
 * This matters because a monitor is ONE-SHOT upstream — when it fires, the
 * monitor is stripped from ``executionPolicy`` and only this state survives.
 * Rendering the policy alone therefore makes a task that is mid-wait (or that
 * just woke) look like nothing was ever scheduled. */
export interface IssueMonitorState {
  /** Upstream ISSUE_EXECUTION_MONITOR_STATE_STATUSES. */
  status?: "scheduled" | "triggered" | "cleared" | null;
  nextCheckAt?: string | null;
  lastTriggeredAt?: string | null;
  attemptCount?: number | null;
  notes?: string | null;
  serviceName?: string | null;
  timeoutAt?: string | null;
  maxAttempts?: number | null;
  clearedAt?: string | null;
  /** Upstream ISSUE_EXECUTION_MONITOR_CLEAR_REASONS — e.g. ``timeout_exceeded``,
   * ``max_attempts_exhausted``, ``triggered``, ``manual``. */
  clearReason?: string | null;
}

/** Upstream's executionState. Only the monitor slice is modeled — the stage
 * fields are read through ``executionPolicy`` today.
 *
 * NOTE: the issues LIST route hard-nulls ``executionState`` upstream, so this
 * is populated on the fetch-by-id detail response only. */
export interface IssueExecutionState {
  monitor?: IssueMonitorState | null;
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
  /** Fields the routine reviewer UI does not edit but must round-trip exactly. */
  commentRequired?: boolean;
  reviewPreset?: unknown;
  authorizationPolicy?: unknown;
  maxReviewRounds?: number | null;
  monitor?: IssueMonitorPolicy | null;
  /** Reviewer + approver stages. Each ``type`` appears at most once. Absent or
   * empty means no review/approval gate. */
  stages?: IssueExecutionStage[];
}

// The Blocked view's per-issue attention, from upstream Paperclip's
// packages/shared/src/types/issue.ts @ v2026.1012.0-isol8.4 (MIT, (c) 2025
// Paperclip AI). Verbatim but for IssueStatus/IssuePriority, which are ours.
export type IssueInboxAttentionKind = "blocked";

export type IssueBlockedInboxState =
  | "needs_attention"
  | "awaiting_decision"
  | "external_wait"
  | "recovery_open"
  | "missing_disposition";

export type IssueBlockedInboxSeverity = "critical" | "high" | "medium" | "low";

export type IssueBlockedInboxReason =
  | "blocked_by_unassigned_issue"
  | "blocked_by_assigned_backlog_issue"
  | "blocked_by_uninvokable_assignee"
  | "blocked_by_cancelled_issue"
  | "blocked_chain_stalled"
  | "invalid_review_participant"
  | "in_review_without_action_path"
  | "missing_successful_run_disposition"
  | "pending_board_decision"
  | "pending_user_decision"
  | "external_owner_action"
  | "open_recovery_issue";

export type IssueBlockedInboxOwnerType = "agent" | "user" | "board" | "external" | "unknown";

export interface IssueBlockedInboxIssueRef {
  id: string;
  identifier: string | null;
  title: string;
  status: IssueStatus;
  priority: IssuePriority;
  assigneeAgentId: string | null;
  assigneeUserId: string | null;
}

export interface IssueBlockedInboxOwner {
  type: IssueBlockedInboxOwnerType;
  agentId: string | null;
  userId: string | null;
  label: string | null;
}

export interface IssueBlockedInboxAction {
  label: string;
  detail: string | null;
}

export interface IssueBlockedInboxAttention {
  kind: IssueInboxAttentionKind;
  state: IssueBlockedInboxState;
  reason: IssueBlockedInboxReason;
  severity: IssueBlockedInboxSeverity;
  stoppedSinceAt: string | null;
  owner: IssueBlockedInboxOwner;
  action: IssueBlockedInboxAction;
  sourceIssue: IssueBlockedInboxIssueRef | null;
  leafIssue: IssueBlockedInboxIssueRef | null;
  recoveryIssue: IssueBlockedInboxIssueRef | null;
  approvalId: string | null;
  interactionId: string | null;
  sampleIssueIdentifier: string | null;
  redaction: {
    externalDetailsRedacted: boolean;
    secretFieldsOmitted: true;
  };
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
  /** Goal this task rolls up to (upstream `issues.goalId`). The goal-detail
   * page lists its tasks off this field and the rail's Goal row edits it. */
  goalId?: string | null;
  /** How the task came to exist. `"routine_execution"` marks a row a routine
   * spawned on one of its firings — the Tasks panel hides those by default. */
  originKind?: string | null;
  /** How the task runs. ``skill_test`` is upstream-internal: rendered when an
   * issue carries it, never offered in a picker. */
  workMode?: IssueWorkMode | "skill_test" | null;
  /** The active task watchdog, or ``null``/absent when none is armed. Upstream
   * attaches it to every issue read, and the BFF passes it through. */
  watchdog?: IssueWatchdog | null;
  /** Set when the task is hidden. Every list/board/search read filters these
   * out upstream, but fetch-by-id does not — so the detail page is the one
   * surface that can see (and restore) a hidden task. */
  hiddenAt?: string | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  /** Upstream issue.createdByAgentId — picks the task chat brief's side. */
  createdByAgentId?: string | null;
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
  /** Live execution state (detail route only — the list route nulls it). Carries
   * the monitor's real status/attempt count, which outlives the one-shot policy
   * entry once the monitor fires. */
  executionState?: IssueExecutionState | null;
  /** The parked scheduled retry (detail route). The recovery card reads it to
   * tell a retry that is genuinely running from one whose due time passed. */
  scheduledRetry?: IssueScheduledRetry | null;
  /** Why execution stopped (detail route); drives the ported ExecutionBlockerNotice. */
  executionBlocker?: ExecutionBlocker | null;
  /** Upstream's flattened monitor columns (detail route); the ported
   * IssueMonitorBanner falls back to them when executionState has no monitor. */
  monitorNextCheckAt?: string | null;
  monitorAttemptCount?: number | null;
  /** A successful run left the task open without a next step (detail route);
   * drives the ported IssueBlockedNotice's handoff alarm. */
  successfulRunHandoff?: SuccessfulRunHandoffState | null;
  /** The issue's active recovery action, when upstream attaches it to the read. */
  activeRecoveryAction?: IssueRecoveryAction | null;
  /** Why the task is stopped, on the Blocked view's list read only
   * (`attention=blocked&includeBlockedInboxAttention=true`). */
  blockedInboxAttention?: IssueBlockedInboxAttention | null;
}

// ---------------------------------------------------------------------------
// Issue recovery actions — mirrored from upstream Paperclip
// packages/shared/src/{constants.ts,types/issue.ts,types/heartbeat.ts}
// @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Read by the ported
// IssueRecoveryActionCard via GET /teams/issues/{id}/recovery-actions.
// ---------------------------------------------------------------------------

export type IssueRecoveryActionKind =
  | "missing_disposition"
  | "deliberate_wait_without_target"
  | "stranded_assigned_issue"
  | "workspace_validation"
  | "configuration_validation"
  | "active_run_watchdog"
  | "issue_graph_liveness";

export type IssueRecoveryActionStatus = "active" | "escalated" | "resolved" | "cancelled";

export type IssueRecoveryActionOwnerType = "agent" | "user" | "board" | "system";

export type IssueRecoveryActionOutcome =
  | "restored"
  | "handed_back"
  | "owner_completed"
  | "delegated"
  | "false_positive"
  | "blocked"
  | "escalated"
  | "cancelled";

export type GitWorktreeBranchAncestryVerdict = "ancestor" | "diverged" | "unknown";

export interface IssueRecoveryAction {
  /** Read-only activity of the exact native run named by the wake policy. */
  nativeRunActivity?: {
    runId: string;
    status: "queued" | "running";
    workspaceOperationId: string | null;
  } | null;
  id: string;
  companyId: string;
  sourceIssueId: string;
  recoveryIssueId: string | null;
  kind: IssueRecoveryActionKind;
  status: IssueRecoveryActionStatus;
  ownerType: IssueRecoveryActionOwnerType;
  ownerAgentId: string | null;
  ownerUserId: string | null;
  previousOwnerAgentId: string | null;
  returnOwnerAgentId: string | null;
  cause: string;
  fingerprint: string;
  evidence: Record<string, unknown>;
  nextAction: string;
  wakePolicy: Record<string, unknown> | null;
  monitorPolicy: Record<string, unknown> | null;
  attemptCount: number;
  maxAttempts: number | null;
  timeoutAt: Date | string | null;
  lastAttemptAt: Date | string | null;
  outcome: IssueRecoveryActionOutcome | null;
  resolutionNote: string | null;
  resolvedAt: Date | string | null;
  createdAt: Date | string;
  updatedAt: Date | string;
}

export type IssueScheduledRetryStatus = "scheduled_retry" | "queued" | "running" | "cancelled";

export interface IssueScheduledRetry {
  runId: string;
  status: IssueScheduledRetryStatus;
  agentId: string;
  agentName: string | null;
  retryOfRunId: string | null;
  scheduledRetryAt: Date | string | null;
  scheduledRetryAttempt: number;
  scheduledRetryReason: string | null;
  retryExhaustedReason?: string | null;
  error?: string | null;
  errorCode?: string | null;
}

/** Upstream IssueRetryNowOutcome / IssueRetryNowResponse — the body of
 * ``POST /teams/issues/{id}/scheduled-retry/retry-now`` (ported IssueScheduledRetryCard). */
export type IssueRetryNowOutcome =
  | "promoted"
  | "already_promoted"
  | "no_scheduled_retry"
  | "gate_suppressed";

export interface IssueRetryNowResponse {
  outcome: IssueRetryNowOutcome;
  message: string;
  scheduledRetry: IssueScheduledRetry | null;
}

/** Upstream ``GET /api/issues/:id/recovery-actions`` response. */
export interface IssueRecoveryActionsResponse {
  active: IssueRecoveryAction | null;
  actions: IssueRecoveryAction[];
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
  companyId?: string | null;
  type?: string | null;
  status: string;
  requestedByAgentId?: string | null;
  requestedByUserId?: string | null;
  payload?: Record<string, unknown> | null;
  decisionNote?: string | null;
  decidedAt?: string | null;
  createdAt?: string | null;
}

/** Upstream ApprovalComment (packages/shared/src/types/approval.ts @ v2026.916.1-isol8.2). */
export interface ApprovalComment {
  id: string;
  approvalId: string;
  authorAgentId: string | null;
  authorUserId: string | null;
  body: string;
  createdAt: string;
  updatedAt?: string;
}

export interface HeartbeatRun {
  id: string;
  agentId?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  startedAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
}

/** Upstream heartbeatRuns.livenessState (RunLivenessState). */
export type RunLivenessState =
  | "completed"
  | "advanced"
  | "plan_only"
  | "empty_response"
  | "blocked"
  | "failed"
  | "needs_followup";

/** A run for an issue (upstream RunForIssue subset — the fields the ported
 *  IssueRunLedger reads). `status` is a free string; every non-id field is
 *  optional-defensive because the BFF forwards upstream rows verbatim. */
export interface IssueRun {
  runId: string;
  status: string;
  agentId?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  invocationSource?: string | null;
  responsibleUserId?: string | null;
  errorCode?: string | null;
  resultJson?: Record<string, unknown> | null;
  retryOfRunId?: string | null;
  scheduledRetryAt?: string | null;
  scheduledRetryAttempt?: number | null;
  scheduledRetryReason?: string | null;
  retryExhaustedReason?: string | null;
  livenessState?: RunLivenessState | null;
  livenessReason?: string | null;
  continuationAttempt?: number | null;
  lastUsefulActionAt?: string | null;
  nextAction?: string | null;
}

/** Upstream WatchdogDecisionInput (ui/src/api/heartbeats.ts) — the body of
 *  POST /teams/heartbeat-runs/{runId}/watchdog-decisions, plus its run id. */
export interface WatchdogDecisionInput {
  runId: string;
  decision: "snooze" | "continue" | "dismissed_false_positive";
  evaluationIssueId?: string | null;
  reason?: string | null;
  snoozedUntil?: string | null;
}

/** Upstream ExecutionBlocker (packages/shared/src/types/execution-projection.ts):
 *  why a task's execution stopped and what has to happen next. */
export interface ExecutionBlocker {
  /** Null when live execution authority itself blocks continuation. */
  recoveryActionId: string | null;
  runId: string | null;
  agentId: string | null;
  cause: string;
  nextAction: string;
  runStatus?: string | null;
  runError?: string | null;
  /** Candidate only: admission must still verify termination and all gates. */
  canContinue?: boolean;
  /** Explicit Retry candidate; the server rechecks stop proof and execution gates. */
  canRetry?: boolean;
  savedMessageCount?: number;
}

/** Full heartbeat-run row for the run-detail panel (upstream
 *  `GET /api/heartbeat-runs/{id}`, verbatim pass-through via the BFF).
 *  Field set verified against the fork's shared HeartbeatRun type; every
 *  field is optional-defensive because upstream row shapes drift. */
export interface RunDetail {
  id: string;
  agentId?: string | null;
  /** Free string; known vocab: queued | scheduled_retry | running |
   *  succeeded | failed | cancelled | timed_out. Render defensively. */
  status?: string | null;
  invocationSource?: string | null;
  triggerDetail?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  error?: string | null;
  errorCode?: string | null;
  exitCode?: number | null;
  signal?: string | null;
  usageJson?: Record<string, unknown> | null;
  /** Adapter result; `summary`/`result` feed the agent Dashboard/Runs excerpts. */
  resultJson?: Record<string, unknown> | null;
  sessionIdBefore?: string | null;
  sessionIdAfter?: string | null;
  logBytes?: number | null;
  stdoutExcerpt?: string | null;
  stderrExcerpt?: string | null;
  retryOfRunId?: string | null;
  retryExhaustedReason?: string | null;
  livenessState?: string | null;
  livenessReason?: string | null;
  nextAction?: string | null;
  lastUsefulActionAt?: string | null;
  contextSnapshot?: Record<string, unknown> | null;
  outputSilence?: RunOutputSilence | null;
}

/** One `heartbeat_run_events` row (upstream events timeline). */
export interface RunEvent {
  id?: number;
  seq: number;
  eventType?: string | null;
  stream?: string | null;
  level?: string | null;
  message?: string | null;
  payload?: Record<string, unknown> | null;
  createdAt?: string | null;
}

/**
 * A value a run-now caller can supply for a routine variable. Mirrors
 * upstream `routineVariableValueSchema`
 * (paperclip/packages/shared/src/validators/routine.ts) — a string, finite
 * number, or boolean.
 */
export type RoutineVariableValue = string | number | boolean;

/** The variable types a routine can declare (upstream ROUTINE_VARIABLE_TYPES).
 * ``date`` was added upstream in #8655 (b3c0fadd6) — a structured YYYY-MM-DD
 * calendar value collected via a date picker in the run + edit dialogs. */
export type RoutineVariableType =
  | "text"
  | "textarea"
  | "number"
  | "boolean"
  | "select"
  | "date";

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
  // Webhook triggers carry a public id + signing mode; the detail panel uses
  // these to decide whether a "Rotate secret" affordance applies (only a
  // signed webhook trigger has a secret to rotate).
  signingMode?: string | null;
  publicId?: string | null;
  // Webhook replay-window (seconds) — only meaningful for a signed webhook
  // (signingMode "bearer" | "hmac_sha256"); null/omitted otherwise.
  replayWindowSec?: number | null;
  // The public URL that fires this trigger — what the owner pastes into the
  // provider's dashboard (Stripe/PostHog/Sentry/Linear/Attio) to ARM it.
  // Server-owned and server-projected (routers/teams/work.py::get_routine via
  // core/services/trigger_fire_url.py): the shape depends on PUBLIC_API_BASE
  // and backend owner-id resolution, and a subtly wrong URL pasted into a
  // provider fails silently at delivery time — so never reconstruct it here.
  // Webhook triggers only; absent on schedules.
  fireUrl?: string | null;
}

/** The one-time secret material a webhook trigger create/rotate returns
 * (upstream ``RoutineTriggerSecretMaterial``) — the URL + secret are shown
 * exactly once and never retrievable again after the dialog closes. */
export interface RoutineTriggerSecretMaterial {
  webhookUrl: string;
  webhookSecret: string;
}

/**
 * One entry in a routine's revision history (upstream RoutineRevision subset).
 * Surfaced by the detail panel's Revisions view; a Restore reverts the routine
 * to the snapshot captured at that revision.
 */
export interface RoutineRevision {
  id: string;
  revisionNumber?: number | null;
  changeSummary?: string | null;
  createdAt?: string | null;
  // Full upstream RoutineRevision (packages/shared/src/types/routine.ts @
  // v2026.916.1-isol8.2) — read by the ported RoutineHistoryTab.
  snapshot?: RoutineRevisionSnapshot | null;
  restoredFromRevisionId?: string | null;
  createdByAgentId?: string | null;
  createdByUserId?: string | null;
}

/** Upstream RoutineRevisionSnapshotRoutineV1 subset the history tab renders. */
export interface RoutineRevisionSnapshotRoutine {
  title: string;
  description?: string | null;
  assigneeAgentId?: string | null;
  projectId?: string | null;
  priority?: string | null;
  status?: string | null;
  concurrencyPolicy?: string | null;
  catchUpPolicy?: string | null;
  variables?: RoutineVariable[];
  // Upstream RoutineEnvConfig: key -> plain string or a secret_ref binding.
  env?: Record<string, unknown> | null;
}

/** Upstream RoutineRevisionSnapshotTriggerV1. */
export interface RoutineRevisionSnapshotTrigger {
  id: string;
  kind: string;
  label?: string | null;
  enabled?: boolean;
  cronExpression?: string | null;
  timezone?: string | null;
  signingMode?: string | null;
  replayWindowSec?: number | null;
}

export interface RoutineRevisionSnapshot {
  routine: RoutineRevisionSnapshotRoutine;
  triggers?: RoutineRevisionSnapshotTrigger[];
}

/** Upstream RestoreRoutineRevisionResponse (ui/src/api/routines.ts). */
export interface RestoreRoutineRevisionResponse {
  revision?: RoutineRevision | null;
  restoredFromRevisionNumber?: number | null;
  secretMaterials?: Array<RoutineTriggerSecretMaterial & { triggerId: string }>;
}

/** One row of GET /teams/issues/{id}/activity (upstream ActivityEvent; the BFF
 *  redacts adapter/secret/cost-shaped keys out of ``details``). */
export interface IssueActivityEvent {
  id: string;
  actorType: "agent" | "user" | "system" | string;
  actorId: string;
  action: string;
  entityType?: string;
  entityId?: string;
  agentId?: string | null;
  runId?: string | null;
  responsibleUserId?: string | null;
  details?: Record<string, unknown> | null;
  createdAt: string;
}

/** One row of GET /teams/routines/{id}/activity (upstream ActivityEvent subset). */
export interface RoutineActivityEvent {
  id: string;
  action: string;
  details?: Record<string, unknown> | null;
  createdAt: string;
}

/** Single routine from GET /routines/{id} (upstream RoutineDetail subset). */
export interface RoutineDetail {
  id: string;
  title: string;
  status?: string | null;
  // Upstream ISSUE_PRIORITIES; editable from the detail page's edit dialog.
  priority?: string | null;
  description?: string | null;
  assigneeAgentId?: string | null;
  assignee?: RoutineAgentSummary | null;
  // Default project for runs (prefills the run dialog) and the revision the
  // routine currently sits on (the history tab's "Current" row).
  projectId?: string | null;
  latestRevisionId?: string | null;
  triggers?: RoutineTriggerSummary[];
  // Variable definitions the routine declares. A run-now caller must collect a
  // value per variable; empty/absent means the routine takes no input.
  variables?: RoutineVariable[];
  // Policy copied onto every issue this routine generates. The UI exposes a
  // single agent reviewer while preserving every other policy field on edits.
  executionPolicy?: IssueExecutionPolicy | null;
  // Upstream Routine carries createdAt/updatedAt verbatim through the BFF;
  // surfaced in the detail page's Properties aside.
  createdAt?: string | null;
  updatedAt?: string | null;
  // Circuit-breaker auto-pause state (upstream shared/routine.ts). Set together
  // when the breaker trips a routine to `status: "paused"` after repeated
  // failed runs — distinct from a human pausing it by hand, which leaves
  // `autoPausedAt` null. See `isAutoPaused` in shared/lib/routineHealth.ts.
  autoPausedAt?: string | null;
  consecutiveFailureCount?: number | null;
  autoPauseReason?: string | null;
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
  latestRevisionNumber?: number | null;
  latestRevisionId?: string | null;
  /** Set while the document is locked against a person's edits. */
  lockedAt?: string | null;
  lockedByAgentId?: string | null;
}

/**
 * A binary file attached to an issue (upstream IssueAttachment + asset join,
 * `paperclip/server/src/routes/issues.ts` `withContentPath`). `contentPath` is
 * the upstream-relative content URL; the BFF download goes through
 * `/teams/attachments/{id}/content`, so the UI builds its own path from `id`
 * rather than trusting `contentPath` for routing.
 */
export interface IssueAttachment {
  id: string;
  /** Original upload filename (may be null for unnamed uploads). */
  originalFilename?: string | null;
  contentType: string;
  byteSize: number;
  /** Upstream-relative content path (`/api/attachments/{id}/content`). */
  contentPath?: string | null;
  createdAt?: string | null;
}

/** A work-product's type (upstream IssueWorkProductType). */
export type IssueWorkProductType =
  | "preview_url"
  | "runtime_service"
  | "pull_request"
  | "branch"
  | "commit"
  | "artifact"
  | "document";

/** A work-product's lifecycle status (upstream IssueWorkProductStatus). */
export type IssueWorkProductStatus =
  | "active"
  | "ready_for_review"
  | "approved"
  | "changes_requested"
  | "merged"
  | "closed"
  | "failed"
  | "archived"
  | "draft";

/** A work-product's board-review state (upstream IssueWorkProductReviewState). */
export type IssueWorkProductReviewState =
  | "none"
  | "needs_board_review"
  | "approved"
  | "changes_requested";

/**
 * A typed deliverable an agent produced for an issue (upstream IssueWorkProduct
 * subset; `paperclip/packages/shared/src/types/work-product.ts`). Distinct from
 * a raw attachment: a work-product is the reviewable *thing* (a preview URL, a
 * PR, a generated artifact), carrying a `reviewState` the board can approve or
 * request changes on. Surfaced on the issue-detail "Output" section.
 */
export interface IssueWorkProduct {
  id: string;
  issueId: string;
  type: IssueWorkProductType | string;
  provider: string;
  title: string;
  url?: string | null;
  status: IssueWorkProductStatus | string;
  reviewState: IssueWorkProductReviewState | string;
  isPrimary?: boolean;
  summary?: string | null;
  externalId?: string | null;
  healthStatus?: "unknown" | "healthy" | "unhealthy" | string;
  /** Provider metadata; attachment-backed artifacts carry the attachment paths here. */
  metadata?: Record<string, unknown> | null;
  createdByRunId?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** Media classification for a company artifact (upstream companyArtifactMediaKind). */
export type CompanyArtifactMediaKind =
  | "image"
  | "video"
  | "text"
  | "document"
  | "file"
  | "empty";

/** Underlying source of a company artifact (upstream companyArtifactSource). */
export type CompanyArtifactSource = "document" | "attachment" | "work_product";

/**
 * A single row in the company Artifacts gallery (upstream companyArtifactSchema).
 * The `*Path` fields are upstream-relative (`/api/attachments/{id}/content`);
 * the gallery links back into the owning issue's detail panel rather than
 * rendering the raw path.
 */
export interface CompanyArtifact {
  id: string;
  source: CompanyArtifactSource | string;
  mediaKind: CompanyArtifactMediaKind | string;
  title: string;
  previewText?: string | null;
  contentType?: string | null;
  contentPath?: string | null;
  openPath?: string | null;
  downloadPath?: string | null;
  issue: { id: string; identifier: string; title: string };
  project?: { id: string; name: string } | null;
  createdByAgent?: { id: string; name: string } | null;
  updatedAt: string;
  href: string;
}

/**
 * A task-grouped bundle of company artifacts (upstream companyArtifactGroupSchema).
 * The gallery ports the GROUPED shape (`groupBy=task`) so deliverables cluster
 * under the issue that produced them rather than showing as a flat firehose.
 */
export interface CompanyArtifactGroup {
  id: string;
  groupBy: "task" | "parent_task" | string;
  issue: { id: string; identifier: string; title: string };
  title: string;
  count: number;
  mediaKinds: (CompanyArtifactMediaKind | string)[];
  previewArtifacts: CompanyArtifact[];
  updatedAt: string;
  href: string;
}

/** Response shape of `GET /teams/artifacts` (upstream companyArtifactsResponseSchema). */
export interface CompanyArtifactsResponse {
  artifacts: CompanyArtifact[];
  groups?: CompanyArtifactGroup[];
  selectedGroup?: CompanyArtifactGroup | null;
  nextCursor: string | null;
}

export interface CompanyMember {
  userId: string;
  name?: string | null;
  email?: string | null;
  imageUrl?: string | null;
  /** The id Paperclip's `assigneeUserId` holds; null until the member has a Paperclip identity. */
  paperclipUserId?: string | null;
}

export interface CompanyAgent {
  id: string;
  name: string;
  /** Upstream Paperclip's stable agent slug (e.g. "cto"), derived from the
   *  name — server/src/services/agents.ts. The bundled paperclip skill writes
   *  `/<prefix>/agents/<urlKey>` links; AgentInspectorPanel resolves a
   *  non-uuid agent id against this field before using it for anything else. */
  urlKey?: string | null;
  /** The @handle rooms seat this agent under (GET /teams/agents, super-agent on; absent on older servers). */
  mentionHandle?: string | null;
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
  /** Backend-authored install source. Seller artifacts cannot set this. */
  origin?: "Seed" | "Starter" | "Marketplace" | "Custom";
  marketplace?: {
    listingId: string;
    slug: string;
    version: number;
    seller: string;
  };
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
}

export interface DashboardBudgets {
  pendingApprovals: number;
}

export interface DashboardSummary {
  agents: DashboardAgentBuckets;
  tasks: DashboardTaskBuckets;
  runActivity: DashboardRunActivityDay[];
  costs?: DashboardCosts;
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
  /** Upstream Paperclip's stable project slug, derived from the name
   *  (server/src/services/projects.ts `deriveProjectUrlKey`). The bundled
   *  paperclip skill writes `/<prefix>/projects/<urlKey>` links; useProjectDetail
   *  resolves a non-uuid project id against this field before fetching — the
   *  upstream `GET /projects/:id` route has no url-key fallback of its own. */
  urlKey?: string | null;
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

// ─── Issue thread interactions ──────────────────────────────────────────────
//
// Ported from upstream Paperclip's IssueThreadInteraction shapes
// (paperclip/packages/shared/src/types/issue.ts + constants.ts)
// (MIT, © 2025 Paperclip AI). An interaction is an inline, agent-initiated
// request that lives in an issue's conversation thread: the agent suggests
// tasks, asks the user questions, or requests a confirmation, and the user
// accepts / declines / answers it inline. Returned from
// GET /teams/issues/{id}/interactions and resolved via the
// {accept,reject,respond,cancel} POST endpoints.
//
// `createdAt`/`updatedAt`/`resolvedAt` are typed `Date | string` to match
// upstream exactly so the ported helpers/card typecheck verbatim — over the
// wire (BFF proxies upstream JSON) they always arrive as ISO strings.

/** Upstream ISSUE_THREAD_INTERACTION_KINDS. */
export const ISSUE_THREAD_INTERACTION_KINDS = [
  "suggest_tasks",
  "ask_user_questions",
  "request_confirmation",
  "request_checkbox_confirmation",
  "request_item_verdicts",
] as const;
export type IssueThreadInteractionKind =
  (typeof ISSUE_THREAD_INTERACTION_KINDS)[number];

/** Upstream ISSUE_THREAD_INTERACTION_STATUSES. */
export const ISSUE_THREAD_INTERACTION_STATUSES = [
  "pending",
  "accepted",
  "rejected",
  "answered",
  "cancelled",
  "expired",
  "failed",
] as const;
export type IssueThreadInteractionStatus =
  (typeof ISSUE_THREAD_INTERACTION_STATUSES)[number];

/** Upstream ISSUE_THREAD_INTERACTION_CONTINUATION_POLICIES — whether resolving
 * the interaction wakes the assignee agent to continue the run. */
export const ISSUE_THREAD_INTERACTION_CONTINUATION_POLICIES = [
  "none",
  "wake_assignee",
  "wake_assignee_on_accept",
] as const;
export type IssueThreadInteractionContinuationPolicy =
  (typeof ISSUE_THREAD_INTERACTION_CONTINUATION_POLICIES)[number];

export interface IssueThreadInteractionActorFields {
  createdByAgentId?: string | null;
  createdByUserId?: string | null;
  resolvedByAgentId?: string | null;
  resolvedByUserId?: string | null;
  /** BFF-enriched — the user-id fields are Paperclip principal ids the
   * frontend can't resolve itself; the ClerkUserId twins disambiguate "You". */
  createdByEmail?: string | null;
  createdByClerkUserId?: string | null;
  resolvedByEmail?: string | null;
  resolvedByClerkUserId?: string | null;
}

// --- suggest_tasks ---

export interface SuggestedTaskDraft {
  clientKey: string;
  parentClientKey?: string | null;
  parentId?: string | null;
  title: string;
  description?: string | null;
  priority?: IssuePriority | null;
  workMode?: IssueWorkMode | null;
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  /** BFF-enriched — see IssueThreadInteractionActorFields. */
  assigneeEmail?: string | null;
  assigneeClerkUserId?: string | null;
  projectId?: string | null;
  goalId?: string | null;
  billingCode?: string | null;
  labels?: string[];
  hiddenInPreview?: boolean;
}

export interface SuggestTasksPayload {
  version: 1;
  defaultParentId?: string | null;
  tasks: SuggestedTaskDraft[];
}

export interface SuggestTasksResultCreatedTask {
  clientKey: string;
  issueId: string;
  identifier?: string | null;
  title?: string | null;
  parentIssueId?: string | null;
  parentIdentifier?: string | null;
}

export interface SuggestTasksResult {
  version: 1;
  createdTasks?: SuggestTasksResultCreatedTask[];
  skippedClientKeys?: string[];
  rejectionReason?: string | null;
}

// --- ask_user_questions ---

export interface AskUserQuestionsQuestionOption {
  id: string;
  label: string;
  description?: string | null;
}

export interface AskUserQuestionsQuestion {
  id: string;
  prompt: string;
  helpText?: string | null;
  selectionMode: "single" | "multi";
  required?: boolean;
  options: AskUserQuestionsQuestionOption[];
}

export interface AskUserQuestionsPayload {
  version: 1;
  title?: string | null;
  submitLabel?: string | null;
  questions: AskUserQuestionsQuestion[];
}

export interface AskUserQuestionsAnswer {
  questionId: string;
  optionIds: string[];
  otherText?: string | null;
}

export interface AskUserQuestionsResult {
  version: 1;
  answers: AskUserQuestionsAnswer[];
  cancelled?: true;
  cancellationReason?: string | null;
  summaryMarkdown?: string | null;
}

// --- request_confirmation / request_checkbox_confirmation ---

export interface RequestConfirmationIssueDocumentTarget {
  type: "issue_document";
  issueId?: string | null;
  documentId?: string | null;
  key: string;
  revisionId: string;
  revisionNumber?: number | null;
  label?: string | null;
  href?: string | null;
}

export interface RequestConfirmationCustomTarget {
  type: "custom";
  key: string;
  revisionId?: string | null;
  revisionNumber?: number | null;
  label?: string | null;
  href?: string | null;
}

export type RequestConfirmationTarget =
  | RequestConfirmationIssueDocumentTarget
  | RequestConfirmationCustomTarget;

export interface RequestConfirmationPayload {
  version: 1;
  prompt: string;
  acceptLabel?: string | null;
  rejectLabel?: string | null;
  rejectRequiresReason?: boolean;
  rejectReasonLabel?: string | null;
  allowDeclineReason?: boolean;
  declineReasonPlaceholder?: string | null;
  detailsMarkdown?: string | null;
  supersedeOnUserComment?: boolean;
  target?: RequestConfirmationTarget | null;
}

export interface RequestCheckboxConfirmationOption {
  id: string;
  label: string;
  description?: string | null;
}

export interface RequestCheckboxConfirmationPayload {
  version: 1;
  prompt: string;
  detailsMarkdown?: string | null;
  options: RequestCheckboxConfirmationOption[];
  defaultSelectedOptionIds?: string[];
  minSelected?: number;
  maxSelected?: number | null;
  acceptLabel?: string | null;
  rejectLabel?: string | null;
  rejectRequiresReason?: boolean;
  rejectReasonLabel?: string | null;
  allowDeclineReason?: boolean;
  declineReasonPlaceholder?: string | null;
  supersedeOnUserComment?: boolean;
  target?: RequestConfirmationTarget | null;
}

export interface RequestConfirmationResult {
  version: 1;
  outcome: "accepted" | "rejected" | "superseded_by_comment" | "stale_target";
  reason?: string | null;
  commentId?: string | null;
  staleTarget?: RequestConfirmationTarget | null;
}

export interface RequestCheckboxConfirmationResult
  extends RequestConfirmationResult {
  selectedOptionIds?: string[];
}

// --- request_item_verdicts (upstream packages/shared validators/issue.ts @ v2026.916.1-isol8.2) ---

export type RequestItemVerdictValue = "approve" | "reject" | "defer";

export interface RequestItemVerdictsItem {
  id: string;
  label: string;
  description?: string | null;
  previewMarkdown?: string | null;
  href?: string | null;
  attachmentId?: string | null;
}

export interface RequestItemVerdictsPayload {
  version: 1;
  prompt: string;
  detailsMarkdown?: string | null;
  items: RequestItemVerdictsItem[];
  /** Upstream defaults to ["approve", "reject"]. */
  verdicts?: RequestItemVerdictValue[];
  /** Upstream defaults to ["reject"]. */
  requireReasonOn?: RequestItemVerdictValue[];
  reasonLabel?: string | null;
  allowBulkApprove?: boolean;
  supersedeOnUserComment?: boolean;
  target?: RequestConfirmationTarget | null;
}

export interface RequestItemVerdictsResultItem {
  id: string;
  verdict: RequestItemVerdictValue;
  reason?: string | null;
  resolvedByUserId?: string | null;
  resolvedByAgentId?: string | null;
  resolvedAt: Date | string;
}

export interface RequestItemVerdictsResult {
  version: 1;
  outcome:
    | "resolved"
    | "superseded_by_comment"
    | "stale_target"
    | "cancelled"
    | "skipped"
    | "withdrawn"
    | "issue_closed"
    | "addressee_deleted";
  reason?: string | null;
  complete: boolean;
  items: RequestItemVerdictsResultItem[];
}

/** Body of POST /teams/issues/{id}/interactions/{iid}/verdicts. */
export interface SubmitIssueThreadInteractionVerdicts {
  verdicts: Array<{ id: string; verdict: RequestItemVerdictValue; reason?: string }>;
}

// --- the discriminated union ---

// Resolver audience (who may resolve an interaction). Shapes from the fork's
// packages/shared/src/constants.ts @ v2026.1012.0-isol8.4.
export type IssueThreadInteractionCanonicalResolverPolicy = "anyone" | "not_creator" | "human_only";
export type IssueThreadInteractionResolverPolicyProvenance =
  | "explicit"
  | "inherited"
  | "legacy_inherited_restriction";
export type IssueThreadInteractionEffectiveResolverPolicySource =
  | "requested"
  | "company_cap"
  | "governed_action";

export interface IssueThreadInteractionBase
  extends IssueThreadInteractionActorFields {
  id: string;
  companyId: string;
  issueId: string;
  kind: IssueThreadInteractionKind;
  idempotencyKey?: string | null;
  sourceCommentId?: string | null;
  sourceRunId?: string | null;
  addresseeAgentId?: string | null;
  addresseeUserId?: string | null;
  /** Server-evaluated audience. Optional here: rows from an older Paperclip
   *  omit it, and the audience line then renders nothing. */
  requestedResolverPolicy?: IssueThreadInteractionCanonicalResolverPolicy;
  effectiveResolverPolicy?: IssueThreadInteractionCanonicalResolverPolicy;
  resolverPolicyProvenance?: IssueThreadInteractionResolverPolicyProvenance;
  effectiveResolverPolicySource?: IssueThreadInteractionEffectiveResolverPolicySource;
  title?: string | null;
  summary?: string | null;
  status: IssueThreadInteractionStatus;
  continuationPolicy: IssueThreadInteractionContinuationPolicy;
  createdAt: Date | string;
  updatedAt: Date | string;
  resolvedAt?: Date | string | null;
}

export interface SuggestTasksInteraction extends IssueThreadInteractionBase {
  kind: "suggest_tasks";
  payload: SuggestTasksPayload;
  result?: SuggestTasksResult | null;
}

export interface AskUserQuestionsInteraction
  extends IssueThreadInteractionBase {
  kind: "ask_user_questions";
  payload: AskUserQuestionsPayload;
  result?: AskUserQuestionsResult | null;
}

export interface RequestConfirmationInteraction
  extends IssueThreadInteractionBase {
  kind: "request_confirmation";
  payload: RequestConfirmationPayload;
  result?: RequestConfirmationResult | null;
}

export interface RequestCheckboxConfirmationInteraction
  extends IssueThreadInteractionBase {
  kind: "request_checkbox_confirmation";
  payload: RequestCheckboxConfirmationPayload;
  result?: RequestCheckboxConfirmationResult | null;
}

export interface RequestItemVerdictsInteraction
  extends IssueThreadInteractionBase {
  kind: "request_item_verdicts";
  payload: RequestItemVerdictsPayload;
  result?: RequestItemVerdictsResult | null;
}

export type IssueThreadInteraction =
  | SuggestTasksInteraction
  | AskUserQuestionsInteraction
  | RequestConfirmationInteraction
  | RequestCheckboxConfirmationInteraction
  | RequestItemVerdictsInteraction;

export type IssueThreadInteractionPayload =
  | SuggestTasksPayload
  | AskUserQuestionsPayload
  | RequestConfirmationPayload
  | RequestCheckboxConfirmationPayload
  | RequestItemVerdictsPayload;

export type IssueThreadInteractionResult =
  | SuggestTasksResult
  | AskUserQuestionsResult
  | RequestConfirmationResult
  | RequestCheckboxConfirmationResult
  | RequestItemVerdictsResult;

// ---------------------------------------------------------------------------
// Company search (cross-entity ranked search)
//
// Ported from upstream Paperclip (packages/shared/src/types/search.ts,
// MIT © 2025 Paperclip AI). Mirrors the `CompanySearchResponse` object the
// Teams BFF `/teams/search` route returns verbatim. Kept as a subset — the
// SearchPanel renders results + counts + zero-results; the exhaustive
// filterOptionCounts shape is typed loosely since v1 does not surface facets.
// ---------------------------------------------------------------------------

export const COMPANY_SEARCH_SCOPES = [
  "all",
  "issues",
  "comments",
  "documents",
  "artifacts",
  "agents",
  "projects",
] as const;
export type CompanySearchScope = (typeof COMPANY_SEARCH_SCOPES)[number];

export const COMPANY_SEARCH_SORTS = ["relevance", "updated", "created", "priority"] as const;
export type CompanySearchSort = (typeof COMPANY_SEARCH_SORTS)[number];

export const COMPANY_SEARCH_UPDATED_WITHIN_OPTIONS = ["24h", "7d", "30d", "90d"] as const;
export type CompanySearchUpdatedWithinOption = (typeof COMPANY_SEARCH_UPDATED_WITHIN_OPTIONS)[number];

export type CompanySearchResultType = "issue" | "artifact" | "agent" | "project";
export type CompanySearchCountType = CompanySearchResultType | "comment" | "document";

export interface CompanySearchHighlight {
  start: number;
  end: number;
}

export interface CompanySearchSnippet {
  field: string;
  label: string;
  text: string;
  highlights: CompanySearchHighlight[];
}

export interface CompanySearchIssueSummary {
  id: string;
  identifier: string | null;
  title: string;
  status: IssueStatus;
  priority: IssuePriority;
  assigneeAgentId: string | null;
  assigneeUserId: string | null;
  projectId: string | null;
  updatedAt: string;
}

export interface CompanySearchArtifactSummary {
  id: string;
  source: "document" | "attachment" | "work_product";
  mediaKind: "image" | "video" | "text" | "document" | "file" | "empty";
  issueId: string;
  issueIdentifier: string;
  issueTitle: string;
  projectId: string | null;
  projectName: string | null;
  updatedAt: string;
}

export interface CompanySearchResult {
  id: string;
  type: CompanySearchResultType;
  score: number;
  title: string;
  href: string;
  matchedFields: string[];
  sourceLabel: string | null;
  snippet: string | null;
  snippets: CompanySearchSnippet[];
  issue?: CompanySearchIssueSummary;
  artifact?: CompanySearchArtifactSummary;
  updatedAt: string | null;
  previewImageUrl: string | null;
}

export interface CompanySearchZeroResults {
  unfilteredTotal: number;
  loosenSuggestions: {
    filter: string;
    values: string[];
    resultCount: number;
    additionalCount: number;
  }[];
}

export interface CompanySearchResponse {
  query: string;
  normalizedQuery: string;
  scope: CompanySearchScope;
  sort: CompanySearchSort;
  limit: number;
  offset: number;
  results: CompanySearchResult[];
  countsByType: Partial<Record<CompanySearchCountType, number>>;
  filterOptionCounts?: Record<string, unknown>;
  zeroResults: CompanySearchZeroResults | null;
  hasMore: boolean;
}

// --- Attention feed (the unified "needs you" list) -------------------------
//
// Mirrors upstream Paperclip's `AttentionFeed` (packages/shared/src/types/
// attention.ts), narrowed to the fields this UI reads, PLUS the two rows the
// Isol8 BFF hydrates onto each item (`approval`, `interaction`) so a decision
// can be resolved from the list without a follow-up fetch. See
// apps/backend/routers/teams/attention.py.

export type AttentionSourceKind =
  | "approval"
  | "decision"
  | "issue_thread_interaction"
  | "join_request"
  | "recovery_action"
  | "productivity_review"
  | "blocker_attention"
  | "review"
  | "failed_run"
  | "budget_alert"
  | "agent_error_alert";

export type AttentionSeverity = "critical" | "high" | "medium" | "low";

export interface AttentionSubject {
  kind: string;
  id: string;
  title: string | null;
  identifier: string | null;
  status: string | null;
  href: string | null;
  metadata?: Record<string, unknown>;
}

export interface AttentionItem {
  id: string;
  sourceKind: AttentionSourceKind;
  subject: AttentionSubject;
  /** Upstream's one-line reason this is on the list. Rendered verbatim. */
  whyNow: string;
  severity: AttentionSeverity;
  activityAt: string;
  createdAt: string;
  relatedIssue: AttentionSubject | null;
  originAgentName: string | null;
  /** BFF-hydrated: the full approval row (`sourceKind: "approval"`). */
  approval?: Approval | null;
  /** BFF-hydrated: the full interaction row (`issue_thread_interaction`). */
  interaction?: IssueThreadInteraction | null;
  // Desk fields (upstream AttentionItem, fork v2026.1012.0-isol8.4). Optional so
  // a feed built before them, and older fixtures, still type-check.
  /** Key for `/inbox-dismissals` (dismiss / snooze / restore). Contains ':'. */
  dismissalKey?: string;
  /** Present on `includeDismissed=true` reads when the item is dismissed or snoozed. */
  dismissal?: AttentionItemDismissal | null;
  /** `today` | `this_week` | `whenever` | `YYYY-MM-DD`. */
  decideBy?: string | null;
  decideByAttribution?: AttentionTriageAttribution | null;
  snoozedUntil?: string | null;
  /** Server escalation order; lower is more urgent. */
  rank?: number;
  /** Idle past retention: the item belongs on the Aging shelf, not the desk. */
  shelf?: boolean;
  decisionVerbs?: AttentionDecisionVerb[];
  inlineResolvable?: boolean;
  detail?: AttentionItemDetail | null;
  /** Set for `issue_thread_interaction` rows only. */
  resolverAudience?: AttentionResolverAudience | null;
}

/** Who may resolve an `issue_thread_interaction` attention row (fork
 *  packages/shared/src/types/attention.ts). */
export interface AttentionResolverAudience {
  requestedResolverPolicy: IssueThreadInteractionCanonicalResolverPolicy;
  effectiveResolverPolicy: IssueThreadInteractionCanonicalResolverPolicy;
  effectiveResolverPolicySource: IssueThreadInteractionEffectiveResolverPolicySource;
  resolverPolicyProvenance: IssueThreadInteractionResolverPolicyProvenance;
  addresseeAgentId: string | null;
  addresseeUserId?: string | null;
  addresseeName: string | null;
  createdByAgentId: string | null;
  createdByAgentName: string | null;
}

export interface AttentionDecisionVerb {
  id: string;
  label: string;
  description: string | null;
}

export interface AttentionItemDismissal {
  kind: "dismiss" | "snooze";
  dismissedAt: string;
  snoozedUntil: string | null;
  isActive: boolean;
}

export interface AttentionTriageAttribution {
  type: "agent" | "user";
  agentId: string | null;
  agentName: string | null;
  userId: string | null;
  runId: string | null;
  responsibleUserId: string | null;
  updatedAt: string;
}

type AttentionDetailImages = { images?: { assetId: string; alt?: string | null }[] };

export type AttentionItemDetail = AttentionDetailImages & (
  | { kind: "approval"; approvalType: string; summaryExcerpt: string | null }
  | { kind: "plan_approval"; issueTitle: string | null; planTitle: string | null; summaryExcerpt: string | null }
  | { kind: "confirmation"; promptExcerpt: string | null }
  | { kind: "questions"; questionCount: number; firstQuestionText: string | null }
  | { kind: "suggested_tasks"; taskCount: number; firstTaskTitle: string | null }
  | { kind: "checkbox_confirmation"; optionCount: number; promptExcerpt: string | null }
  | { kind: "item_verdicts"; itemCount: number; promptExcerpt: string | null }
  | { kind: "failed_run"; agentName: string | null; failureReasonExcerpt: string | null }
  | { kind: "agent_error"; agentName: string | null; failureReasonExcerpt: string | null }
  | {
      kind: "blocker";
      blockingIssue: { id: string | null; identifier: string | null; title: string | null } | null;
      blockedTaskCount?: number;
    }
  | { kind: "budget"; observedPercent: number; amountObserved: number; amountLimit: number }
  | { kind: "generic"; summaryExcerpt: string | null }
);

export interface AttentionFeedResponse {
  /** Server clock at feed time: the desk's shelves and idle labels use it. */
  generatedAt?: string;
  items?: AttentionItem[];
  /** Computed upstream BEFORE pagination — safe to render as a badge. */
  totalCount?: number;
  countsBySourceKind?: Partial<Record<AttentionSourceKind, number>>;
}

// --- Decisions (agent-proposed choices a person answers) --------------------
//
// Ported from upstream Paperclip's decision types
// (paperclip/packages/shared/src/types/decision.ts, validators/decision.ts and
// paperclip/ui/src/api/decisions.ts) (MIT, (c) 2025 Paperclip AI).

export type DecisionEffectStaleness = "strict" | "lenient";
export type DecisionOptionStyle = "default" | "primary" | "destructive";

/** One free-text answer the decision asks for (0-4 per decision). */
export interface DecisionInput {
  id: string;
  label: string;
  placeholder?: string | null;
  required?: boolean;
  maxLength?: number;
}

interface DecisionEffectBase {
  targetIssueId: string;
  staleness: DecisionEffectStaleness;
}

export interface CommentOnIssueDecisionEffect extends DecisionEffectBase {
  type: "comment_on_issue";
  bodyMarkdown: string;
}

export interface CreateIssueDecisionEffect extends DecisionEffectBase {
  type: "create_issue";
  draft: {
    title: string;
    description?: string | null;
    parentId?: string | null;
    assigneeAgentId?: string | null;
    assigneeUserId?: string | null;
    projectId?: string | null;
    goalId?: string | null;
    blockedByIssueIds?: string[];
  };
}

export interface UpdateIssueStatusDecisionEffect extends DecisionEffectBase {
  type: "update_issue_status";
  status: IssueStatus;
  comment?: string | null;
}

export interface AssignIssueDecisionEffect extends DecisionEffectBase {
  type: "assign_issue";
  assigneeAgentId?: string | null;
  assigneeUserId?: string | null;
  comment?: string | null;
}

export interface CancelIssueTreeDecisionEffect extends DecisionEffectBase {
  type: "cancel_issue_tree";
  staleness: "strict";
  reasonComment: string;
}

export interface ResolveBlockerDecisionEffect extends DecisionEffectBase {
  type: "resolve_blocker";
  removeBlockedByIssueIds: string[];
}

export type DecisionEffect =
  | CommentOnIssueDecisionEffect
  | CreateIssueDecisionEffect
  | UpdateIssueStatusDecisionEffect
  | AssignIssueDecisionEffect
  | CancelIssueTreeDecisionEffect
  | ResolveBlockerDecisionEffect;

/** One choice (1-8 per decision). Paperclip runs its effects when picked. */
export interface DecisionOption {
  id: string;
  label: string;
  description?: string | null;
  style?: DecisionOptionStyle;
  effects: DecisionEffect[];
}

export type DecisionStatus = "open" | "decided" | "expired" | "cancelled";
export type DecisionExecutionStatus = "running" | "succeeded" | "partial" | "failed";
export type DecisionEffectExecutionStatus = "claimed" | "executed" | "failed" | "skipped";

export interface DecisionTargetSnapshot {
  status: string;
  assigneeAgentId: string | null;
  assigneeUserId: string | null;
  updatedAt: string;
  descendantCount?: number;
  descendantIds?: string[];
  /** Legacy snapshots created before descendantCount was named explicitly. */
  childCount?: number;
}

export interface Decision {
  id: string;
  companyId: string;
  bundleId: string | null;
  originAgentId: string;
  originIssueId: string;
  originRunId: string;
  ruleKey: string | null;
  title: string;
  body: string;
  options: DecisionOption[];
  inputs: DecisionInput[] | null;
  status: DecisionStatus;
  executionStatus: DecisionExecutionStatus | null;
  chosenOptionId: string | null;
  inputValues: Record<string, string> | null;
  decidedByUserId: string | null;
  decidedAt: string | null;
  expiresAt: string;
  idempotencyKey: string | null;
  targetSnapshots: Record<string, DecisionTargetSnapshot>;
  continuationPolicy: "none" | "wake_origin_agent";
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

/** One effect's result row, present once a decision is decided. */
export interface DecisionEffectExecution {
  id: string;
  decisionId: string;
  effectIndex: number;
  effectType: string;
  targetIssueId: string;
  status: DecisionEffectExecutionStatus;
  result: Record<string, unknown> | null;
  error: string | null;
  activityLogId: string | null;
  executedAt: string | null;
}

/** What `GET /teams/decisions/{id}`, decide and dismiss return. */
export interface DecisionOutcome extends Decision {
  executions: DecisionEffectExecution[];
}

/** A row of `GET /teams/decisions`: each open decision is annotated with which
 *  target issues drifted since it was proposed. */
export interface DecisionListItem extends Decision {
  targetChanged: Record<string, boolean>;
  /** Included on terminal-state rows. */
  executions?: DecisionEffectExecution[];
}

// ---------------------------------------------------------------------------
// Cases. Ported from upstream Paperclip's api/cases.ts
// (paperclip/ui/src/api/cases.ts) (MIT, (c) 2025 Paperclip AI). Wire shapes of
// server/src/routes/cases.ts; dates are ISO strings. Human-writable = status
// + labels only; everything else is agent-authored.
// ---------------------------------------------------------------------------

export const CASE_STATUSES = [
  "draft",
  "in_progress",
  "in_review",
  "approved",
  "done",
  "cancelled",
] as const;
export type CaseStatus = (typeof CASE_STATUSES)[number];

/** Statuses hidden by the list's default `Active` filter. */
export const TERMINAL_CASE_STATUSES: readonly CaseStatus[] = ["done", "cancelled"];

export type CaseLinkRole = "origin" | "work" | "reference";

/** A case row as returned by the list endpoint. */
export interface CaseSummary {
  id: string;
  companyId: string;
  projectId: string | null;
  caseNumber: number;
  identifier: string;
  caseType: string;
  key: string | null;
  title: string;
  summary: string | null;
  status: CaseStatus;
  fields: Record<string, unknown>;
  parentCaseId: string | null;
  createdByAgentId: string | null;
  createdByUserId: string | null;
  completedAt: string | null;
  createdAt: string;
  updatedAt: string;
  /**
   * Present only when `includeAncestors` is requested. Ancestor rows included for
   * tree context are `false`; rows that matched the list query are `true`.
   */
  matchesListFilters?: boolean;
}

export interface CaseDocument {
  id: string;
  title: string | null;
  format: string;
  latestBody: string | null;
  latestRevisionId: string | null;
  latestRevisionNumber: number | null;
  updatedByAgentId: string | null;
  updatedByUserId: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface CaseDocumentRef {
  key: string;
  document: CaseDocument;
}

export interface CaseIssueLink {
  id: string;
  caseId: string;
  issueId: string;
  role: CaseLinkRole;
  createdAt: string;
  issue: { id: string; identifier: string; title: string; status: string };
}

export interface CaseAttachmentRef {
  id: string;
  asset: {
    id: string;
    contentType: string;
    byteSize: number;
    originalFilename: string | null;
  };
  createdAt: string;
  updatedAt: string;
}

/** A lightweight parent reference embedded in the detail payload. */
export interface CaseParentRef {
  id: string;
  identifier: string;
  title: string;
  caseType: string;
  status: CaseStatus;
}

/** The full detail payload (loadCaseDetail on the server). */
export interface CaseDetail extends CaseSummary {
  parent: CaseParentRef | null;
  labels: IssueLabel[];
  issueLinks: CaseIssueLink[];
  documents: CaseDocumentRef[];
  attachments: CaseAttachmentRef[];
}

export type CaseEventKind =
  | "created"
  | "updated"
  | "fields_changed"
  | "status_changed"
  | "issue_linked"
  | "issue_unlinked"
  | "document_revised"
  | "child_linked"
  | "attachment_added"
  | "label_added"
  | "label_removed";

export interface CaseEvent {
  id: string;
  caseId: string;
  kind: CaseEventKind;
  actorType: "user" | "agent" | "system";
  actorUserId: string | null;
  actorAgentId: string | null;
  runId: string | null;
  payload: Record<string, unknown>;
  createdAt: string;
  /** Display name of the acting agent, null for user/system. */
  actorAgentName: string | null;
  /** Issue linked by this event, or the issue whose run produced it. */
  issue: { id: string; identifier: string; title: string; status: string } | null;
}

/** A case linked to an issue, as returned by the issue-page rail endpoint. */
export interface IssueCaseLink {
  id: string;
  role: CaseLinkRole;
  createdAt: string;
  case: {
    id: string;
    identifier: string;
    title: string;
    caseType: string;
    status: CaseStatus;
  };
}

// ---------------------------------------------------------------------------
// External objects: the GitHub PRs / issues an issue mentions, with live status.
// Subset of upstream `paperclip/packages/shared/src/types/external-object.ts`
// (the fields the ported components read).
// ---------------------------------------------------------------------------

export type ExternalObjectStatusCategory =
  | "unknown"
  | "open"
  | "waiting"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "closed"
  | "archived"
  | "auth_required"
  | "unreachable";

export type ExternalObjectStatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "muted";

export type ExternalObjectLivenessState = "unknown" | "fresh" | "stale" | "auth_required" | "unreachable";

export interface ExternalObjectSummaryItem {
  id: string;
  providerKey: string;
  objectType: string;
  displayKey?: string | null;
  iconKey?: string | null;
  displayTitle: string | null;
  statusIconKey?: string | null;
  statusCategory: ExternalObjectStatusCategory;
  statusTone: ExternalObjectStatusTone;
  liveness: ExternalObjectLivenessState;
  isTerminal: boolean;
}

export interface ExternalObject extends ExternalObjectSummaryItem {
  sanitizedCanonicalUrl: string | null;
  statusLabel: string | null;
  lastResolvedAt?: string | null;
  lastChangedAt?: string | null;
}

export interface ExternalObjectMention {
  id: string;
  sourceKind: "title" | "description" | "comment" | "document" | "property" | "plugin";
  documentKey: string | null;
  propertyKey: string | null;
}

export interface ExternalObjectMentionGroup {
  object: ExternalObject | null;
  mentions: ExternalObjectMention[];
  mentionCount: number;
  sourceLabels: string[];
}

export interface ExternalObjectSummary {
  total: number;
  byStatusCategory: Record<string, number>;
  byLiveness: Record<string, number>;
  highestSeverity: ExternalObjectStatusTone;
  staleCount: number;
  authRequiredCount: number;
  unreachableCount: number;
  objects: ExternalObjectSummaryItem[];
}
