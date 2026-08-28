// apps/frontend/src/components/teams/issues/IssueComments.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx chat tab
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// Thread list + plaintext composer with Cmd+Enter submit. Drops markdown
// editor, @mentions, file upload, action flags, edit/delete. Submit action
// uses <EditorialButton>.
//
// The thread interleaves plain comments with agent-initiated thread
// interactions (suggest_tasks / ask_user_questions / request_confirmation /
// request_checkbox_confirmation) in chronological order via
// buildTeamsThreadItems. When no interaction props are supplied it renders a
// comments-only list (backward compatible).

"use client";

import { useState } from "react";
import { EditorialButton } from "@/components/teams/shell";
import { notifyApiError } from "@/lib/notify";
import { Textarea } from "@/components/ui/textarea";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { IssueThreadInteractionCard } from "./IssueThreadInteractionCard";
import { OutputFeedbackButtons } from "./OutputFeedbackButtons";
import { buildTeamsThreadItems } from "./lib/teams-thread-items";
import type {
  AskUserQuestionsAnswer,
  AskUserQuestionsInteraction,
  CompanyAgent,
  FeedbackVote,
  FeedbackVoteValue,
  IssueComment,
  IssueThreadInteraction,
  RequestCheckboxConfirmationInteraction,
  RequestConfirmationInteraction,
  SuggestTasksInteraction,
} from "@/components/teams/shared/types";

export interface IssueCommentsProps {
  comments: IssueComment[];
  agents: CompanyAgent[];
  isLoading?: boolean;
  onSubmit: (body: string) => Promise<void>;
  /** Agent-initiated thread interactions to interleave with comments. */
  interactions?: IssueThreadInteraction[];
  /** Clerk user id of the viewer — disambiguates "You" in actor labels. */
  currentUserId?: string | null;
  onAcceptInteraction?: (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    selectedClientKeys?: string[],
    selectedOptionIds?: string[],
  ) => Promise<void> | void;
  onRejectInteraction?: (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    reason?: string,
  ) => Promise<void> | void;
  onSubmitInteractionAnswers?: (
    interaction: AskUserQuestionsInteraction,
    answers: AskUserQuestionsAnswer[],
  ) => Promise<void> | void;
  onCancelInteraction?: (
    interaction: AskUserQuestionsInteraction,
  ) => Promise<void> | void;
  /** Active feedback votes keyed by comment id — highlights the thumbs
   * up/down on agent-authored comments. Omit to hide the vote controls. */
  feedbackVotes?: Map<string, FeedbackVote>;
  /** Cast a feedback vote on an agent comment. Omit to hide the controls. */
  onVoteComment?: (
    commentId: string,
    vote: FeedbackVoteValue,
    options?: { reason?: string },
  ) => Promise<void>;
}

/** Agent-authored comments are the reinforcement-signal targets. */
function isAgentComment(c: IssueComment): boolean {
  return Boolean(c.authorAgentId) || c.authorKind === "agent";
}

function authorLabel(
  c: IssueComment,
  agents: CompanyAgent[],
  currentUserId?: string | null,
): string {
  if (c.authorAgentId) return agents.find((a) => a.id === c.authorAgentId)?.name ?? "Agent";
  if (c.authorKind === "agent") return "Agent";
  if (c.authorClerkUserId && currentUserId && c.authorClerkUserId === currentUserId) return "You";
  if (c.authorName) return c.authorName;
  if (c.authorUserId || c.authorKind === "user") return "User";
  return "Comment";
}

function CommentRow({
  comment,
  agents,
  currentUserId,
  feedbackVotes,
  onVoteComment,
}: {
  comment: IssueComment;
  agents: CompanyAgent[];
  currentUserId?: string | null;
  feedbackVotes?: Map<string, FeedbackVote>;
  onVoteComment?: (
    commentId: string,
    vote: FeedbackVoteValue,
    options?: { reason?: string },
  ) => Promise<void>;
}) {
  // Feedback votes attach only to agent output, and only when the caller wired
  // the vote handler (the reinforcement signal back into agents).
  const showFeedback = Boolean(onVoteComment) && isAgentComment(comment);
  return (
    <li
      className="flex flex-col gap-1 pl-3"
      style={{ borderLeft: "2px solid var(--edge)" }}
    >
      <div
        className="flex items-baseline gap-2 text-xs"
        style={{ color: "var(--midnight-muted)" }}
      >
        <span>{authorLabel(comment, agents, currentUserId)}</span>
        <span aria-hidden="true">·</span>
        <time dateTime={comment.createdAt} title={new Date(comment.createdAt).toLocaleString()}>
          {timeAgo(comment.createdAt)}
        </time>
      </div>
      <div className="text-sm" style={{ color: "var(--midnight-soft)" }}>
        <MarkdownView>{comment.body}</MarkdownView>
      </div>
      {showFeedback ? (
        <OutputFeedbackButtons
          activeVote={feedbackVotes?.get(comment.id)?.vote ?? null}
          onVote={(vote, options) => onVoteComment!(comment.id, vote, options)}
        />
      ) : null}
    </li>
  );
}

export function IssueComments({
  comments,
  agents,
  isLoading,
  onSubmit,
  interactions,
  currentUserId,
  onAcceptInteraction,
  onRejectInteraction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
  feedbackVotes,
  onVoteComment,
}: IssueCommentsProps) {
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    const trimmed = body.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(trimmed);
      setBody("");
    } catch (e) {
      notifyApiError(e, "Failed to post comment");
    } finally {
      setSubmitting(false);
    }
  };

  const threadInteractions = interactions ?? [];
  const agentMap = new Map(agents.map((a) => [a.id, a] as const));
  const items = buildTeamsThreadItems({ comments, interactions: threadInteractions });
  const isEmpty = items.length === 0;

  return (
    <div className="flex flex-col gap-4">
      {/* Thread */}
      {isLoading && isEmpty ? (
        <p
          className="text-sm italic"
          style={{
            fontFamily: "var(--font-serif), serif",
            color: "var(--midnight-muted)",
          }}
        >
          Loading comments…
        </p>
      ) : isEmpty ? (
        <p
          className="text-sm italic"
          style={{
            fontFamily: "var(--font-serif), serif",
            color: "var(--midnight-muted)",
          }}
        >
          No comments yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) =>
            item.kind === "comment" ? (
              <CommentRow
                key={`comment-${item.id}`}
                comment={item.comment}
                agents={agents}
                currentUserId={currentUserId}
                feedbackVotes={feedbackVotes}
                onVoteComment={onVoteComment}
              />
            ) : (
              <li key={`interaction-${item.id}`}>
                <IssueThreadInteractionCard
                  interaction={item.interaction}
                  agentMap={agentMap}
                  currentUserId={currentUserId}
                  onAcceptInteraction={onAcceptInteraction}
                  onRejectInteraction={onRejectInteraction}
                  onSubmitInteractionAnswers={onSubmitInteractionAnswers}
                  onCancelInteraction={onCancelInteraction}
                />
              </li>
            ),
          )}
        </ul>
      )}

      {/* Composer */}
      <div
        className="flex flex-col gap-2 pt-4"
        style={{ borderTop: "1px solid var(--edge)" }}
      >
        <Textarea
          rows={3}
          placeholder="Add a comment..."
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
          disabled={submitting}
          style={{
            background: "var(--cream-deep)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        />
        <div className="flex justify-end gap-2">
          <EditorialButton
            type="button"
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={!body.trim() || submitting}
          >
            {submitting ? "Posting…" : "Comment"}
          </EditorialButton>
        </div>
      </div>
    </div>
  );
}
