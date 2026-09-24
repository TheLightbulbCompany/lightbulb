// apps/frontend/src/components/teams/issues/task-chat/TaskChatBubbleActions.tsx

// Ported from upstream Paperclip's TaskChatBubbleActions
// (paperclip/ui/src/components/task-chat/TaskChatBubbleActions.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI): the compact copy · 👍 · 👎 cluster on a bubble's
// footer line.
//
// Adaptations:
//   - IssueChatFeedbackButtons -> our OutputFeedbackButtons (local-only votes,
//     no data-sharing prompt — same as the rest of Teams)
//   - adds upstream IssueChatThread's own-comment delete (the classic thread
//     wires `onDeleteComment` for `isCurrentUser && !queued && !pending`;
//     TaskChatThread inherits the prop but never renders it). Upstream has no
//     comment edit route (only queued-comment edit, part B), so there is no edit.

"use client";

import { useState } from "react";
import { Check, Copy, Trash2 } from "lucide-react";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { OutputFeedbackButtons } from "../OutputFeedbackButtons";
import type { FeedbackVoteValue } from "@/components/teams/shared/types";

/** Feedback-vote wiring for an agent bubble, resolved per comment by the host. */
export interface TaskChatBubbleFeedback {
  activeVote: FeedbackVoteValue | null;
  onVote: (vote: FeedbackVoteValue, options?: { reason?: string }) => Promise<void>;
}

const ICON_BUTTON =
  "inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors hover:bg-[var(--cream-deep)]";

export function TaskChatBubbleActions({
  copyText,
  feedback,
  onDelete,
}: {
  copyText: string;
  feedback?: TaskChatBubbleFeedback | null;
  onDelete?: () => Promise<void> | void;
}) {
  const [copied, setCopied] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

  return (
    <div className="flex items-center gap-0.5" data-testid="task-chat-bubble-actions">
      <button
        type="button"
        className={ICON_BUTTON}
        style={{ color: "var(--midnight-muted)" }}
        title="Copy message"
        aria-label="Copy message"
        onClick={() => {
          void navigator.clipboard
            ?.writeText(copyText)
            .then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            })
            .catch(() => {});
        }}
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
      {onDelete ? (
        <button
          type="button"
          className={ICON_BUTTON}
          style={{ color: "var(--midnight-muted)" }}
          title="Delete comment"
          aria-label="Delete comment"
          onClick={() => setDeleteDialogOpen(true)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      ) : null}
      {feedback ? (
        <OutputFeedbackButtons activeVote={feedback.activeVote} onVote={feedback.onVote} />
      ) : null}
      {onDelete ? (
        <EditorialDialog
          open={deleteDialogOpen}
          onOpenChange={setDeleteDialogOpen}
          title="Delete comment?"
          description="This will replace the comment with a deleted-comment marker."
          footer={
            <>
              <EditorialButton variant="secondary" size="sm" onClick={() => setDeleteDialogOpen(false)}>
                Cancel
              </EditorialButton>
              <EditorialButton
                variant="destructive"
                size="sm"
                onClick={() => {
                  setDeleteDialogOpen(false);
                  void onDelete();
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
