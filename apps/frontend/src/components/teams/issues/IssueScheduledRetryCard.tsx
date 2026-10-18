// apps/frontend/src/components/teams/issues/IssueScheduledRetryCard.tsx

// Ported from upstream Paperclip's IssueScheduledRetryCard
// (paperclip/ui/src/components/IssueScheduledRetryCard.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Markup, copy and the Retry-now states are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types      -> components/teams/shared/types
//   - @/lib/issue-monitor            -> ./lib/issue-monitor
//   - formatRetryReason (lib/runRetryState.ts) and formatDateTime (lib/utils.ts)
//                                    -> ./lib/runRetryState (shared with IssueRunLedger)
//   - hooks/useRetryNowMutation (react-query + ToastContext)
//                                    -> useRetryNowMutation below: the same outcome
//                                       handling over useTeamsApi.post and lib/notify,
//                                       posting to the BFF's issue-scoped
//                                       /teams/issues/{id}/scheduled-retry/retry-now
//   - react-router Link to /agents/:id/runs/:runId
//                                    -> in-shell anchor to the run detail panel
//   - blue/rose palette              -> brand state tokens info/err (.dark re-points
//                                       them, so the dark: variants collapse)
//   - sm:max-w-(--sz-12rem)          -> sm:max-w-48

"use client";

import { useCallback, useState } from "react";
import { Clock, RotateCcw, AlertCircle, Loader2, CheckCircle2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { notifyApiError, notifyError, notifySuccess } from "@/lib/notify";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type {
  IssueRetryNowOutcome,
  IssueRetryNowResponse,
  IssueScheduledRetry,
} from "@/components/teams/shared/types";
import { formatMonitorOffset } from "./lib/issue-monitor";
import { formatDateTime, formatRetryReason } from "./lib/runRetryState";

// --- ui/src/hooks/useRetryNowMutation.ts ------------------------------------

export type RetryNowError = {
  message: string;
  outcomeMessage: string | null;
  status: number | null;
};

function readErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "The request failed. Try again in a moment.";
}

export const RETRY_NOW_OUTCOME_HEADLINE: Record<IssueRetryNowOutcome, string> = {
  promoted: "Retry promoted",
  already_promoted: "Retry already running",
  no_scheduled_retry: "No scheduled retry",
  gate_suppressed: "Couldn't retry now",
};

export function useRetryNowMutation(issueId: string | null | undefined) {
  const { post, mutate: mutateKey } = useTeamsApi();
  const [isPending, setPending] = useState(false);
  const [data, setData] = useState<IssueRetryNowResponse | null>(null);
  const [error, setError] = useState<unknown>(null);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
  }, []);

  const mutate = useCallback(() => {
    if (!issueId) return;
    setPending(true);
    setError(null);
    post<IssueRetryNowResponse>(`/issues/${issueId}/scheduled-retry/retry-now`, {})
      .then((response) => {
        setData(response);
        void mutateKey(`/teams${teamsQueryKeys.issues.detail(issueId)}`);
        void mutateKey(`/teams/issues/${issueId}/runs`);
        if (response.outcome === "promoted") {
          notifySuccess(`${RETRY_NOW_OUTCOME_HEADLINE.promoted}: ${response.message}`);
        } else if (response.outcome === "gate_suppressed") {
          notifyError(`${RETRY_NOW_OUTCOME_HEADLINE.gate_suppressed}: ${response.message}`);
        }
      })
      .catch((err: unknown) => {
        setData(null);
        setError(err);
        notifyApiError(err, "Couldn't retry now");
      })
      .finally(() => setPending(false));
  }, [issueId, post, mutateKey]);

  const lastError: RetryNowError | null = error
    ? {
        message: readErrorMessage(error),
        outcomeMessage: null,
        status: (error as { status?: number }).status ?? null,
      }
    : data?.outcome === "gate_suppressed"
      ? { message: data.message, outcomeMessage: data.message, status: null }
      : null;

  return {
    mutate,
    reset,
    data,
    isPending,
    isError: error !== null,
    isSuccess: data !== null,
    lastError,
  };
}

// --- ui/src/components/IssueScheduledRetryCard.tsx --------------------------

/** Upstream's react-router Link, as an in-shell anchor (new-tab still works). */
function Link({ to, className, children }: { to: string; className?: string; children: React.ReactNode }) {
  return (
    <a href={to} className={className} onClick={(e) => handleShellLinkClick(e, to)}>
      {children}
    </a>
  );
}

const MAX_TURN_CONTINUATION = "max_turns_continuation";

function isContinuationReason(reason: string | null | undefined) {
  return reason === MAX_TURN_CONTINUATION;
}

function shortRunId(runId: string | null | undefined) {
  return typeof runId === "string" && runId.length >= 8 ? runId.slice(0, 8) : runId ?? "";
}

interface IssueScheduledRetryCardProps {
  issueId: string | null | undefined;
  scheduledRetry: IssueScheduledRetry | null | undefined;
}

// Upstream's workspace_busy branch renders an InlineBanner; this is its info-tone
// markup inlined (IssueMonitorBanner owns the full InlineBanner port).
function WorkspaceBusyBanner() {
  return (
    <div
      role="note"
      className="mb-3 flex flex-col gap-2 rounded-lg border border-info/40 bg-info/10 p-4 text-info sm:flex-row sm:items-start sm:justify-between"
    >
      <div className="flex items-start gap-2.5">
        <Clock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="space-y-1 text-ui">
          <p className="font-medium leading-tight">Waiting for workspace</p>
          <div className="leading-snug opacity-90">
            Another task is using this workspace. Work starts automatically when it is available.
          </div>
        </div>
      </div>
    </div>
  );
}

