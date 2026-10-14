// apps/frontend/src/components/teams/issues/IssueActivityEventRow.tsx

// Ported from upstream Paperclip @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI):
//   - the activity row IssueDetail's Activity tab hands IssueRunLedger as
//     `renderActivityEvent`, with its ActorIdentity (ui/src/pages/IssueDetail.tsx)
//   - successfulRunHandoffActivityTone (ui/src/lib/successful-run-handoff.ts)
//   - formatIssueActivityAction and its label tables (ui/src/lib/activity-format.ts)
//
// Adaptations vs upstream (only what had to change):
//   - agentMap / userProfileMap    -> the ledger's agent map + member resolver
//                                     (a user actor with no member row reads "Board")
//   - relativeTime                 -> shared/lib/timeAgo
//   - red/amber palette            -> brand state tokens err/warn
//
// Dropped for now (their own stock modules, not needed for the write-denial
// notice): IssueReferenceActivitySummary, IssueFieldChangeReceipt, and
// activity-format's field-level `issue.updated` / structured-change phrasing, so
// an update reads "updated the issue" instead of naming the field.

"use client";

import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, IssueActivityEvent } from "@/components/teams/shared/types";
import { IssueWriteDenialNotice } from "./IssueWriteDenialNotice";
import { issueWriteDenialForActivity } from "./lib/issue-write-denial";

// --- ui/src/lib/successful-run-handoff.ts -----------------------------------

const SUCCESSFUL_RUN_HANDOFF_REQUIRED_ACTION = "issue.successful_run_handoff_required";
const SUCCESSFUL_RUN_HANDOFF_ESCALATED_ACTION = "issue.successful_run_handoff_escalated";

function successfulRunHandoffActivityTone(action: string) {
  if (action === SUCCESSFUL_RUN_HANDOFF_ESCALATED_ACTION) {
    return { className: "border-err/35 bg-err/10 text-err", iconClassName: "text-err" };
  }
  if (action === SUCCESSFUL_RUN_HANDOFF_REQUIRED_ACTION) {
    return { className: "border-warn/40 bg-warn/10 text-warn", iconClassName: "text-warn" };
  }
  return { className: "border-border/60 text-muted-foreground", iconClassName: "text-muted-foreground" };
}

// --- ui/src/lib/activity-format.ts (formatIssueActivityAction subset) -------

type ActivityDetails = Record<string, unknown> | null | undefined;

