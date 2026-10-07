// apps/frontend/src/components/teams/issues/hooks/useIssueFeedback.ts

// Feedback votes (thumbs up/down on agent output) for the issue-detail thread.
// Reads the caller's votes for the issue and exposes a castVote mutation.
// Backed by the Teams BFF /issues/{id}/feedback-votes proxy (which forwards to
// Paperclip's feedback-votes pipeline — the only reinforcement signal into
// agents).

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { FeedbackVote, FeedbackVoteValue } from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";

type VotesResponse = { votes: FeedbackVote[] } | FeedbackVote[];

function normalizeVotes(data: VotesResponse | undefined): FeedbackVote[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.votes ?? [];
}

export interface UseIssueFeedbackResult {
  /** Active vote per output, keyed by targetId (e.g. the comment id). */
  votesByTarget: Map<string, FeedbackVote>;
  /** Cast (upsert) a vote on an agent output. */
  castVote: (
    targetId: string,
    vote: FeedbackVoteValue,
    options?: { targetType?: string; reason?: string },
  ) => Promise<void>;
}

export function useIssueFeedback(issueId: string): UseIssueFeedbackResult {
  const { read, post } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const votesKey = teamsQueryKeys.issues.feedbackVotes(issueId);
  const { data } = read<VotesResponse>(votesKey);
  const votes = normalizeVotes(data);
  const votesByTarget = new Map(votes.map((v) => [v.targetId, v] as const));

  const castVote = useCallback(
    async (
      targetId: string,
      vote: FeedbackVoteValue,
      options?: { targetType?: string; reason?: string },
    ): Promise<void> => {
      const body: Record<string, unknown> = {
        targetType: options?.targetType ?? "issue_comment",
        targetId,
        vote,
      };
      if (options?.reason) body.reason = options.reason;
      await post(`/issues/${issueId}/feedback-votes`, body);
      // Revalidate the votes list so the active highlight reflects the server.
      await mutate(`${SWR_PREFIX}${votesKey}`);
    },
    [post, mutate, issueId, votesKey],
  );

  return { votesByTarget, castVote };
}