export function IssueScheduledRetryCard({
  issueId,
  scheduledRetry,
}: IssueScheduledRetryCardProps) {
  const retryNow = useRetryNowMutation(issueId);

  if (!scheduledRetry || !issueId) return null;
  if (scheduledRetry.status !== "scheduled_retry") return null;

  if (scheduledRetry.scheduledRetryReason === "workspace_busy") {
    return <WorkspaceBusyBanner />;
  }

  const continuation = isContinuationReason(scheduledRetry.scheduledRetryReason);
  const dueAtIso = scheduledRetry.scheduledRetryAt
    ? new Date(scheduledRetry.scheduledRetryAt).toISOString()
    : null;
  const relative = dueAtIso ? formatMonitorOffset(dueAtIso) : null;
  const absolute = scheduledRetry.scheduledRetryAt
    ? formatDateTime(scheduledRetry.scheduledRetryAt)
    : null;
  const reason = formatRetryReason(scheduledRetry.scheduledRetryReason);
  const attempt =
    typeof scheduledRetry.scheduledRetryAttempt === "number"
    && Number.isFinite(scheduledRetry.scheduledRetryAttempt)
    && scheduledRetry.scheduledRetryAttempt > 0
      ? scheduledRetry.scheduledRetryAttempt
      : null;

  const badgeLabel = continuation ? "Continuation scheduled" : "Retry scheduled";
  const titleAction = continuation ? "Automatic continuation" : "Automatic retry";
  let titleSuffix: string;
  if (relative === "now") {
    titleSuffix = "due now";
  } else if (relative) {
    titleSuffix = relative;
  } else {
    titleSuffix = "pending schedule";
  }
  const title = `${titleAction} ${titleSuffix}`;

  const helperIdle = continuation
    ? "Pulls continuation forward immediately"
    : "Pulls retry forward immediately";
  const isError = retryNow.isError || retryNow.lastError !== null;
  const isSuccessTransient = retryNow.isSuccess
    && (retryNow.data?.outcome === "promoted" || retryNow.data?.outcome === "already_promoted");

  return (
    <div
      data-testid="issue-scheduled-retry-card"
      className="mb-3 rounded-lg border border-info/30 bg-info/5 px-3 py-3"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-caption">
            <Badge variant="outline" className="border-info/30 bg-info/10 text-info">
              <Clock className="h-3 w-3" aria-hidden="true" />
              {badgeLabel}
            </Badge>
            {attempt !== null ? (
              <span className="text-muted-foreground">Attempt {attempt}</span>
            ) : null}
            {reason ? (
              <span className="text-muted-foreground">{reason}</span>
            ) : null}
          </div>
          <div className="mt-1 text-ui font-medium text-foreground">{title}</div>
          {(absolute || scheduledRetry.retryOfRunId) ? (
            <div className="mt-0.5 text-caption text-muted-foreground">
              {absolute ? <span>{absolute}</span> : null}
              {absolute && scheduledRetry.retryOfRunId ? <span>{" · "}</span> : null}
              {scheduledRetry.retryOfRunId ? (
                <span>
                  Replaces run{" "}
                  <Link
                    to={teamsDetailUrl("run", scheduledRetry.retryOfRunId)}
                    className="font-mono text-foreground hover:underline"
                  >
                    {shortRunId(scheduledRetry.retryOfRunId)}
                  </Link>
                </span>
              ) : null}
            </div>
          ) : null}
          {scheduledRetry.error ? (
            <div className="mt-1 text-caption text-muted-foreground">
              Last attempt failed: {scheduledRetry.error}. Paperclip will retry automatically.
            </div>
          ) : null}
          {isError ? (
            <RetryErrorBand
              error={retryNow.lastError}
              onRetry={() => {
                retryNow.reset();
                retryNow.mutate();
              }}
            />
          ) : null}
        </div>
        <div className="flex flex-col items-stretch gap-1 sm:items-end">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="shrink-0 shadow-none"
            onClick={() => retryNow.mutate()}
            disabled={retryNow.isPending || isSuccessTransient}
            data-testid="issue-scheduled-retry-card-retry-now"
          >
            {retryNow.isPending ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                Retrying…
              </span>
            ) : isSuccessTransient ? (
              <span className="inline-flex items-center gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                {retryNow.data?.outcome === "already_promoted" ? "Already promoted" : "Promoted"}
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5">
                <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry now
              </span>
            )}
          </Button>
          <span className="text-right text-caption text-muted-foreground sm:max-w-48">
            {retryNow.isPending
              ? "Promoting scheduled retry"
              : isSuccessTransient
                ? retryNow.data?.outcome === "already_promoted"
                  ? "Already promoted — run starting"
                  : "Promoted — run starting"
                : helperIdle}
          </span>
        </div>
      </div>
    </div>
  );
}

interface RetryErrorBandProps {
  error: RetryNowError | null;
  onRetry: () => void;
  className?: string;
}

export function RetryErrorBand({ error, onRetry, className }: RetryErrorBandProps) {
  if (!error) return null;
  return (
    <div
      className={cn(
        "mt-2 flex items-start gap-2 rounded-md border border-err/30 bg-err/5 px-2 py-1.5 text-caption text-err",
        className,
      )}
      role="alert"
      data-testid="issue-scheduled-retry-error-band"
    >
      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="font-medium">Couldn&apos;t retry now</div>
        <div className="mt-0.5 text-muted-foreground">{error.message}</div>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="shrink-0 font-medium text-err hover:underline"
      >
        Try again
      </button>
    </div>
  );
}
