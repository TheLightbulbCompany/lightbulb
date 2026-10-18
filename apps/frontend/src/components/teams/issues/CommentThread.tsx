// apps/frontend/src/components/teams/issues/CommentThread.tsx

// Ported from upstream Paperclip's CommentThread
// (ui/src/components/CommentThread.tsx @ c6f5a4d2 on isol8/2026.916-rebase,
// the fork we deploy) and its timeline-event extractor
// (ui/src/lib/issue-timeline-events.ts, extractIssueTimelineEvents), MIT,
// (c) 2025 Paperclip AI. The stock issue timeline: one chronological list of
// comment cards (author, full date-time anchor, copy, markdown body, feedback
// votes + run chip), status/assignee/workspace change rows and run rows, then
// the live run slot and the "Leave a comment..." composer. Sort, tie-break
// order (event < approval < comment < run) and the card/row layout are stock's.
//
// Adaptations vs upstream (only what had to change):
//   - react-query props            -> useTeamsApi.read of the BFF's issue-scoped
//                                     /activity, /runs, /live-runs, /active-run
//                                     (the keys IssueRunLedger reads, so SWR dedupes)
//   - approvals in the timeline    -> Lightbulb's durable interactions instead
//                                     (approvals keep their side-panel section):
//                                     resolved ones are receipt rows here, pending
//                                     ones take over the composer
//   - Identity/MarkdownBody/Button -> teams Identity, TaskMarkdown, EditorialButton
//   - react-router Links           -> in-shell run detail anchors
//   - author labels                -> multi-human: "You" / BFF name / "User";
//                                     no author ids at all reads "Comment" (no votes)
//   - own-comment delete           -> kept from the task chat (stock's classic
//                                     thread wires onDeleteComment the same way)
//   - palette                      -> brand tokens (ok/info/warn/err, muted neutral)
//
// Dropped: plugin slots, queued comments + interrupt, reassign selector,
// image attach, drafts, highlight-on-hash (the page scrolls to the anchor).

"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowRight, Check, Copy, Trash2 } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useScrollToHashAnchor } from "@/components/teams/shared/lib/useScrollToHashAnchor";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import type {
  CompanyAgent,
  CompanyMember,
  FeedbackVote,
  FeedbackVoteValue,
  Issue,
  IssueActivityEvent,
  IssueComment,
  IssueLiveRun,
  IssueRun,
  IssueThreadInteraction,
} from "@/components/teams/shared/types";
import type { IssueThreadInteractionCardProps } from "./IssueThreadInteractionCard";
import { OutputFeedbackButtons } from "./OutputFeedbackButtons";
import { formatDateTime } from "./lib/runRetryState";
import { interactionReplacesComposerSkip } from "./lib/issue-thread-interactions";
import { TaskMarkdown } from "@/components/teams/shared/components/TaskMarkdown";
import { TaskChatComposer } from "./task-chat/TaskChatComposer";
import {
  TaskChatCompactInteractionCard,
  type SubmitInteractionVerdicts,
} from "./task-chat/TaskChatCompactInteractionCard";
import { TaskChatRunTail } from "./task-chat/TaskChatLiveRunPill";

// --- ui/src/lib/issue-timeline-events.ts (extractIssueTimelineEvents) --------

interface TimelineAssignee {
  agentId: string | null;
  userId: string | null;
}

interface TimelineWorkspace {
  label: string | null;
  projectWorkspaceId: string | null;
  executionWorkspaceId: string | null;
  mode: string | null;
}

