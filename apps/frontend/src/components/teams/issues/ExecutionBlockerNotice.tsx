// apps/frontend/src/components/teams/issues/ExecutionBlockerNotice.tsx

// Ported from upstream Paperclip's ExecutionBlockerNotice
// (paperclip/ui/src/components/ExecutionBlockerNotice.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Copy, the failed-run match and the Retry states
// are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared ExecutionBlocker -> components/teams/shared/types
//   - react-query runs query              -> useTeamsApi.read on /issues/{id}/runs
//                                            (the run ledger's key, so SWR dedupes)
//   - agentsApi.retryFailedRun            -> the same wakeup body + outcome handling
//                                            (ui/src/api/agents.ts) over useTeamsApi.post
//                                            to the BFF's /agents/{id}/wakeup
//   - queryClient invalidations           -> useTeamsApi.mutate on the same four keys
//   - companyId prop                      -> dropped (the BFF pins the workspace)
//   - --sz-execution-blocker-* spacing vars and the execution-blocker-notice class
//                                         -> plain spacing utilities

"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { ExecutionBlocker, IssueRun } from "@/components/teams/shared/types";

type RetryResponse = {
  id?: string;
  actionId?: string;
  status?: string;
  message?: string | null;
};

export function ExecutionBlockerNotice({ issueId, blocker, onRetried }: {
  issueId: string;
  blocker: ExecutionBlocker;
  onRetried?: () => void;
}) {
  const { read, post, mutate } = useTeamsApi();
  const { data } = read<{ runs: IssueRun[] }>(`/issues/${issueId}/runs`);
  const failedRun = data?.runs?.find(run => run.runId === blocker.runId &&
    ["failed", "timed_out"].includes(run.status));
  const [isPending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const retry = async () => {
    if (!failedRun?.agentId) return;
    setPending(true);
    setError(null);
    try {
      const result = await post<RetryResponse>(`/agents/${failedRun.agentId}/wakeup`, {
        source: "on_demand",
        triggerDetail: "manual",
        reason: "retry_failed_run",
        failedRunId: failedRun.runId,
      });
      if (!result?.id) {
        if (result?.actionId) {
          if (result.status === "failed" || result.status === "cancelled") {
            throw new Error(
              "This retry could not start. Open the task to review its current access and recovery state.",
            );
          }
        } else {
          throw new Error(result?.message ?? "Retry was skipped.");
        }
      }
      onRetried?.();
      for (const key of ["", "/runs", "/live-runs", "/active-run"]) {
        void mutate(`/teams/issues/${issueId}${key}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err : new Error("Retry failed."));
    } finally {
      setPending(false);
    }
  };

  return (
    <div role="status" aria-label="Task recovery" className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-muted px-3 py-2 text-ui text-foreground">
      <span>{blocker.cause === "legacy_execution_requires_reconciliation" ? "Automatic recovery of this task stopped." : blocker.nextAction}</span>
      {failedRun && (
        <Button variant="outline" size="sm" disabled={isPending} onClick={() => void retry()}>
          {isPending ? "Retrying…" : "Retry"}
        </Button>
      )}
      {error && (
        <p role="alert" className="w-full text-destructive">{error.message}</p>
      )}
    </div>
  );
}
