// apps/frontend/src/components/teams/issues/ExecutionBlockerNotice.tsx

// Ported from upstream Paperclip's ExecutionBlockerNotice
// (paperclip/ui/src/components/ExecutionBlockerNotice.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Copy, the failed-run match and the
// Inspect run / Continue / Retry conditions are verbatim.
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
//   - router Link to /agents/{id}/runs/{run} -> teamsDetailUrl("run", runId) +
//                                            handleShellLinkClick (in-shell run detail)
//   - --sz-execution-blocker-* spacing vars and the execution-blocker-notice class
//                                         -> plain spacing utilities

"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
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
  const { data, error: runsError } = read<{ runs: IssueRun[] }>(`/issues/${issueId}/runs`);
  const failedRun = data?.runs?.find(run => run.runId === blocker.runId &&
    ["failed", "timed_out"].includes(run.status));
  const modelRejected = failedRun?.errorCode === "native_provider_model_rejected";
  const requiresInspection = blocker.cause === "native_continuation_requires_reconciliation" ||
    blocker.cause === "native_session_cleanup_quarantined";
  const [isPending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const retry = async () => {
    if (!blocker.agentId || !blocker.runId) return;
    setPending(true);
    setError(null);
    try {
      const result = await post<RetryResponse>(`/agents/${blocker.agentId}/wakeup`, {
        source: "on_demand",
        triggerDetail: "manual",
        reason: "retry_failed_run",
        failedRunId: blocker.runId,
      });
      if (result?.id) {
        // Exact retries are idempotent: a repeated click can return a successor
        // that has already stopped. It does not mean a new attempt was queued.
        if (["failed", "timed_out", "cancelled", "interrupted"].includes(result.status ?? "")) {
          throw new Error(
            "The previous retry has already stopped. Refresh the task and retry its latest failed run.",
          );
        }
      } else if (result?.actionId) {
        if (result.status === "failed" || result.status === "cancelled") {
          throw new Error(
            "This retry could not start. Open the task to review its current access and recovery state.",
          );
        }
      } else {
        throw new Error(result?.message ?? "Retry was skipped.");
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

  const runHref = blocker.runId ? teamsDetailUrl("run", blocker.runId) : null;

  return (
    <div role="status" aria-label="Task recovery" className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-muted px-3 py-2 text-ui text-foreground">
      <div className="min-w-0 flex-1 break-words">
        <p>{modelRejected ? "Model unavailable." : "Recovery needed."}{blocker.runError ? ` ${blocker.runError}` : ""}</p>
        {modelRejected && <p>Choose a supported model or clear the task&apos;s model override, then retry.</p>}
        <p>{blocker.nextAction}</p>
        {Boolean(blocker.savedMessageCount) && (
          <p>{blocker.savedMessageCount} saved {blocker.savedMessageCount === 1 ? "message is" : "messages are"} waiting for recovery.</p>
        )}
      </div>
      {blocker.agentId && runHref && (
        <Button variant="outline" size="sm" asChild>
          <a href={runHref} onClick={(e) => handleShellLinkClick(e, runHref)}>Inspect run</a>
        </Button>
      )}
      {(!requiresInspection || blocker.canRetry) && blocker.agentId && blocker.runId &&
        ((blocker.cause === "legacy_execution_requires_reconciliation" && failedRun) || blocker.canContinue || blocker.canRetry) && (
        <Button variant="outline" size="sm" disabled={isPending} onClick={() => void retry()}>
          {isPending ? "Starting…" : blocker.canContinue ? "Continue" : "Retry"}
        </Button>
      )}
      {error && (
        <p role="alert" className="w-full text-destructive">{error.message}</p>
      )}
      {runsError && <p role="alert" className="w-full text-destructive">{runsError.message}</p>}
    </div>
  );
}
