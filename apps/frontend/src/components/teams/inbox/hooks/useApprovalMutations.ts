// apps/frontend/src/components/teams/inbox/hooks/useApprovalMutations.ts
"use client";

import { useCallback } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { captureHandoffCompleted } from "@/lib/analytics";

const APPROVALS_KEY = "/teams/approvals";
const DASHBOARD_KEY = "/teams/dashboard"; // pending-approvals tile

/**
 * Approve / reject / request revision on a pending approval. Bodies are
 * whitelisted by the BFF (extra="forbid"): approve accepts an optional {note},
 * reject requires {reason}, request-revision an optional {decisionNote}. On success we revalidate the
 * band's approvals source + the dashboard tile; the approved/rejected item
 * leaves the pending list on refetch.
 */
export function useApprovalMutations() {
  const { post, mutate, workspaceId } = useTeamsApi();

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
    async (approvalId: string, note?: string) => {
      await post(`/approvals/${approvalId}/approve`, note ? { note } : {});
      captureHandoffCompleted(workspaceId, "approval_answered");
      revalidate();
    },
    [post, revalidate, workspaceId],
  );

  const reject = useCallback(
    async (approvalId: string, reason: string) => {
      await post(`/approvals/${approvalId}/reject`, { reason });
      captureHandoffCompleted(workspaceId, "approval_answered");
      revalidate();
    },
    [post, revalidate, workspaceId],
  );

  const requestRevision = useCallback(
    async (approvalId: string, note?: string) => {
      await post(`/approvals/${approvalId}/request-revision`, note ? { decisionNote: note } : {});
      revalidate();
    },
    [post, revalidate],
  );

  return { approve, reject, requestRevision };
}
