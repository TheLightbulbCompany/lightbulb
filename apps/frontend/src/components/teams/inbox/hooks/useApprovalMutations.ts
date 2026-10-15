// apps/frontend/src/components/teams/inbox/hooks/useApprovalMutations.ts
"use client";

import { useCallback } from "react";
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
  const { post, mutate } = useTeamsApi();

  const revalidate = useCallback(() => {
    mutate(APPROVALS_KEY);
    mutate(DASHBOARD_KEY);
    // Also refresh any per-issue linked-approvals lists (sub-project D2) so a
    // reused ApprovalRow on an issue-detail page updates after approve/reject,
    // and the single-approval reads (`/teams/approvals/<id>`) behind the card in
    // a chat message and the detail panel, which otherwise kept showing Pending.
    mutate((key) => typeof key === "string"
      && (/^\/teams\/issues\/.+\/approvals$/.test(key) || key.startsWith(`${APPROVALS_KEY}/`)));
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

  /** Ask the agent to change it: the approval stays open as `revision_requested`. */
  const requestRevision = useCallback(
    async (approvalId: string) => {
      await post(`/approvals/${approvalId}/request-revision`, {});
      revalidate();
    },
    [post, revalidate],
  );

  return { approve, reject, requestRevision };
}
