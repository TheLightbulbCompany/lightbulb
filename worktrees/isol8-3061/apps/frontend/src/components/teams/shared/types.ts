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
  /** BFF-enriched display name for a human author (authorUserId is a
   * Paperclip principal id the frontend can't resolve itself). */
  authorName?: string | null;
  /** BFF-enriched Clerk id of a human author — compare against the viewer's
   * Clerk id to render "You". */
  authorClerkUserId?: string | null;
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
  /** Link the task to a goal, or ``null`` to detach it. ``undefined`` leaves
   * the link unchanged. The BFF camelCase-aliases this to ``goalId``. */
  goalId?: string | null;
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
  /** Set when the task is hidden. Every list/board/search read filters these
   * out upstream, but fetch-by-id does not — so the detail page is the one
   * surface that can see (and restore) a hidden task. */
  hiddenAt?: string | null;
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
  /** Live execution state (detail route only — the list route nulls it). Carries
   * the monitor's real status/attempt count, which outlives the one-shot policy
   * entry once the monitor fires. */
  executionState?: IssueExecutionState | null;
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

// --- the discriminated union ---

export interface IssueThreadInteractionBase
  extends IssueThreadInteractionActorFields {
  id: string;
  companyId: string;
  issueId: string;
  kind: IssueThreadInteractionKind;
  idempotencyKey?: string | null;
  sourceCommentId?: string | null;
  sourceRunId?: string | null;
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

export type IssueThreadInteraction =
  | SuggestTasksInteraction
  | AskUserQuestionsInteraction
  | RequestConfirmationInteraction
  | RequestCheckboxConfirmationInteraction;

export type IssueThreadInteractionPayload =
  | SuggestTasksPayload
  | AskUserQuestionsPayload
  | RequestConfirmationPayload
  | RequestCheckboxConfirmationPayload;

export type IssueThreadInteractionResult =
  | SuggestTasksResult
  | AskUserQuestionsResult
  | RequestConfirmationResult
  | RequestCheckboxConfirmationResult;

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
}

export interface AttentionFeedResponse {
  items?: AttentionItem[];
  /** Computed upstream BEFORE pagination — safe to render as a badge. */
  totalCount?: number;
  countsBySourceKind?: Partial<Record<AttentionSourceKind, number>>;
}
