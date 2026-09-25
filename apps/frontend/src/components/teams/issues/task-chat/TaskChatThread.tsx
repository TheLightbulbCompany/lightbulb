// apps/frontend/src/components/teams/issues/task-chat/TaskChatThread.tsx

// Ported from upstream Paperclip's TaskChatThread
// (paperclip/ui/src/components/TaskChatThread.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI) — stock 916's default task page, where "the chat
// IS the page": the thread header, the description as the first bubble, the
// author-typed comment bubbles with interaction cards interleaved, the live run
// tail (TaskChatLiveRunPill + the empty-tail status copy of the legacy-adapter
// branch that hermes_gateway takes), and a sticky composer dock with
// `composerAccessory` above it and `footer` below.
//
// Upstream's orchestrator is 3k lines on top of IssueChatThread's 6.8k-line
// prop surface, so this is a composition of the ported leaves, not a line port.
// What it keeps of the orchestration: tail run = the newest queued/running run
// (live-runs, then active-run), "Waiting to start..." / currentStatusMessage /
// "Waiting for transcript..." empty copy, Stop on the composer while live.
//
// Live updates (adaptation): upstream's LiveUpdatesProvider socket is not
// exposed. TeamsEventsProvider turns Paperclip's AppSync `paperclip.event`s into
// SWR revalidation — `activity.logged` for this issue refreshes the whole
// `/teams/issues/{id}/*` subtree (comments, interactions, live-runs,
// active-run) and run queued/status events (plus the 20s board keeper) refresh
// the per-issue run lists.
//
// Interactions (part B2): stock's pending-input takeover — pending interactions
// (newest first) replace the composer with TaskChatInteractionCard's takeover
// form, a new one reopens it, "N pending" cycles, X dismisses to the pending
// indicator, and Skip calls the skip route; resolved ones collapse to receipt
// rows in the timeline.
//
// Deferred to part B: queued comments (edit/reorder/steer/interrupt/discard),
// runtime-request resolve, the native paperclip_runner turn, attachments and
// uploads, user mentions, the activity route + IssueWriteDenialNotice, and the
// streamed transcript (TaskChatLiveTail/Thinking + transcript-adapter + the
// hermes_gateway stdout parser) once the Hermes facade serves a run events route.

"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { useScrollToHashAnchor } from "@/components/teams/shared/lib/useScrollToHashAnchor";
import type { IssueThreadInteractionCardProps } from "../IssueThreadInteractionCard";
import { interactionReplacesComposerSkip } from "../lib/issue-thread-interactions";
import { buildTeamsThreadItems } from "../lib/teams-thread-items";
import type {
  CompanyAgent,
  FeedbackVote,
  FeedbackVoteValue,
  Issue,
  IssueComment,
  IssueLiveRun,
  IssueThreadInteraction,
} from "@/components/teams/shared/types";
import { commentToTaskChatItem } from "./task-chat-adapter";
import { TaskChatBubble } from "./TaskChatBubble";
import { TaskChatBubbleActions } from "./TaskChatBubbleActions";
import { TaskChatDescriptionBubble } from "./TaskChatDescriptionBubble";
import { TaskChatComposer } from "./TaskChatComposer";
import { TaskChatInteractionCard } from "./TaskChatInteractionCard";
import type { SubmitInteractionVerdicts } from "./TaskChatCompactInteractionCard";
import { TaskChatLiveRunPill } from "./TaskChatLiveRunPill";

type InteractionHandlers = Pick<
  IssueThreadInteractionCardProps,
  "onAcceptInteraction" | "onRejectInteraction" | "onSubmitInteractionAnswers" | "onCancelInteraction"
>;

export interface TaskChatThreadProps extends InteractionHandlers {
  onSkipInteraction?: (interaction: IssueThreadInteraction) => Promise<void>;
  onSubmitInteractionVerdicts?: SubmitInteractionVerdicts;
  issue: Issue;
  comments: IssueComment[];
  interactions?: IssueThreadInteraction[];
  agents: CompanyAgent[];
  /** Clerk id of the viewer. */
  currentUserId?: string | null;
  isLoading?: boolean;
  onAdd: (body: string) => Promise<void>;
  onDeleteComment?: (commentId: string) => Promise<void>;
  onSaveDescription: (description: string) => Promise<unknown>;
  feedbackVotes?: Map<string, FeedbackVote>;
  onVote?: (commentId: string, vote: FeedbackVoteValue, options?: { reason?: string }) => Promise<void>;
  /** Breadcrumb + issue header + status cards, rendered at the top of the thread. */
  threadHeader?: ReactNode;
  /** Rendered directly above the composer (the monitor composer strip). */
  composerAccessory?: ReactNode;
  footer?: ReactNode;
  emptyMessage?: string;
}

function toMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
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

function asRuns(data: unknown): IssueLiveRun[] {
  return Array.isArray(data) ? (data as IssueLiveRun[]) : [];
}

/** The live run tail: stock's pill + empty-tail status copy for the newest live run. */
function TaskChatRunTail({ run }: { run: IssueLiveRun }) {
  return (
    <div data-testid="task-chat-live-transcript">
      <TaskChatLiveRunPill
        status={run.status}
        startedAtMs={toMs(run.startedAt) ?? toMs(run.createdAt)}
        finishedAtMs={toMs(run.finishedAt)}
      />
      <div className="px-1 py-1 text-xs" style={{ color: "var(--midnight-muted)" }}>
        {run.status === "queued"
          ? "Waiting to start..."
          : run.currentStatusMessage || "Waiting for transcript..."}
      </div>
    </div>
  );
}