const ISSUE_ACTIVITY_LABELS: Record<string, string> = {
  "issue.created": "created the issue",
  "issue.updated": "updated the issue",
  "issue.checked_out": "checked out the issue",
  "issue.released": "released the issue",
  "issue.comment_added": "added a comment",
  "issue.comment_cancelled": "cancelled a queued comment",
  "issue.queued_comment_edited": "edited a queued comment",
  "issue.queued_comments_reordered": "reordered queued comments",
  "issue.queued_comment_discarded": "discarded a queued comment",
  "issue.comment_deleted": "deleted a comment",
  "issue.feedback_vote_saved": "saved feedback on an AI output",
  "issue.attachment_added": "added an attachment",
  "issue.attachment_removed": "removed an attachment",
  "issue.document_created": "created a document",
  "issue.document_updated": "updated a document",
  "issue.document_locked": "locked a document",
  "issue.document_unlocked": "unlocked a document",
  "issue.document_deleted": "deleted a document",
  "issue.monitor_scheduled": "scheduled a monitor",
  "issue.monitor_triggered": "triggered a monitor",
  "issue.monitor_cleared": "cleared a monitor",
  "issue.monitor_skipped": "skipped a monitor",
  "issue.monitor_exhausted": "exhausted a monitor",
  "issue.monitor_recovery_wake_queued": "queued a monitor recovery wake",
  "issue.monitor_recovery_issue_created": "created a monitor recovery issue",
  "issue.monitor_escalated_to_board": "escalated a monitor to the board",
  "issue.deleted": "deleted the issue",
  "issue.successful_run_handoff_required": "Run finished without a clear next step",
  "issue.successful_run_handoff_resolved": "Next step chosen",
  "issue.successful_run_handoff_escalated": "Run finished without a next step - recovery escalated",
  "issue.cross_issue_influence_cap_rejected": "hit the per-run cross-task write cap",
  "issue.cross_issue_influence_observed": "made a cross-task write",
  "issue.attribution_spoof_rejected": "tried to choose its own responsible user",
  "issue.recovery_action_opened": "Opened a source-scoped recovery action",
  "issue.recovery_action_resolved": "Resolved the recovery action",
  "issue.recovery_action_escalated": "Escalated the recovery action",
  "issue.accepted_plan_decomposition_updated": "updated the accepted-plan decomposition",
  "agent.created": "created an agent",
  "agent.updated": "updated the agent",
  "agent.paused": "paused the agent",
  "agent.resumed": "resumed the agent",
  "agent.error_cleared": "cleared the agent error",
  "agent.terminated": "terminated the agent",
  "heartbeat.invoked": "invoked a heartbeat",
  "heartbeat.cancelled": "cancelled a heartbeat",
  "heartbeat.output_stale_source_resolved": "System folded a stale run",
  "heartbeat.output_stale_recovery_recursion_refused": "Refused recovery-on-recovery escalation",
  "approval.created": "requested approval",
  "approval.approved": "approved",
  "approval.rejected": "rejected",
  "issue.thread_interaction_created": "asked for a decision",
  "issue.thread_interaction_accepted": "accepted the request",
  "issue.thread_interaction_rejected": "rejected the request",
  "issue.thread_interaction_answered": "answered the request",
  "issue.thread_interaction_withdrawn": "withdrew the request",
  "issue.thread_interaction_cancelled": "cancelled the request",
  "issue.thread_interaction_skipped": "skipped the request",
  "issue.thread_interaction_expired": "expired the request",
  "issue.thread_interaction_item_verdicts_submitted": "submitted verdicts on the request",
  "issue.stalled_review_decided": "recorded a review verdict",
};

/**
 * `issue.stalled_review_decided` carries the verb the actor chose, so the line
 * names the verdict ("approved the review") rather than the generic action.
 * Mirrors `StalledReviewDecisionAction` in shared.
 */
const STALLED_REVIEW_DECISION_LABELS: Record<string, string> = {
  approve: "approved the review",
  request_changes: "requested changes on the review",
  send_back: "sent the review back to work",
};

/**
 * `issue.thread_interaction_accepted` / `_rejected` fire for *every* interaction
 * kind, not only for a review. A task suggestion or a question is accepted, not
 * approved, so the kind on the event picks the verb. Kinds absent from a map
 * keep the neutral "accepted the request" wording from the tables above, which
 * is also the fallback for an event that carries no kind.
 */
const INTERACTION_ACCEPTED_LABELS: Record<string, string> = {
  request_confirmation: "approved the request",
  request_checkbox_confirmation: "approved the request",
  suggest_tasks: "accepted the task suggestions",
  ask_user_questions: "accepted the answers",
};

const INTERACTION_REJECTED_LABELS: Record<string, string> = {
  request_confirmation: "rejected the request",
  request_checkbox_confirmation: "rejected the request",
  suggest_tasks: "declined the task suggestions",
  ask_user_questions: "declined the questions",
};

/**
 * Kind-aware wording for an interaction outcome, or `null` when the tables
 * above already say it well enough.
 */
function formatInteractionOutcomeLabel(action: string, details: ActivityDetails): string | null {
  const table = action === "issue.thread_interaction_accepted"
    ? INTERACTION_ACCEPTED_LABELS
    : action === "issue.thread_interaction_rejected"
      ? INTERACTION_REJECTED_LABELS
      : null;
  if (!table) return null;
  const kind = typeof details?.interactionKind === "string" ? details.interactionKind : null;
  return kind ? table[kind] ?? null : null;
}