export interface IssueTimelineEvent {
  id: string;
  createdAt: string;
  actorType: string;
  actorId: string;
  statusChange?: { from: string | null; to: string | null };
  assigneeChange?: { from: TimelineAssignee; to: TimelineAssignee };
  workspaceChange?: { from: TimelineWorkspace; to: TimelineWorkspace };
  followUpRequested?: boolean;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function hasOwn(record: Record<string, unknown>, key: string) {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function workspaceFromRecord(value: unknown): TimelineWorkspace | null {
  const record = asRecord(value);
  if (!record) return null;
  return {
    label: nullableString(record.label),
    projectWorkspaceId: nullableString(record.projectWorkspaceId),
    executionWorkspaceId: nullableString(record.executionWorkspaceId),
    mode: nullableString(record.mode),
  };
}

function formatTimelineWorkspaceLabel(workspace: TimelineWorkspace) {
  const fallbackId = workspace.executionWorkspaceId ?? workspace.projectWorkspaceId;
  return workspace.label ?? (fallbackId ? fallbackId.slice(0, 8) : "None");
}

export function extractIssueTimelineEvents(activity: IssueActivityEvent[] | null | undefined): IssueTimelineEvent[] {
  const events: IssueTimelineEvent[] = [];
  for (const event of activity ?? []) {
    const details = asRecord(event.details);
    if (!details) continue;
    const base = { id: event.id, createdAt: event.createdAt, actorType: event.actorType, actorId: event.actorId };

    if (event.action === "issue.comment_added") {
      if (details.followUpRequested !== true && details.resumeIntent !== true) continue;
      if (details.reopened === true) continue;
      events.push({ ...base, followUpRequested: true });
      continue;
    }
    if (event.action !== "issue.updated") continue;

    const previous = asRecord(details._previous);
    const timelineEvent: IssueTimelineEvent = { ...base };
    if (details.followUpRequested === true || details.resumeIntent === true) {
      timelineEvent.followUpRequested = true;
    }
    if (hasOwn(details, "status")) {
      const from = nullableString(previous?.status) ?? nullableString(details.reopenedFrom);
      const to = nullableString(details.status);
      if (from !== to) timelineEvent.statusChange = { from, to };
    }
    if (hasOwn(details, "assigneeAgentId") || hasOwn(details, "assigneeUserId")) {
      const from: TimelineAssignee = {
        agentId: nullableString(previous?.assigneeAgentId),
        userId: nullableString(previous?.assigneeUserId),
      };
      const to: TimelineAssignee = {
        agentId: hasOwn(details, "assigneeAgentId") ? nullableString(details.assigneeAgentId) : from.agentId,
        userId: hasOwn(details, "assigneeUserId") ? nullableString(details.assigneeUserId) : from.userId,
      };
      if (from.agentId !== to.agentId || from.userId !== to.userId) {
        timelineEvent.assigneeChange = { from, to };
      }
    }
    const change = asRecord(details.workspaceChange);
    const wsFrom = change ? workspaceFromRecord(change.from) : null;
    const wsTo = change ? workspaceFromRecord(change.to) : null;
    if (wsFrom && wsTo && JSON.stringify(wsFrom) !== JSON.stringify(wsTo)) {
      timelineEvent.workspaceChange = { from: wsFrom, to: wsTo };
    }
    if (
      timelineEvent.statusChange ||
      timelineEvent.assigneeChange ||
      timelineEvent.workspaceChange ||
      timelineEvent.followUpRequested
    ) {
      events.push(timelineEvent);
    }
  }
  return events;
}

// --- ui/src/components/CommentThread.tsx ------------------------------------

type AgentMap = ReadonlyMap<string, CompanyAgent>;
type ResolveUserLabel = (userId: string) => string;

function humanizeValue(value: string | null): string {
  if (!value) return "None";
  return value.replace(/_/g, " ");
}

function formatRunStatusLabel(status: string) {
  return status === "timed_out" ? "timed out" : status.replace(/_/g, " ");
}

function runStatusColor(status: string): string {
  switch (status) {
    case "succeeded":
      return "var(--ok)";
    case "failed":
    case "error":
      return "var(--err)";
    case "timed_out":
    case "queued":
    case "pending":
      return "var(--warn)";
    case "running":
      return "var(--info)";
    case "cancelled":
      return "var(--midnight-muted)";
    default:
      return "var(--midnight)";
  }
}

function runTimestamp(run: IssueRun): string {
  return run.finishedAt ?? run.startedAt ?? run.createdAt ?? "";
}

function RunChip({ runId }: { runId: string }) {
  const to = teamsDetailUrl("run", runId);
  return (
    <a
      href={to}
      onClick={(e) => handleShellLinkClick(e, to)}
      className="inline-flex items-center rounded-md px-2 py-1 font-mono text-2xs transition-colors hover:bg-[var(--cream-deeper)]"
      style={{ border: "1px solid var(--edge)", background: "var(--cream-deep)", color: "var(--midnight-muted)" }}
    >
      run {runId.slice(0, 8)}
    </a>
  );
}

function CopyMarkdownButton({ text }: { text: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);
  const label = status === "copied" ? "Copied" : status === "failed" ? "Copy failed" : "Copy";
  return (
    <button
      type="button"
      className="inline-flex min-h-8 items-center gap-1.5 rounded-md px-2.5 text-caption font-medium transition-colors hover:bg-[var(--cream-deep)]"
      style={{ color: status === "copied" ? "var(--ok)" : status === "failed" ? "var(--err)" : "var(--midnight-muted)" }}
      title={label}
      aria-label="Copy comment as markdown"
      onClick={() => {
        void Promise.resolve(navigator.clipboard?.writeText(text))
          .then(() => setStatus("copied"))
          .catch(() => setStatus("failed"));
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => {
          setStatus("idle");
          timeoutRef.current = null;
        }, 1500);
      }}
    >
      {status === "copied" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      <span className="sr-only" aria-live="polite">
        {label}
      </span>
    </button>
  );
}

function commentAuthor(
  comment: IssueComment,
  agentMap: AgentMap,
  currentUserId: string | null | undefined,
): { name: string; isAgent: boolean; ownedByViewer: boolean } {
  if (comment.presentation?.kind === "system_notice" || comment.authorType === "system") {
    return { name: "System", isAgent: false, ownedByViewer: false };
  }
  if (comment.authorAgentId) {
    return { name: agentMap.get(comment.authorAgentId)?.name ?? "Agent", isAgent: true, ownedByViewer: false };
  }
  if (comment.authorType === "user" || comment.authorKind === "user" || comment.authorUserId) {
    const ownedByViewer = Boolean(currentUserId) && comment.authorClerkUserId === currentUserId;
    return { name: ownedByViewer ? "You" : comment.authorName || "User", isAgent: false, ownedByViewer };
  }
  return { name: "Comment", isAgent: false, ownedByViewer: false };
}

function CommentCard({
  comment,
  agentMap,
  currentUserId,
  feedbackVote,
  onVote,
  onDelete,
}: {
  comment: IssueComment;
  agentMap: AgentMap;
  currentUserId?: string | null;
  feedbackVote: FeedbackVoteValue | null;
  onVote?: (vote: FeedbackVoteValue, options?: { reason?: string }) => Promise<void>;
  onDelete?: (commentId: string) => Promise<void>;
}) {
  const [deleteOpen, setDeleteOpen] = useState(false);
  const isDeleted = Boolean(comment.deletedAt);
  const author = commentAuthor(comment, agentMap, currentUserId);
  const runId = comment.createdByRunId ?? null;
  const showVotes = author.isAgent && Boolean(onVote) && !isDeleted;

  return (
    <div
      id={`comment-${comment.id}`}
      data-testid="issue-comment-card"
      className="min-w-0 scroll-mt-4 overflow-hidden rounded-sm p-3"
      style={{
        border: "1px solid var(--edge)",
        background: isDeleted ? "var(--cream-deep)" : undefined,
        color: isDeleted ? "var(--midnight-muted)" : undefined,
      }}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <Identity name={author.name} size="sm" />
        <span className="flex items-center gap-1.5">
          <a
            href={`#comment-${comment.id}`}
            className="text-caption transition-colors hover:underline"
            style={{ color: "var(--midnight-muted)" }}
          >
            {formatDateTime(comment.createdAt)}
          </a>
          {!isDeleted ? <CopyMarkdownButton text={comment.body} /> : null}
          {!isDeleted && author.ownedByViewer && onDelete ? (
            <button
              type="button"
              className="inline-flex min-h-8 items-center rounded-md px-2 transition-colors hover:bg-[var(--cream-deep)]"
              style={{ color: "var(--midnight-muted)" }}
              title="Delete comment"
              aria-label="Delete comment"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          ) : null}
        </span>
      </div>
      {isDeleted ? (
        <div className="text-ui italic">Comment deleted</div>
      ) : (
        <div className="text-ui" style={{ color: "var(--midnight)" }}>
          <TaskMarkdown skipHtml body={comment.body} />
        </div>
      )}
      {showVotes || (runId && !isDeleted) ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 pt-3" style={{ borderTop: "1px solid var(--edge-soft)" }}>
          {showVotes && onVote ? <OutputFeedbackButtons activeVote={feedbackVote} onVote={onVote} /> : null}
          {runId ? (
            <span className="ml-auto">
              <RunChip runId={runId} />
            </span>
          ) : null}
        </div>
      ) : null}
      {onDelete ? (
        <EditorialDialog
          open={deleteOpen}
          onOpenChange={setDeleteOpen}
          title="Delete comment?"
          description="This will replace the comment with a deleted-comment marker."
          footer={
            <>
              <EditorialButton variant="secondary" size="sm" onClick={() => setDeleteOpen(false)}>
                Cancel
              </EditorialButton>
              <EditorialButton
                variant="destructive"
                size="sm"
                onClick={() => {
                  setDeleteOpen(false);
                  void onDelete(comment.id);
                }}
              >
                Delete comment
              </EditorialButton>
            </>
          }
        />
      ) : null}
    </div>
  );
}

function ChangeRow({ label, from, to }: { label: string; from: string; to: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-ui">
      <span className="w-16 text-2xs font-medium uppercase tracking-wider" style={{ color: "var(--midnight-muted)" }}>
        {label}
      </span>
      <span style={{ color: "var(--midnight-muted)" }}>{from}</span>
      <ArrowRight className="h-3.5 w-3.5" style={{ color: "var(--midnight-muted)" }} />
      <span className="font-medium" style={{ color: "var(--midnight)" }}>
        {to}
      </span>
    </div>
  );
}

function TimelineEventCard({
  event,
  agentMap,
  resolveUserLabel,
}: {
  event: IssueTimelineEvent;
  agentMap: AgentMap;
  resolveUserLabel: ResolveUserLabel;
}) {
  const actorName =
    event.actorType === "agent"
      ? agentMap.get(event.actorId)?.name ?? event.actorId.slice(0, 8)
      : event.actorType === "system"
        ? "System"
        : resolveUserLabel(event.actorId);
  const assigneeLabel = (a: TimelineAssignee) =>
    a.agentId ? agentMap.get(a.agentId)?.name ?? a.agentId.slice(0, 8) : a.userId ? resolveUserLabel(a.userId) : "Unassigned";
  return (
    <div id={`activity-${event.id}`} className="flex items-start gap-2.5 py-1.5" data-testid="issue-timeline-event">
      <Identity name={actorName} size="sm" avatarOnly />
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1 text-ui">
          <span className="font-medium" style={{ color: "var(--midnight)" }}>
            {actorName}
          </span>
          <span style={{ color: "var(--midnight-muted)" }}>
            {event.followUpRequested ? "requested follow-up" : "updated this task"}
          </span>
          <a
            href={`#activity-${event.id}`}
            className="text-ui transition-colors hover:underline"
            style={{ color: "var(--midnight-muted)" }}
            title={formatDateTime(event.createdAt)}
          >
            {timeAgo(event.createdAt)}
          </a>
        </div>
        {event.statusChange ? (
          <ChangeRow label="Status" from={humanizeValue(event.statusChange.from)} to={humanizeValue(event.statusChange.to)} />
        ) : null}
        {event.assigneeChange ? (
          <ChangeRow
            label="Assignee"
            from={assigneeLabel(event.assigneeChange.from)}
            to={assigneeLabel(event.assigneeChange.to)}
          />
        ) : null}
        {event.workspaceChange ? (
          <ChangeRow
            label="Workspace"
            from={formatTimelineWorkspaceLabel(event.workspaceChange.from)}
            to={formatTimelineWorkspaceLabel(event.workspaceChange.to)}
          />
        ) : null}
      </div>
    </div>
  );
}

function RunRow({ run, agentMap }: { run: IssueRun; agentMap: AgentMap }) {
  const actorName = (run.agentId && agentMap.get(run.agentId)?.name) || run.agentId?.slice(0, 8) || "Agent";
  const at = runTimestamp(run);
  return (
    <div id={`run-${run.runId}`} className="flex items-center gap-2.5 py-1.5" data-testid="issue-timeline-run">
      <Identity name={actorName} size="sm" avatarOnly />
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-1 text-ui">
        <span className="font-medium" style={{ color: "var(--midnight)" }}>
          {actorName}
        </span>
        <span style={{ color: "var(--midnight-muted)" }}>run</span>
        <RunChip runId={run.runId} />
        <span className="font-medium" style={{ color: runStatusColor(run.status) }}>
          {formatRunStatusLabel(run.status)}
        </span>
        {at ? (
          <a
            href={`#run-${run.runId}`}
            className="text-ui transition-colors hover:underline"
            style={{ color: "var(--midnight-muted)" }}
            title={formatDateTime(at)}
          >
            {timeAgo(at)}
          </a>
        ) : null}
      </div>
    </div>
  );
}

type TimelineItem =
  | { kind: "event"; id: string; createdAtMs: number; event: IssueTimelineEvent }
  | { kind: "interaction"; id: string; createdAtMs: number; interaction: IssueThreadInteraction }
  | { kind: "comment"; id: string; createdAtMs: number; comment: IssueComment }
  | { kind: "run"; id: string; createdAtMs: number; run: IssueRun };

const KIND_ORDER = { event: 0, interaction: 1, comment: 2, run: 3 } as const;

function toMs(value: Date | string | null | undefined): number {
  if (!value) return 0;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : 0;
}

/** Stock's timeline merge: ascending by time, then kind order, then id. */
export function buildTimeline(input: {
  comments: readonly IssueComment[];
  events: readonly IssueTimelineEvent[];
  interactions: readonly IssueThreadInteraction[];
  runs: readonly IssueRun[];
}): TimelineItem[] {
  const items: TimelineItem[] = [
    ...input.comments.map((comment) => ({ kind: "comment" as const, id: comment.id, createdAtMs: toMs(comment.createdAt), comment })),
    ...input.events.map((event) => ({ kind: "event" as const, id: event.id, createdAtMs: toMs(event.createdAt), event })),
    ...input.interactions.map((interaction) => ({
      kind: "interaction" as const,
      id: interaction.id,
      createdAtMs: toMs(interaction.createdAt),
      interaction,
    })),
    ...input.runs.map((run) => ({ kind: "run" as const, id: run.runId, createdAtMs: toMs(runTimestamp(run)), run })),
  ];
  return items.sort((a, b) => {
    if (a.createdAtMs !== b.createdAtMs) return a.createdAtMs - b.createdAtMs;
    if (a.kind === b.kind) return a.id.localeCompare(b.id);
    return KIND_ORDER[a.kind] - KIND_ORDER[b.kind];
  });
}

/** Upstream durableInputLabel, for the kinds we carry. */
function durableInputLabel(interaction: IssueThreadInteraction): string {
  if (interaction.kind === "ask_user_questions") return interaction.title ?? "Questions";
  if (interaction.kind === "suggest_tasks") return interaction.title ?? "Suggested tasks";
  if (interaction.kind === "request_checkbox_confirmation") return interaction.title ?? "Choose options";
  if (interaction.kind === "request_item_verdicts") return interaction.title ?? "Review items";
  if (interaction.payload.target?.type === "issue_document" && interaction.payload.target.key === "plan") {
    return interaction.title ?? "Review plan";
  }
  return interaction.title ?? "Confirmation";
}

type InteractionHandlers = Pick<
  IssueThreadInteractionCardProps,
  "onAcceptInteraction" | "onRejectInteraction" | "onSubmitInteractionAnswers" | "onCancelInteraction"
>;

export interface CommentThreadProps extends InteractionHandlers {
  issue: Issue;
  comments: IssueComment[];
  interactions?: IssueThreadInteraction[];
  agents: CompanyAgent[];
  members?: CompanyMember[];
  /** Clerk id of the viewer. */
  currentUserId?: string | null;
  onAdd: (body: string) => Promise<void>;
  onDeleteComment?: (commentId: string) => Promise<void>;
  feedbackVotes?: Map<string, FeedbackVote>;
  onVote?: (commentId: string, vote: FeedbackVoteValue, options?: { reason?: string }) => Promise<void>;
  onSkipInteraction?: (interaction: IssueThreadInteraction) => Promise<void>;
  onSubmitInteractionVerdicts?: SubmitInteractionVerdicts;
  /** Rendered directly above the composer (the monitor composer strip). */
  composerAccessory?: ReactNode;
}

function asArray<T>(data: unknown): T[] {
  return Array.isArray(data) ? (data as T[]) : [];
}

export function CommentThread({
  issue,
  comments,
  interactions = [],
  agents,
  members = [],
  currentUserId,
  onAdd,
  onDeleteComment,
  feedbackVotes,
  onVote,
  onSkipInteraction,
  onSubmitInteractionVerdicts,
  composerAccessory,
  onAcceptInteraction,
  onRejectInteraction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
}: CommentThreadProps) {
  const { read, post, mutate } = useTeamsApi();
  const { data: activity } = read<IssueActivityEvent[]>(`/issues/${issue.id}/activity`);
  const { data: runsResp } = read<{ runs: IssueRun[] }>(`/issues/${issue.id}/runs`);
  const liveRunsResp = read<IssueLiveRun[]>(`/issues/${issue.id}/live-runs`, { shouldRetryOnError: false });
  const activeRunResp = read<IssueLiveRun | null>(`/issues/${issue.id}/active-run`, { shouldRetryOnError: false });

  useScrollToHashAnchor(comments.length > 0);

  // Upstream picks the tail from liveRuns (newest first), then activeRun.
  const activeRun = activeRunResp.data && typeof activeRunResp.data === "object" ? activeRunResp.data : null;
  const tailRun = asArray<IssueLiveRun>(liveRunsResp.data)[0] ?? activeRun ?? null;

  // The run's reply lands as a comment when it settles; refetch once the tail clears.
  const hadTail = useRef(false);
  const commentsKey = `/teams/issues/${issue.id}/comments`;
  const refetchComments = useCallback(() => mutate(commentsKey), [mutate, commentsKey]);
  useEffect(() => {
    if (tailRun) hadTail.current = true;
    else if (hadTail.current) {
      hadTail.current = false;
      void refetchComments();
    }
  }, [tailRun, refetchComments]);

  const agentMap = useMemo(() => new Map(agents.map((a) => [a.id, a] as const)), [agents]);
  const resolveUserLabel: ResolveUserLabel = (userId) => {
    const member = members.find((m) => m.userId === userId || m.paperclipUserId === userId);
    if (member && currentUserId && member.userId === currentUserId) return "You";
    return member?.name ?? member?.email ?? "Board";
  };

  const timeline = buildTimeline({
    comments,
    events: extractIssueTimelineEvents(asArray<IssueActivityEvent>(activity)),
    interactions: interactions.filter((interaction) => interaction.status !== "pending"),
    runs: asArray<IssueRun>(runsResp?.runs),
  });

  // Pending interactions take over the composer: newest first, reopened by any new one.
  const pending = interactions
    .filter((interaction) => interaction.status === "pending")
    .sort((a, b) => toMs(b.createdAt) - toMs(a.createdAt));
  const pendingKey = pending.map((interaction) => interaction.id).join(",");
  const [takeoverOpen, setTakeoverOpen] = useState(true);
  const [selectedPendingId, setSelectedPendingId] = useState<string | null>(null);
  const [seenPendingKey, setSeenPendingKey] = useState(pendingKey);
  if (pendingKey !== seenPendingKey) {
    const seen = new Set(seenPendingKey.split(","));
    if (pending.some((interaction) => !seen.has(interaction.id))) setTakeoverOpen(true);
    setSeenPendingKey(pendingKey);
  }
  const selectedPending = pending.find((interaction) => interaction.id === selectedPendingId) ?? pending[0] ?? null;
  const interactionHandlers = {
    agentMap,
    currentUserId,
    onAcceptInteraction,
    onRejectInteraction,
    onSubmitInteractionAnswers,
    onCancelInteraction,
    onSubmitInteractionVerdicts,
  };
  const takeover =
    takeoverOpen && selectedPending
      ? {
          id: selectedPending.id,
          label: durableInputLabel(selectedPending),
          pendingCount: pending.length,
          content: (
            <TaskChatCompactInteractionCard
              key={selectedPending.id}
              interaction={selectedPending}
              presentation="takeover"
              {...interactionHandlers}
            />
          ),
          onDismiss: () => setTakeoverOpen(false),
          onSkip: async () => {
            if (!onSkipInteraction) throw new Error("Skipping this interaction is unavailable.");
            await onSkipInteraction(selectedPending);
            setTakeoverOpen(false);
          },
          onShowNext: () => {
            const index = pending.findIndex((interaction) => interaction.id === selectedPending.id);
            setSelectedPendingId(pending[(index + 1) % pending.length]?.id ?? null);
          },
          inlineSkip:
            selectedPending.kind === "ask_user_questions" || selectedPending.kind === "request_item_verdicts",
          hideSkip: interactionReplacesComposerSkip(selectedPending),
        }
      : null;
  const pendingTakeover =
    !takeover && selectedPending
      ? { count: pending.length, label: durableInputLabel(selectedPending), onOpen: () => setTakeoverOpen(true) }
      : null;

  const deleteComment = onDeleteComment
    ? async (commentId: string) => {
        try {
          await onDeleteComment(commentId);
          void refetchComments();
        } catch (e) {
          notifyApiError(e, "Couldn't delete the comment.");
        }
      }
    : undefined;

  const stopRun = tailRun
    ? async () => {
        await post(`/heartbeat-runs/${tailRun.id}/cancel`, {});
        void liveRunsResp.mutate();
        void activeRunResp.mutate();
      }
    : undefined;

  return (
    <div className="space-y-4" data-testid="issue-comment-thread">
      <h3 className="text-ui font-semibold" style={{ color: "var(--midnight)" }}>
        Timeline ({timeline.length})
      </h3>

      {timeline.length === 0 ? (
        <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>
          No timeline entries yet.
        </p>
      ) : (
        <div className="space-y-3">
          {timeline.map((item) => {
            if (item.kind === "event") {
              return (
                <TimelineEventCard
                  key={`event:${item.id}`}
                  event={item.event}
                  agentMap={agentMap}
                  resolveUserLabel={resolveUserLabel}
                />
              );
            }
            if (item.kind === "run") return <RunRow key={`run:${item.id}`} run={item.run} agentMap={agentMap} />;
            if (item.kind === "interaction") {
              return (
                <div key={`interaction:${item.id}`} id={`interaction-${item.id}`} className="w-full py-1.5">
                  <TaskChatCompactInteractionCard interaction={item.interaction} {...interactionHandlers} />
                </div>
              );
            }
            const comment = item.comment;
            return (
              <CommentCard
                key={comment.id}
                comment={comment}
                agentMap={agentMap}
                currentUserId={currentUserId}
                feedbackVote={feedbackVotes?.get(comment.id)?.vote ?? null}
                onVote={onVote ? (vote, options) => onVote(comment.id, vote, options) : undefined}
                onDelete={deleteComment}
              />
            );
          })}
        </div>
      )}

      {tailRun ? <TaskChatRunTail run={tailRun} onStop={stopRun} /> : null}

      {composerAccessory}
      <TaskChatComposer takeover={takeover} pendingTakeover={pendingTakeover} onAdd={onAdd} agents={agents} />
    </div>
  );
}
