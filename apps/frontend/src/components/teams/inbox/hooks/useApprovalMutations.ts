// apps/frontend/src/components/teams/inbox/hooks/useApprovalMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const APPROVALS_KEY = "/teams/approvals";
const DASHBOARD_KEY = "/teams/dashboard"; // pending-approvals tile

/**
 * Approve / reject a pending approval. Both bodies are whitelisted by the BFF
 * (extra="forbid"): approve accepts an optional {note} (not surfaced in v1 —
 * one-click approve), reject requires {reason}. On success we revalidate the
 * band's approvals source + the dashboard tile; the approved/rejected item
 * leaves the pending list on refetch.
 */
export function useApprovalMutations() {
  const { post } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(() => {
    mutate(APPROVALS_KEY);
    mutate(DASHBOARD_KEY);
  }, [mutate]);

  const approve = useCallback(
    async (approvalId: string) => {
      await post(`/approvals/${approvalId}/approve`, {});
      revalidate();
    },
    [post, revalidate],
  );

  const reject = useCallback(
    async (approvalId: string, reason: string) => {
      await post(`/approvals/${approvalId}/reject`, { reason });
      revalidate();
    },
    [post, revalidate],
  );

  return { approve, reject };
}
