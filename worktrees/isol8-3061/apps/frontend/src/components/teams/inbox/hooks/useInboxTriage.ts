// apps/frontend/src/components/teams/inbox/hooks/useInboxTriage.ts
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { HeartbeatRun } from "@/components/teams/shared/types";

export interface UseInboxTriageResult {
  failedRuns: HeartbeatRun[];
  count: number;
  isLoading: boolean;
  isError: boolean;
}

type RunsResponse =
  | { runs?: HeartbeatRun[] }
  | { items?: HeartbeatRun[] }
  | HeartbeatRun[]
  | undefined;

function asRuns(data: RunsResponse): HeartbeatRun[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return (data as { runs?: HeartbeatRun[] }).runs ?? (data as { items?: HeartbeatRun[] }).items ?? [];
}

/**
 * Failed agent runs for the "Agent failures" band.
 *
 * Source: `/inbox/runs` (the BFF already forwards status=failed; we re-filter
 * defensively) minus the locally-acknowledged ids. `acknowledgedRunIds` is
 * owned by the caller (`InboxNeedsYou` via useAcknowledgedRuns) so the
 * dismiss state is shared with the rows.
 *
 * APPROVALS DELIBERATELY LEFT OUT. They used to be read here too, which meant
 * an approval was both a band row and — once the unified feed shipped — a
 * "Needs you" row, under two labels with two different counts. A decision now
 * has exactly one home (the "Needs you" tab, via `useAttentionFeed`) and this
 * band has exactly one meaning: something an agent did went wrong. Failures
 * are FYI and dismissible; decisions block work until answered. Merging them
 * is what made "needs attention" impossible to act on.
 */
export function useInboxTriage(acknowledgedRunIds: Set<string>): UseInboxTriageResult {
  const { read } = useTeamsApi();
  const runsRes = read<RunsResponse>("/inbox/runs");

  const failedRuns = asRuns(runsRes.data).filter(
    (r) => r.status === "failed" && !acknowledgedRunIds.has(r.id),
  );

  return {
    failedRuns,
    count: failedRuns.length,
    isLoading: runsRes.isLoading,
    // A failed read leaves the band unable to vouch for "nothing broke", so
    // surface the failure rather than silently hiding the band.
    isError: Boolean(runsRes.error),
  };
}
