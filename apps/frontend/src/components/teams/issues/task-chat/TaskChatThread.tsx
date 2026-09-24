// apps/frontend/src/components/teams/issues/task-chat/TaskChatThread.tsx

// Ported from upstream Paperclip's TaskChatThread
// (paperclip/ui/src/components/TaskChatThread.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI) — stock 916's default task page, where "the chat
// IS the page": the thread header, the description as the first bubble, the
// author-typed comment bubbles with interaction cards interleaved, the live run
// tail (TaskChatLiveRunPill + TaskChatLiveTail, the legacy-adapter branch that
// hermes_gateway takes), and a sticky composer dock with `composerAccessory`
// above it and `footer` below.
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
// active-run) and run queued/status events refresh the per-issue run lists —
// while the tail's log is polled every 3s only while its run is live.
//
// Deferred to part B: queued comments (edit/reorder/steer/interrupt/discard),
// interaction skip + item verdicts (TaskChatInteractionCard/QuestionForm — the
// classic IssueThreadInteractionCard stays mounted in the thread meanwhile),
// runtime-request resolve, the native paperclip_runner turn, attachments and
// uploads, user mentions, the activity route + IssueWriteDenialNotice.

"use client";

import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { IssueThreadInteractionCard } from "../IssueThreadInteractionCard";
import type { IssueThreadInteractionCardProps } from "../IssueThreadInteractionCard";
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
import { TaskChatLiveRunPill, TaskChatLiveTail } from "./TaskChatLiveTail";
import { transcriptToTaskChatItems } from "./transcript-adapter";
import { useLiveRunTranscript } from "./useLiveRunTranscript";

type InteractionHandlers = Pick<
  IssueThreadInteractionCardProps,
  "onAcceptInteraction" | "onRejectInteraction" | "onSubmitInteractionAnswers" | "onCancelInteraction"
>;

export interface TaskChatThreadProps extends InteractionHandlers {
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

function asRuns(data: unknown): IssueLiveRun[] {
  return Array.isArray(data) ? (data as IssueLiveRun[]) : [];
}

/** The live run tail: pill + streamed transcript for the newest live run. */
function TaskChatRunTail({ run }: { run: IssueLiveRun }) {
  const active = run.status === "queued" || run.status === "running";
  const entries = useLiveRunTranscript(run.id, { active, logBytes: run.logBytes });
  const items = useMemo(
    () =>
      transcriptToTaskChatItems(entries, {
        runId: run.id,
        agentName: run.agentName ?? undefined,
        running: active,
      }),
    [entries, run.id, run.agentName, active],
  );
  return (
    <div data-testid="task-chat-live-transcript">
      <TaskChatLiveRunPill
        status={run.status}
        startedAtMs={toMs(run.startedAt) ?? toMs(run.createdAt)}
        finishedAtMs={toMs(run.finishedAt)}
      />
      <TaskChatLiveTail
        items={items}
        emptyMessage={
          run.status === "queued"
            ? "Waiting to start..."
            : run.currentStatusMessage || "Waiting for transcript..."
        }
      />
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
}: TaskChatThreadProps) {
  const { read, post } = useTeamsApi();
  const liveRunsResp = read<IssueLiveRun[]>(`/issues/${issue.id}/live-runs`, { shouldRetryOnError: false });
  const activeRunResp = read<IssueLiveRun | null>(`/issues/${issue.id}/active-run`, { shouldRetryOnError: false });
  const commentsResp = read(`/issues/${issue.id}/comments`);

  // Upstream picks the tail from liveRuns (newest first), then activeRun.
  const liveRuns = asRuns(liveRunsResp.data);
  const activeRun = activeRunResp.data && typeof activeRunResp.data === "object" ? activeRunResp.data : null;
  const tailRun = liveRuns[0] ?? activeRun ?? null;

  // The run's reply lands as a comment when it settles; refetch once the tail clears.
  const hadTail = useRef(false);
  const refetchComments = commentsResp.mutate;
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

  const cancelRun = tailRun
    ? async () => {
        await post(`/heartbeat-runs/${tailRun.id}/cancel`, {});
        void liveRunsResp.mutate();
        void activeRunResp.mutate();
      }
    : undefined;

  const rows = threadItems.map((entry) => {
    if (entry.kind === "interaction") {
      return (
        <div key={`interaction-${entry.id}`}>
          <IssueThreadInteractionCard
            interaction={entry.interaction}
            agentMap={agentMap}
            currentUserId={currentUserId}
            onAcceptInteraction={onAcceptInteraction}
            onRejectInteraction={onRejectInteraction}
            onSubmitInteractionAnswers={onSubmitInteractionAnswers}
            onCancelInteraction={onCancelInteraction}
          />
        </div>
      );
    }
    const item = commentToTaskChatItem(entry.comment, { agentMap, currentUserId });
    if (!item) return null;
    const feedback =
      item.author === "agent" && onVote
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
  });

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
          onAdd={async (body) => {
            await onAdd(body);
            void refetchComments();
            void liveRunsResp.mutate();
          }}
          onStop={cancelRun}
          agents={agents}
          assigneeName={assigneeName}
        />
        {footer}
      </div>
    </div>
  );
}
