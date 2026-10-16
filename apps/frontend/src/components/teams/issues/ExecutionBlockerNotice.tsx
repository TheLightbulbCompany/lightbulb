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
//
// Added here (owner 2026-10-16; stock has no UI for it): a reconciliation-class
// stop (an operator Stop on our hermes_gateway adapter lands as
// legacy_execution_requires_reconciliation) refuses a generic Retry, so instead
// of Retry the notice asks what the stopped run got done and resolves the
// recovery action with upstream's executionReconciliation body. Paperclip then
// starts a new run on the same task (deliverReconciledExecutions).

"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { ExecutionBlocker, IssueRun } from "@/components/teams/shared/types";
import { requiresExecutionReconciliation } from "./lib/recovery";

/** The person's answer -> upstream's actionOutcome. */
const OUTCOMES = [
  { value: "not_performed", label: "Nothing done" },
  { value: "mixed", label: "Partly" },
  { value: "completed", label: "Done" },
] as const;
type ActionOutcome = (typeof OUTCOMES)[number]["value"];

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

  if (requiresExecutionReconciliation(blocker.cause) && blocker.recoveryActionId && blocker.runId &&
    !blocker.canRetry && !blocker.canContinue) {
    return <ResumeStoppedRun issueId={issueId} blocker={blocker} onResumed={onRetried} />;
  }

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

function ResumeStoppedRun({ issueId, blocker, onResumed }: {
  issueId: string;
  blocker: ExecutionBlocker;
  onResumed?: () => void;
}) {
  const { post, mutate } = useTeamsApi();
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  const [note, setNote] = useState("");
  const [isPending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const resume = async () => {
    const choice = OUTCOMES.find(o => o.value === outcome);
    if (!choice) return;
    setPending(true);
    setError(null);
    // Upstream wants >= 20 chars of evidence; the choice itself always gives that.
    const answer = `A person answered: ${choice.label.toLowerCase()}.`;
    try {
      await post(`/issues/${issueId}/recovery-actions/resolve`, {
        actionId: blocker.recoveryActionId,
        outcome: "restored",
        sourceIssueStatus: "todo",
        executionReconciliation: {
          runId: blocker.runId,
          providerStopped: true,
          actionOutcome: choice.value,
          outcomeEvidence: note.trim() ? `${answer} ${note.trim()}` : answer,
        },
      });
      onResumed?.();
      for (const key of ["", "/runs", "/live-runs", "/active-run", "/recovery-actions"]) {
        void mutate(`/teams/issues/${issueId}${key}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err : new Error("Couldn't resume the task."));
    } finally {
      setPending(false);
    }
  };

  return (
    <div role="status" aria-label="Task recovery" className="mb-3 flex flex-col gap-2 rounded-md border border-border bg-muted px-3 py-2 text-ui text-foreground">
      <p>This run was stopped. Did it finish what it was doing?</p>
      <div className="flex flex-wrap gap-2">
        {OUTCOMES.map(o => (
          <Button
            key={o.value}
            variant={outcome === o.value ? "default" : "outline"}
            size="sm"
            aria-pressed={outcome === o.value}
            disabled={isPending}
            onClick={() => setOutcome(o.value)}
          >
            {o.label}
          </Button>
        ))}
      </div>
      <Textarea
        aria-label="Note for the agent (optional)"
        placeholder="Note for the agent (optional)"
        maxLength={11000}
        value={note}
        disabled={isPending}
        onChange={e => setNote(e.target.value)}
        className="min-h-[60px]"
      />
      <div>
        <Button size="sm" disabled={!outcome || isPending} onClick={() => void resume()}>
          {isPending ? "Resuming…" : "Resume"}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-destructive">{error.message}</p>
      )}
    </div>
  );
}
