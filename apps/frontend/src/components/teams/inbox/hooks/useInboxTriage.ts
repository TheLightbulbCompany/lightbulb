// apps/frontend/src/components/teams/inbox/hooks/useInboxTriage.ts
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Approval, HeartbeatRun } from "@/components/teams/shared/types";

export interface UseInboxTriageResult {
  approvals: Approval[];
  failedRuns: HeartbeatRun[];
  count: number;
  isLoading: boolean;
  isError: boolean;
}

type ApprovalsResponse = { approvals?: Approval[] } | Approval[] | undefined;
type RunsResponse =
  | { runs?: HeartbeatRun[] }
  | { items?: HeartbeatRun[] }
  | HeartbeatRun[]
  | undefined;

function asApprovals(data: ApprovalsResponse): Approval[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.approvals ?? [];
}

function asRuns(data: RunsResponse): HeartbeatRun[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return (data as { runs?: HeartbeatRun[] }).runs ?? (data as { items?: HeartbeatRun[] }).items ?? [];
}

/**
 * Merge the two triage sources for the "Needs attention" band.
 *
 * - Approvals: the real list endpoint `/approvals` (the `/inbox/approvals`
 *   query key is an empty tab stub — do NOT use it). Filtered to pending.
 * - Failed runs: `/inbox/runs` (BFF already forwards status=failed; we
 *   re-filter defensively) minus the locally-acknowledged ids.
 *
 * `acknowledgedRunIds` is owned by the caller (NeedsAttentionBand via
 * useAcknowledgedRuns) so the dismiss state is shared with the rows.
 */
export function useInboxTriage(acknowledgedRunIds: Set<string>): UseInboxTriageResult {
  const { read } = useTeamsApi();
  const approvalsRes = read<ApprovalsResponse>("/approvals");
  const runsRes = read<RunsResponse>("/inbox/runs");

  const approvals = asApprovals(approvalsRes.data).filter((a) => a.status === "pending");
  const failedRuns = asRuns(runsRes.data).filter(
    (r) => r.status === "failed" && !acknowledgedRunIds.has(r.id),
  );

  return {
    approvals,
    failedRuns,
    count: approvals.length + failedRuns.length,
    isLoading: approvalsRes.isLoading || runsRes.isLoading,
    // Either source failing leaves the band unable to vouch for "inbox zero",
    // so surface the failure rather than silently hiding the band.
    isError: Boolean(approvalsRes.error) || Boolean(runsRes.error),
  };
}