export function TaskChatThread({
  issue,
  comments,
  interactions = [],
  agents,
  currentUserId,
  isLoading,
  onAdd,
  onDeleteComment,
  onSaveDescription,
  feedbackVotes,
  onVote,
  threadHeader,
  composerAccessory,
  footer,
  emptyMessage = "No messages yet.",
  onAcceptInteraction,
  onRejectInteraction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
  onSkipInteraction,
  onSubmitInteractionVerdicts,
}: TaskChatThreadProps) {
  const { read, post, mutate } = useTeamsApi();
  const liveRunsResp = read<IssueLiveRun[]>(`/issues/${issue.id}/live-runs`, { shouldRetryOnError: false });
  const activeRunResp = read<IssueLiveRun | null>(`/issues/${issue.id}/active-run`, { shouldRetryOnError: false });

  // Deep-link scroll for `#comment-<id>` — TaskChatBubble sets the anchor id;
  // this makes it actually land there after a pushState navigation (comments
  // are read-then-rendered, so wait for at least one before scrolling).
  useScrollToHashAnchor(comments.length > 0);

  // Upstream picks the tail from liveRuns (newest first), then activeRun.
  const liveRuns = asRuns(liveRunsResp.data);
  const activeRun = activeRunResp.data && typeof activeRunResp.data === "object" ? activeRunResp.data : null;
  const tailRun = liveRuns[0] ?? activeRun ?? null;

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
  const threadItems = buildTeamsThreadItems({ comments, interactions });
  const createdByAgent = issue.createdByAgentId ? agentMap.get(issue.createdByAgentId) : undefined;
  const assigneeName = issue.assigneeAgentId ? agentMap.get(issue.assigneeAgentId)?.name : null;

  // Upstream's pending composer inputs: open by default, reopened by any new one.
  const pending = interactions
    .filter((interaction) => interaction.status === "pending")
    .sort((a, b) => (toMs(String(b.createdAt)) ?? 0) - (toMs(String(a.createdAt)) ?? 0));
  const pendingKey = pending.map((interaction) => interaction.id).join(",");
  const [takeoverOpen, setTakeoverOpen] = useState(true);
  const [selectedPendingId, setSelectedPendingId] = useState<string | null>(null);
  const [seenPendingKey, setSeenPendingKey] = useState(pendingKey);
  if (pendingKey !== seenPendingKey) {
    // Adjust-state-on-prop-change (no effect): a newly arrived input reopens the takeover.
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
            <TaskChatInteractionCard
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

  const cancelRun = tailRun
    ? async () => {
        await post(`/heartbeat-runs/${tailRun.id}/cancel`, {});
        void liveRunsResp.mutate();
        void activeRunResp.mutate();
      }
    : undefined;

  const rows = threadItems.map((entry) => {
    if (entry.kind === "interaction") {
      if (entry.interaction.status === "pending") return null;
      return <TaskChatInteractionCard key={`interaction-${entry.id}`} interaction={entry.interaction} {...interactionHandlers} />;
    }
    const item = commentToTaskChatItem(entry.comment, { agentMap, currentUserId });
    if (!item) return null;
    const feedback =
      item.author === "agent" && !item.unattributed && onVote
        ? {
            activeVote: feedbackVotes?.get(item.id)?.vote ?? null,
            onVote: (vote: FeedbackVoteValue, options?: { reason?: string }) => onVote(item.id, vote, options),
          }
        : null;
    const onDelete =
      item.ownedByViewer && onDeleteComment
        ? async () => {
            try {
              await onDeleteComment(item.id);
              void refetchComments();
            } catch (e) {
              notifyApiError(e, "Couldn't delete the comment.");
            }
          }
        : undefined;
    return (
      <TaskChatBubble
        key={item.renderKey ?? item.id}
        item={item}
        actions={
          item.author === "system" ? undefined : (
            <TaskChatBubbleActions copyText={item.text} feedback={feedback} onDelete={onDelete} />
          )
        }
      />
    );
  }).filter(Boolean);

  return (
    <div className="flex flex-col" data-testid="task-chat-thread">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-1 pb-4">
        {threadHeader ? (
          <div className="flex flex-col gap-4" data-testid="task-chat-thread-header">
            {threadHeader}
          </div>
        ) : null}
        <TaskChatDescriptionBubble
          brief={{
            description: issue.description ?? "",
            author: createdByAgent ? "agent" : "human",
            authorName: createdByAgent?.name,
            createdAt: issue.createdAt,
            onSave: onSaveDescription,
          }}
        />
        {rows.length === 0 && !tailRun ? (
          <div className="px-3 py-6 text-center text-sm" style={{ color: "var(--midnight-muted)" }}>
            {isLoading ? "Loading conversation…" : emptyMessage}
          </div>
        ) : (
          rows
        )}
        {tailRun ? <TaskChatRunTail run={tailRun} /> : null}
      </div>
      <div
        data-testid="task-chat-composer-dock"
        className="sticky bottom-0 z-10 mx-auto flex w-full max-w-3xl flex-col gap-2 px-1 pb-2 pt-1"
        style={{ background: "var(--cream)" }}
      >
        {composerAccessory}
        <TaskChatComposer
          takeover={takeover}
          pendingTakeover={pendingTakeover}
          onAdd={onAdd}
          onStop={cancelRun}
          agents={agents}
          assigneeName={assigneeName}
        />
        {footer}
      </div>
    </div>
  );
}