export function formatIssueActivityAction(action: string, details?: ActivityDetails): string {
  if (action === "issue.stalled_review_decided") {
    const decision = typeof details?.action === "string" ? details.action : null;
    const label = decision ? STALLED_REVIEW_DECISION_LABELS[decision] : null;
    if (label) return label;
  }

  const outcomeLabel = formatInteractionOutcomeLabel(action, details);
  if (outcomeLabel) return outcomeLabel;

  if (action.startsWith("issue.monitor_") && details) {
    const serviceName = typeof details.serviceName === "string" && details.serviceName.trim()
      ? details.serviceName.trim()
      : null;
    const base = ISSUE_ACTIVITY_LABELS[action] ?? action.replace(/[._]/g, " ");
    return serviceName ? `${base} for ${serviceName}` : base;
  }

  if (
    (
      action === "issue.document_created" ||
      action === "issue.document_updated" ||
      action === "issue.document_locked" ||
      action === "issue.document_unlocked" ||
      action === "issue.document_deleted"
    ) &&
    details
  ) {
    const key = typeof details.key === "string" ? details.key : "document";
    const title = typeof details.title === "string" && details.title ? ` (${details.title})` : "";
    return `${ISSUE_ACTIVITY_LABELS[action] ?? action} ${key}${title}`;
  }

  return ISSUE_ACTIVITY_LABELS[action] ?? action.replace(/[._]/g, " ");
}

// --- ui/src/pages/IssueDetail.tsx (ActorIdentity + renderActivityEvent) -----

type AgentNameMap = ReadonlyMap<string, Pick<CompanyAgent, "name">>;
type ResolveUserLabel = (userId: string) => string | null | undefined;

function ActorIdentity({
  evt,
  agentMap,
  resolveUserLabel,
}: {
  evt: IssueActivityEvent;
  agentMap: AgentNameMap;
  resolveUserLabel?: ResolveUserLabel;
}) {
  const id = evt.actorId;
  if (evt.actorType === "agent") {
    const agent = agentMap.get(id);
    return <Identity name={agent?.name ?? id.slice(0, 8)} size="sm" />;
  }
  if (evt.actorType === "system") return <Identity name="System" size="sm" />;
  if (evt.actorType === "user") {
    return <Identity name={resolveUserLabel?.(id) ?? "Board"} size="sm" />;
  }
  return <Identity name={id || "Unknown"} size="sm" />;
}

export function IssueActivityEventRow({
  event: evt,
  agentMap,
  resolveUserLabel,
}: {
  event: IssueActivityEvent;
  agentMap: AgentNameMap;
  resolveUserLabel?: ResolveUserLabel;
}) {
  const tone = successfulRunHandoffActivityTone(evt.action);
  const isHandoffWarning =
    evt.action === SUCCESSFUL_RUN_HANDOFF_REQUIRED_ACTION ||
    evt.action === SUCCESSFUL_RUN_HANDOFF_ESCALATED_ACTION;
  // A refused write explains itself here, not just in the API error.
  const denial = issueWriteDenialForActivity(evt.action, evt.details, {
    actorLabel: evt.agentId ? (agentMap.get(evt.agentId)?.name ?? null) : null,
    responsibleUserName: evt.responsibleUserId ? (resolveUserLabel?.(evt.responsibleUserId) ?? null) : null,
  });
  return (
    <div className={cn("space-y-1.5 rounded-lg border px-3 py-2 text-caption", tone.className)}>
      <div className="flex items-center gap-1.5">
        {isHandoffWarning ? <AlertTriangle className={cn("h-3.5 w-3.5 shrink-0", tone.iconClassName)} /> : null}
        <ActorIdentity evt={evt} agentMap={agentMap} resolveUserLabel={resolveUserLabel} />
        <span>{formatIssueActivityAction(evt.action, evt.details)}</span>
        <span className="ml-auto shrink-0">{timeAgo(evt.createdAt)}</span>
      </div>
      {denial ? <IssueWriteDenialNotice code={denial.code} context={denial.context} /> : null}
    </div>
  );
}
