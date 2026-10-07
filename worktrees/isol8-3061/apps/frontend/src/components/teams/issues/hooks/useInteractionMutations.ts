// apps/frontend/src/components/teams/issues/hooks/useInteractionMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * Resolve an issue thread interaction inline (mirrors useApprovalMutations).
 *
 * The four verbs map to the BFF's interaction-resolution routes:
 *   accept   → POST /issues/{id}/interactions/{iid}/accept
 *   reject   → POST /issues/{id}/interactions/{iid}/reject
 *   respond  → POST /issues/{id}/interactions/{iid}/respond  (ask_user_questions)
 *   cancel   → POST /issues/{id}/interactions/{iid}/cancel
 *
 * KEY GOTCHA: `post()` takes a path WITHOUT the `/teams` prefix (useTeamsApi
 * prepends it), but `mutate()` keys are the FULL `/teams/...` SWR cache keys.
 * Resolving an interaction can append a comment to the thread (e.g. a decline
 * reason, an answer summary), so we revalidate BOTH the interactions list and
 * the comments list for the issue.
 */
export function useInteractionMutations() {
  const { post } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(
    (issueId: string) => {
      mutate(`/teams/issues/${issueId}/interactions`);
      mutate(`/teams/issues/${issueId}/comments`);
    },
    [mutate],
  );

  const accept = useCallback(
    async (issueId: string, interactionId: string, body?: unknown) => {
      await post(
        `/issues/${issueId}/interactions/${interactionId}/accept`,
        body ?? {},
      );
      revalidate(issueId);
    },
    [post, revalidate],
  );

  const reject = useCallback(
    async (issueId: string, interactionId: string, reason?: string) => {
      await post(`/issues/${issueId}/interactions/${interactionId}/reject`, {
        reason,
      });
      revalidate(issueId);
    },
    [post, revalidate],
  );

  const respond = useCallback(
    async (issueId: string, interactionId: string, body: unknown) => {
      await post(
        `/issues/${issueId}/interactions/${interactionId}/respond`,
        body,
      );
      revalidate(issueId);
    },
    [post, revalidate],
  );

  const cancel = useCallback(
    async (issueId: string, interactionId: string, reason?: string) => {
      await post(`/issues/${issueId}/interactions/${interactionId}/cancel`, {
        reason,
      });
      revalidate(issueId);
    },
    [post, revalidate],
  );

  return { accept, reject, respond, cancel };
}
