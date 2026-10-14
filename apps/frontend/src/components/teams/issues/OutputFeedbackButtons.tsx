// apps/frontend/src/components/teams/issues/OutputFeedbackButtons.tsx

// Ported + adapted from upstream Paperclip's
// ui/src/components/OutputFeedbackButtons.tsx (MIT, (c) 2025 Paperclip AI).
// Thumbs up/down on an agent output — the reinforcement signal fed back into
// agents. Adapted to the isol8 editorial kit (EditorialButton + Textarea +
// lucide icons). Isol8 casts votes LOCAL-ONLY: there is no Paperclip-Labs
// data-sharing surface here, so the upstream sharing-preference Dialog and the
// allowSharing prompt are dropped. Keeps the up/down buttons + the inline
// downvote-reason note (the "what could have been better?" collector).

"use client";

import { useEffect, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { Textarea } from "@/components/ui/textarea";
import type { FeedbackVoteValue } from "@/components/teams/shared/types";

export interface OutputFeedbackButtonsProps {
  /** The user's current vote on this output, if any. */
  activeVote?: FeedbackVoteValue | null;
  disabled?: boolean;
  /** Cast a vote. A rejection rolls back the optimistic highlight. */
  onVote: (vote: FeedbackVoteValue, options?: { reason?: string }) => Promise<void>;
}

const ACTIVE_UP_STYLE = {
  borderColor: "var(--ok)",
  background: "var(--cream)",
} as const;

const ACTIVE_DOWN_STYLE = {
  borderColor: "var(--warn)",
  background: "var(--cream)",
} as const;

export function OutputFeedbackButtons({
  activeVote,
  disabled = false,
  onVote,
}: OutputFeedbackButtonsProps) {
  const [isSaving, setIsSaving] = useState(false);
  const [collectingDownvoteReason, setCollectingDownvoteReason] = useState(false);
  const [downvoteReason, setDownvoteReason] = useState("");
  const [optimisticVote, setOptimisticVote] = useState<FeedbackVoteValue | null>(null);
  const visibleVote = optimisticVote ?? activeVote ?? null;

  // Clear the optimistic highlight once the server-confirmed vote matches it.
  useEffect(() => {
    if (optimisticVote && activeVote === optimisticVote) {
      setOptimisticVote(null);
    }
  }, [activeVote, optimisticVote]);

  async function submitVote(
    vote: FeedbackVoteValue,
    options?: { reason?: string },
    keepReasonOpen?: boolean,
  ) {
    setIsSaving(true);
    try {
      await onVote(vote, options);
      if (!keepReasonOpen) {
        setCollectingDownvoteReason(false);
        setDownvoteReason("");
      }
    } catch {
      // Roll back the optimistic highlight; onVote's caller surfaces the error.
      setOptimisticVote(null);
    } finally {
      setIsSaving(false);
    }
  }

  function handleVote(vote: FeedbackVoteValue) {
    setOptimisticVote(vote);
    if (vote === "down") {
      // Register the down vote immediately, then keep the reason note open so
      // the user can optionally add a note (mirrors upstream).
      setDownvoteReason("");
      setCollectingDownvoteReason(true);
      void submitVote("down", undefined, true);
      return;
    }
    void submitVote("up");
  }

  return (
    <div className="mt-2 flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <EditorialButton
          type="button"
          size="sm"
          variant="secondary"
          disabled={disabled || isSaving}
          aria-pressed={visibleVote === "up"}
          style={visibleVote === "up" ? ACTIVE_UP_STYLE : undefined}
          onClick={() => handleVote("up")}
        >
          <ThumbsUp size={16} className="mr-1.5 inline" aria-hidden="true" />
          Helpful
        </EditorialButton>
        <EditorialButton
          type="button"
          size="sm"
          variant="secondary"
          disabled={disabled || isSaving}
          aria-pressed={visibleVote === "down"}
          style={visibleVote === "down" ? ACTIVE_DOWN_STYLE : undefined}
          onClick={() => handleVote("down")}
        >
          <ThumbsDown size={16} className="mr-1.5 inline" aria-hidden="true" />
          Needs work
        </EditorialButton>
      </div>

      {collectingDownvoteReason ? (
        <div
          className="rounded-md p-3"
          style={{ border: "1px solid var(--edge)", background: "var(--cream-deep)" }}
        >
          <div className="mb-2 text-ui font-medium" style={{ color: "var(--midnight)" }}>
            What could have been better?
          </div>
          <Textarea
            rows={2}
            value={downvoteReason}
            onChange={(e) => setDownvoteReason(e.target.value)}
            placeholder="Add a short note (optional)"
            disabled={disabled || isSaving}
            style={{
              background: "var(--cream)",
              border: "1px solid var(--edge)",
              color: "var(--midnight)",
            }}
          />
          <div className="mt-3 flex items-center justify-end gap-2">
            <EditorialButton
              type="button"
              size="sm"
              variant="ghost"
              disabled={disabled || isSaving}
              onClick={() => {
                setCollectingDownvoteReason(false);
                setDownvoteReason("");
              }}
            >
              Dismiss
            </EditorialButton>
            <EditorialButton
              type="button"
              size="sm"
              variant="primary"
              disabled={disabled || isSaving || !downvoteReason.trim()}
              onClick={() => void submitVote("down", { reason: downvoteReason.trim() })}
            >
              {isSaving ? "Saving…" : "Save note"}
            </EditorialButton>
          </div>
        </div>
      ) : null}
    </div>
  );
}
