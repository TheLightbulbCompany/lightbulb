// apps/frontend/src/components/teams/issues/IssueRunLedger.tsx

// Ported from upstream Paperclip's IssueRunLedger
// (paperclip/ui/src/components/IssueRunLedger.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Liveness/stop/retry/silence copy, run merging,
// the child-work summary and the watchdog decisions are verbatim. Also carries
// SourceResolvedFoldBadge + readSourceResolvedWatchdogFold
// (ui/src/components/SourceResolvedFoldBadge.tsx, ui/src/lib/source-resolved-watchdog-fold.ts)
// and ResponsibleUserDenialNotice (ui/src/components/ResponsibleUserDenialNotice.tsx),
// each inlined below as the ledger is their only user here. The responsible-user
// copy contract lives in ./lib/responsible-user-denial (shared with the
// issue-write-denial contract).
//
// Adaptations vs upstream (only what had to change):
//   - react-query reads            -> useTeamsApi.read on the BFF's issue-scoped
//                                     /runs, /live-runs, /active-run, /children, /activity
//                                     (the same keys CommentThread/SubIssuesSection
//                                     read, so SWR dedupes them). No refetchInterval:
//                                     TeamsEventsProvider refreshes these keys on run
//                                     events.
//   - watchdog mutation + toast    -> useTeamsApi.post to
//                                     /heartbeat-runs/{id}/watchdog-decisions, useTeamsApi
//                                     mutate, lib/notify
//   - canBoardRecordWatchdogDecision (accessApi.getCurrentBoardAccess)
//                                  -> dropped: no board-access surface here, so the
//                                     actions always show and Paperclip's own 403
//                                     lands in the inline error (same copy)
//   - react-router Links           -> in-shell anchors (run / issue detail panels)
//   - relativeTime                 -> shared/lib/timeAgo
//   - palette (emerald/cyan/amber/orange/yellow/red/sky/blue/slate)
//                                  -> brand state tokens ok/info/warn/err + muted
//                                     neutral (.dark re-points them, so dark: collapses)
//   - text-(length:--text-micro)   -> text-2xs; the 3-column stat grid is one
//                                     column (the ledger sits in the 320px side rail)
//
// Dropped (no surface here):
//   - provider trace badge, trace metadata query and "Re-run with trace"
//     (providerTrace is never exposed through the BFF)
//   - RunnerInspector dialog, the "Inspect run" button and ?inspectRun= deep link
//     (the run id links to the run detail panel instead)

"use client";

import { useMemo, useState, type ReactNode } from "react";
import { ShieldX, Sparkles, UserX } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { notifyError } from "@/lib/notify";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type {
  CompanyAgent,
  CompanyMember,
  Issue,
  IssueActivityEvent,
  IssueLiveRun,
  IssueRun,
  IssueRunOutputSilence,
  RunLivenessState,
  WatchdogDecisionInput,
} from "@/components/teams/shared/types";
import { describeRunRetryState } from "./lib/runRetryState";
import {
  describeResponsibleUserDenial,
  isResponsibleUserDenialCode,
  responsibleUserLabel,
  type ResponsibleUserDenialCode,
} from "./lib/responsible-user-denial";
import { IssueActivityEventRow } from "./IssueActivityEventRow";

/** Upstream's react-router Link, as an in-shell anchor (new-tab still works). */
function Link({ to, className, children }: { to: string; className?: string; children: React.ReactNode }) {
  return (
    <a href={to} className={className} onClick={(e) => handleShellLinkClick(e, to)}>
      {children}
    </a>
  );
}

// --- ui/src/components/ResponsibleUserDenialNotice.tsx ---------------------

function ResponsibleUserDenialNotice({ code, userName }: { code: ResponsibleUserDenialCode; userName?: string | null }) {
  const copy = describeResponsibleUserDenial(code, { userName });
  const isUnavailable = copy.tone === "unavailable";
  const Icon = isUnavailable ? UserX : ShieldX;
  const tone = isUnavailable ? "border-warn/40 bg-warn/10 text-warn" : "border-err/40 bg-err/10 text-err";
  return (
    <div
      role="status"
      data-testid="responsible-user-denial-notice"
      data-denial-code={code}
      data-denial-tone={copy.tone}
      className={cn("rounded-md border px-3 py-2.5 text-ui shadow-sm", tone)}
    >
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium leading-5">{copy.title}</p>
          <p className="leading-5">{copy.description}</p>
          <p className="text-caption leading-5">{copy.recommendedAction}</p>
        </div>
      </div>
    </div>
  );
}

// --- ui/src/lib/source-resolved-watchdog-fold.ts (readSourceResolvedWatchdogFold)
// Only its presence gates the badge, so this keeps upstream's required-field check.

function readSourceResolvedWatchdogFold(resultJson: Record<string, unknown> | null | undefined): boolean {
  const fold = asRecord(asRecord(resultJson)?.sourceResolvedWatchdogFold);
  if (!fold) return false;
  return ["sourceIssueId", "sourceIssueStatus", "sameRunEvidenceKind", "sameRunEvidenceId", "sameRunEvidenceAt"].every(
    (key) => typeof fold[key] === "string" && fold[key] !== "",
  );
}

// --- ui/src/components/SourceResolvedFoldBadge.tsx --------------------------

function SourceResolvedFoldBadge() {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-md border border-ok/40 bg-ok/10 px-1.5 py-0.5 text-2xs font-medium text-ok"
      title="System folded this run as a source-resolved false positive."
      aria-label="Source-resolved watchdog fold"
    >
      <Sparkles className="h-3 w-3" aria-hidden />
      Source-resolved
    </span>
  );
}

// --- ui/src/components/IssueRunLedger.tsx -----------------------------------

type AgentNameMap = ReadonlyMap<string, Pick<CompanyAgent, "name">>;

type IssueRunLedgerContentProps = {
  runs: IssueRun[];
  liveRuns?: IssueLiveRun[];
  activeRun?: IssueLiveRun | null;
  issueStatus: Issue["status"];
  childIssues: Issue[];
  agentMap: AgentNameMap;
  activityEvents?: IssueActivityEvent[];
  renderActivityEvent?: (event: IssueActivityEvent) => ReactNode;
  resolveUserLabel?: (userId: string) => string | null | undefined;
  pendingWatchdogDecision?: WatchdogDecisionInput["decision"] | null;
  canRecordWatchdogDecisions?: boolean;
  watchdogDecisionError?: string | null;
  onWatchdogDecision?: (input: WatchdogDecisionInput) => void;
};

type LedgerRun = IssueRun & {
  isLive?: boolean;
  agentName?: string | null;
  outputSilence?: IssueRunOutputSilence | null;
};

type LedgerFeedItem =
  | { kind: "run"; id: string; timestamp: string; run: LedgerRun }
  | { kind: "activity"; id: string; timestamp: string; event: IssueActivityEvent };

type LivenessCopy = {
  label: string;
  tone: string;
  description: string;
};

const LIVENESS_COPY: Record<RunLivenessState, LivenessCopy> = {
  completed: {
    label: "Completed",
    tone: "border-ok/30 bg-ok/10 text-ok",
    description: "Task reached a terminal state.",
  },
  advanced: {
    label: "Advanced",
    tone: "border-info/30 bg-info/10 text-info",
    description: "Run produced concrete evidence of progress.",
  },
  plan_only: {
    label: "Plan only",
    tone: "border-warn/30 bg-warn/10 text-warn",
    description: "Run described future work without concrete action evidence.",
  },
  empty_response: {
    label: "Empty response",
    tone: "border-warn/30 bg-warn/10 text-warn",
    description: "Run finished without useful output.",
  },
  blocked: {
    label: "Blocked",
    tone: "border-warn/30 bg-warn/10 text-warn",
    description: "Run or task declared a blocker.",
  },
  failed: {
    label: "Failed",
    tone: "border-err/30 bg-err/10 text-err",
    description: "Run ended unsuccessfully.",
  },
  needs_followup: {
    label: "Needs follow-up",
    tone: "border-info/30 bg-info/10 text-info",
    description:
      "Run produced useful output but did not prove concrete progress.",
  },
};

const PENDING_LIVENESS_COPY: LivenessCopy = {
  label: "Checks after finish",
  tone: "border-border bg-background text-muted-foreground",
  description: "Liveness is evaluated after the run finishes.",
};

const RETRY_PENDING_LIVENESS_COPY: LivenessCopy = {
  label: "Retry pending",
  tone: "border-info/30 bg-info/10 text-info",
  description: "Paperclip queued an automatic retry that has not started yet.",
};

const MISSING_LIVENESS_COPY: LivenessCopy = {
  label: "No liveness data",
  tone: "border-border bg-background text-muted-foreground",
  description: "This run has no persisted liveness classification.",
};

const TERMINAL_CHILD_STATUSES = new Set<Issue["status"]>(["done", "cancelled"]);
const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);

type RunOutputSilenceCopy = {
  label: string;
  tone: string;
};

const RUN_OUTPUT_SILENCE_COPY: Partial<
  Record<IssueRunOutputSilence["level"], RunOutputSilenceCopy>
> = {
  suspicious: {
    label: "Output silence",
    tone: "border-warn/30 bg-warn/10 text-warn",
  },
  critical: {
    label: "Critical silence",
    tone: "border-err/30 bg-err/10 text-err",
  },
  snoozed: {
    label: "Silence snoozed",
    tone: "border-info/30 bg-info/10 text-info",
  },
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return null;
  return value as Record<string, unknown>;
}

function readString(value: unknown) {
  return typeof value === "string" && value.trim().length > 0
    ? value.trim()
    : null;
}

function readNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatDuration(
  start: string | Date | null | undefined,
  end: string | Date | null | undefined,
) {
  if (!start) return null;
  const startMs = new Date(start).getTime();
  const endMs = end ? new Date(end).getTime() : Date.now();
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return null;
  const totalSeconds = Math.max(0, Math.round((endMs - startMs) / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60)
    return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function liveRunToLedgerRun(run: IssueLiveRun): LedgerRun {
  return {
    runId: run.id,
    status: run.status,
    agentId: run.agentId,
    agentName: run.agentName,
    startedAt: run.startedAt ?? null,
    finishedAt: run.finishedAt ?? null,
    createdAt: run.createdAt ?? new Date().toISOString(),
    invocationSource: run.invocationSource,
    resultJson: null,
    isLive: run.status === "queued" || run.status === "running",
    outputSilence: run.outputSilence,
  };
}

function runTimestamp(run: LedgerRun) {
  return run.startedAt ?? run.createdAt ?? "";
}

function mergeRuns(
  runs: IssueRun[],
  liveRuns: IssueLiveRun[] | undefined,
  activeRun: IssueLiveRun | null | undefined,
) {
  const byId = new Map<string, LedgerRun>();
  for (const run of runs) byId.set(run.runId, run);
  for (const run of liveRuns ?? []) {
    const existing = byId.get(run.id);
    byId.set(
      run.id,
      existing
        ? {
            ...existing,
            isLive: true,
            agentName: run.agentName,
            outputSilence: run.outputSilence,
          }
        : liveRunToLedgerRun(run),
    );
  }
  if (activeRun) {
    const existing = byId.get(activeRun.id);
    if (existing) {
      byId.set(activeRun.id, {
        ...existing,
        isLive: isActiveRun(existing) || isActiveRun(activeRun),
        agentName: activeRun.agentName,
        outputSilence: activeRun.outputSilence,
      });
    } else {
      byId.set(activeRun.id, liveRunToLedgerRun(activeRun));
    }
  }

  return [...byId.values()].sort((a, b) => {
    const aTime = new Date(runTimestamp(a)).getTime();
    const bTime = new Date(runTimestamp(b)).getTime();
    if (aTime !== bTime) return bTime - aTime;
    return b.runId.localeCompare(a.runId);
  });
}

// Rows are BFF-forwarded upstream JSON; a drifted row may omit status.
function statusLabel(status: string | null | undefined) {
  return (status ?? "unknown").replace(/_/g, " ");
}

// The BFF forwards upstream bodies verbatim; only a row carrying a run id is a run.
function isRunRow<T extends { id?: unknown }>(row: unknown): row is T {
  return typeof (row as { id?: unknown } | null)?.id === "string";
}

function isActiveRun(run: { status: string; isLive?: boolean }) {
  return run.isLive || ACTIVE_RUN_STATUSES.has(run.status);
}

function runSummary(run: LedgerRun, agentMap: AgentNameMap) {
  const agentName = compactAgentName(run, agentMap);
  if (run.status === "running") return `Running now by ${agentName}`;
  if (run.status === "queued") return `Queued for ${agentName}`;
  if (run.status === "scheduled_retry")
    return `Automatic retry scheduled for ${agentName}`;
  return `${statusLabel(run.status)} by ${agentName}`;
}

function livenessCopyForRun(run: LedgerRun) {
  if (run.status === "scheduled_retry") return RETRY_PENDING_LIVENESS_COPY;
  if (run.livenessState && LIVENESS_COPY[run.livenessState]) return LIVENESS_COPY[run.livenessState];
  return isActiveRun(run) ? PENDING_LIVENESS_COPY : MISSING_LIVENESS_COPY;
}

function stopReasonLabel(run: IssueRun) {
  const result = asRecord(run.resultJson);
  const stopReason = readString(result?.stopReason);
  const timeoutFired = result?.timeoutFired === true;
  const effectiveTimeoutSec = readNumber(result?.effectiveTimeoutSec);
  const timeoutText =
    effectiveTimeoutSec && effectiveTimeoutSec > 0
      ? `${effectiveTimeoutSec}s timeout`
      : null;

  if (timeoutFired || stopReason === "timeout") {
    return timeoutText ? `timeout (${timeoutText})` : "timeout";
  }
  if (
    stopReason === "max_turns_exhausted" ||
    stopReason === "turn_limit_exhausted"
  )
    return "max turns exhausted";
  if (stopReason === "budget_paused") return "budget paused";
  if (stopReason === "cancelled") return "cancelled";
  if (stopReason === "paused") return "paused by board";
  if (stopReason === "process_lost") return "process lost";
  if (stopReason === "unmanaged_background_task_stopped")
    return "unmanaged background task stopped";
  if (stopReason === "adapter_failed") return "adapter failed";
  if (stopReason === "completed")
    return timeoutText ? `completed (${timeoutText})` : "completed";
  return timeoutText;
}

function stopStatusLabel(run: LedgerRun, stopReason: string | null) {
  if (stopReason) return stopReason;
  if (run.status === "scheduled_retry") return "Retry pending";
  if (run.status === "queued") return "Waiting to start";
  if (run.status === "running") return "Still running";
  if (!run.livenessState) return "Unavailable";
  return "No stop reason";
}

function lastUsefulActionLabel(run: LedgerRun) {
  if (run.status === "scheduled_retry") return "Waiting for next attempt";
  if (run.lastUsefulActionAt) return timeAgo(run.lastUsefulActionAt);
  if (isActiveRun(run)) return "No action recorded yet";
  if (
    run.livenessState === "plan_only" ||
    run.livenessState === "needs_followup"
  ) {
    return "No concrete action";
  }
  if (run.livenessState === "empty_response") return "No useful output";
  if (!run.livenessState) return "Unavailable";
  return "None recorded";
}

function continuationLabel(run: LedgerRun) {
  if (!run.continuationAttempt || run.continuationAttempt <= 0) return null;
  return `Continuation attempt ${run.continuationAttempt}`;
}

function hasExhaustedContinuation(run: IssueRun) {
  return /continuation attempts exhausted/i.test(run.livenessReason ?? "");
}

function childIssueSummary(childIssues: Issue[]) {
  const active = childIssues.filter(
    (issue) => !TERMINAL_CHILD_STATUSES.has(issue.status),
  );
  const done = childIssues.filter((issue) => issue.status === "done").length;
  const cancelled = childIssues.filter(
    (issue) => issue.status === "cancelled",
  ).length;
  return { active, done, cancelled, total: childIssues.length };
}

function compactAgentName(run: LedgerRun, agentMap: AgentNameMap) {
  const agentId = run.agentId ?? "";
  return run.agentName ?? agentMap.get(agentId)?.name ?? agentId.slice(0, 8);
}

function formatSilenceAge(ms: number | null | undefined) {
  if (!ms || ms <= 0) return null;
  const totalMinutes = Math.floor(ms / 60_000);
  if (totalMinutes < 1) return "under 1 minute";
  if (totalMinutes < 60)
    return `${totalMinutes} minute${totalMinutes === 1 ? "" : "s"}`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${hours}h ${minutes}m`;
}

function watchdogDecisionErrorMessage(error: unknown) {
  if ((error as { status?: number } | null)?.status === 403) {
    return "Only the board or the assigned recovery owner can record watchdog decisions";
  }
  return error instanceof Error && error.message.trim().length > 0
    ? error.message
    : "Paperclip could not record the watchdog decision.";
}

export function IssueRunLedger({
  issueId,
  issueStatus,
  agents,
  members = [],
}: {
  issueId: string;
  issueStatus: Issue["status"];
  agents: CompanyAgent[];
  members?: CompanyMember[];
}) {
  const { read, post, mutate } = useTeamsApi();
  const [watchdogDecisionError, setWatchdogDecisionError] = useState<string | null>(null);
  const [pendingWatchdogDecision, setPendingWatchdogDecision] =
    useState<WatchdogDecisionInput["decision"] | null>(null);
  const { data: runsResp } = read<{ runs: IssueRun[] }>(`/issues/${issueId}/runs`);
  const { data: liveRuns } = read<IssueLiveRun[]>(`/issues/${issueId}/live-runs`, { shouldRetryOnError: false });
  const { data: activeRun } = read<IssueLiveRun | null>(`/issues/${issueId}/active-run`, { shouldRetryOnError: false });
  const { data: childrenResp } = read<{ issues: Issue[] }>(`/issues/${issueId}/children`);
  const { data: activity } = read<IssueActivityEvent[]>(`/issues/${issueId}/activity`);
  const agentMap = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const resolveUserLabel = (userId: string) => {
    const member = members.find((m) => m.userId === userId);
    return member?.name ?? member?.email ?? null;
  };

  const recordWatchdogDecision = (input: WatchdogDecisionInput) => {
    const { runId, ...body } = input;
    setWatchdogDecisionError(null);
    setPendingWatchdogDecision(input.decision);
    post(`/heartbeat-runs/${runId}/watchdog-decisions`, {
      decision: body.decision,
      evaluationIssueId: body.evaluationIssueId ?? null,
      reason: body.reason ?? null,
      snoozedUntil: body.snoozedUntil ?? null,
    })
      .then(() => {
        void mutate(`/teams/issues/${issueId}/active-run`);
        void mutate(`/teams/issues/${issueId}/live-runs`);
      })
      .catch((error: unknown) => {
        const message = watchdogDecisionErrorMessage(error);
        setWatchdogDecisionError(message);
        notifyError(`Watchdog decision not recorded: ${message}`, {
          dedupeKey: `watchdog-decision:${issueId}`,
        });
      })
      .finally(() => setPendingWatchdogDecision(null));
  };

  return (
    <IssueRunLedgerContent
      runs={(Array.isArray(runsResp?.runs) ? runsResp?.runs ?? [] : []).filter((r) => typeof r?.runId === "string")}
      liveRuns={Array.isArray(liveRuns) ? liveRuns.filter((r) => isRunRow<IssueLiveRun>(r)) : undefined}
      activeRun={isRunRow<IssueLiveRun>(activeRun) ? activeRun : null}
      issueStatus={issueStatus}
      childIssues={childrenResp?.issues ?? []}
      agentMap={agentMap}
      activityEvents={Array.isArray(activity) ? activity : []}
      renderActivityEvent={(event) => (
        <IssueActivityEventRow event={event} agentMap={agentMap} resolveUserLabel={resolveUserLabel} />
      )}
      resolveUserLabel={resolveUserLabel}
      pendingWatchdogDecision={pendingWatchdogDecision}
      watchdogDecisionError={watchdogDecisionError}
      onWatchdogDecision={recordWatchdogDecision}
    />
  );
}

export function IssueRunLedgerContent({
  runs,
  liveRuns,
  activeRun,
  issueStatus,
  childIssues,
  agentMap,
  activityEvents,
  renderActivityEvent,
  resolveUserLabel,
  pendingWatchdogDecision,
  canRecordWatchdogDecisions = true,
  watchdogDecisionError,
  onWatchdogDecision,
}: IssueRunLedgerContentProps) {
  const ledgerRuns = useMemo(
    () => mergeRuns(runs, liveRuns, activeRun),
    [activeRun, liveRuns, runs],
  );
  const latestRun = ledgerRuns[0] ?? null;
  const latestSilentRun = useMemo(
    () =>
      ledgerRuns.find(
        (run) =>
          isActiveRun(run) &&
          (run.outputSilence?.level === "critical" ||
            run.outputSilence?.level === "suspicious"),
      ) ?? null,
    [ledgerRuns],
  );
  const children = childIssueSummary(childIssues);
  const canRenderActivityEvents = Boolean(renderActivityEvent);
  const feedItems = useMemo<LedgerFeedItem[]>(() => {
    const items: LedgerFeedItem[] = [];
    for (const run of ledgerRuns) {
      items.push({ kind: "run", id: run.runId, timestamp: runTimestamp(run), run });
    }
    if (canRenderActivityEvents) {
      for (const event of activityEvents ?? []) {
        items.push({ kind: "activity", id: event.id, timestamp: String(event.createdAt), event });
      }
    }
    return items.sort((a, b) => {
      const aTime = new Date(a.timestamp).getTime();
      const bTime = new Date(b.timestamp).getTime();
      if (aTime !== bTime) return bTime - aTime;
      if (a.kind !== b.kind) return a.kind === "run" ? -1 : 1;
      return b.id.localeCompare(a.id);
    });
  }, [activityEvents, canRenderActivityEvents, ledgerRuns]);
  const evaluationIssueRef =
    latestSilentRun?.outputSilence?.evaluationIssueId ??
    latestSilentRun?.outputSilence?.evaluationIssueIdentifier ??
    null;

  return (
    <section className="space-y-3" aria-label="Task run ledger">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-ui font-medium text-muted-foreground">
            Run ledger
          </h3>
          <p className="text-caption text-muted-foreground">
            {latestRun
              ? runSummary(latestRun, agentMap)
              : issueStatus === "in_progress"
                ? "Waiting for the first run record."
                : "No runs linked yet."}
          </p>
        </div>
        {latestRun ? (
          <Link
            to={teamsDetailUrl("run", latestRun.runId)}
            className="shrink-0 rounded-md border border-border px-2 py-1 text-caption text-muted-foreground hover:text-foreground"
          >
            Latest run
          </Link>
        ) : null}
      </div>

      {children.total > 0 ? (
        <div className="rounded-md border border-border/70 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2 text-caption">
            <span className="font-medium text-foreground">Child work</span>
            <span className="text-muted-foreground">
              {children.active.length > 0
                ? `${children.active.length} active, ${children.done} done, ${children.cancelled} cancelled`
                : `all ${children.total} terminal (${children.done} done, ${children.cancelled} cancelled)`}
            </span>
          </div>
          {children.active.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {children.active.slice(0, 4).map((child) => (
                <Link
                  key={child.id}
                  to={teamsDetailUrl("issue", child.id)}
                  className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-2xs hover:bg-accent/40"
                >
                  <span className="shrink-0 font-mono text-muted-foreground">
                    {child.identifier ?? child.id.slice(0, 8)}
                  </span>
                  <span className="truncate">{child.title}</span>
                  <span className="shrink-0 text-muted-foreground">
                    {statusLabel(child.status)}
                  </span>
                </Link>
              ))}
              {children.active.length > 4 ? (
                <span className="rounded-md border border-border px-2 py-1 text-2xs text-muted-foreground">
                  +{children.active.length - 4} more
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {latestSilentRun?.outputSilence ? (
        <div
          className={cn(
            "rounded-md border px-3 py-2 text-caption",
            latestSilentRun.outputSilence.level === "critical"
              ? "border-err/30 bg-err/10 text-err"
              : "border-warn/30 bg-warn/10 text-warn",
          )}
        >
          <p className="font-medium">
            {latestSilentRun.outputSilence.level === "critical"
              ? "Critical output silence"
              : "Output silence watchdog warning"}
          </p>
          <p className="mt-1">
            Latest active run has been silent for{" "}
            {formatSilenceAge(latestSilentRun.outputSilence.silenceAgeMs) ??
              "an extended period"}
            .
            {latestSilentRun.outputSilence.evaluationIssueIdentifier && evaluationIssueRef ? (
              <>
                {" "}
                Review{" "}
                <Link
                  to={teamsDetailUrl("issue", evaluationIssueRef)}
                  className="font-medium underline underline-offset-2"
                >
                  {latestSilentRun.outputSilence.evaluationIssueIdentifier}
                </Link>{" "}
                for recovery context.
              </>
            ) : null}
          </p>
          <p className="mt-1">
            {latestSilentRun.outputSilence.evaluationIssueIdentifier
              ? "This signal is informational. Paperclip did not create new delegated recovery work."
              : "This signal is informational. Paperclip did not create or assign a recovery task."}
          </p>
          {onWatchdogDecision && canRecordWatchdogDecisions ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              <button
                type="button"
                className="rounded-md border border-border bg-background/80 px-2 py-1 text-2xs text-foreground hover:bg-background"
                onClick={() =>
                  onWatchdogDecision({
                    runId: latestSilentRun.runId,
                    decision: "continue",
                    evaluationIssueId:
                      latestSilentRun.outputSilence?.evaluationIssueId ?? null,
                  })
                }
                disabled={pendingWatchdogDecision != null}
              >
                Continue monitoring
              </button>
              <button
                type="button"
                className="rounded-md border border-border bg-background/80 px-2 py-1 text-2xs text-foreground hover:bg-background"
                onClick={() =>
                  onWatchdogDecision({
                    runId: latestSilentRun.runId,
                    decision: "snooze",
                    evaluationIssueId:
                      latestSilentRun.outputSilence?.evaluationIssueId ?? null,
                    snoozedUntil: new Date(
                      Date.now() + 60 * 60 * 1000,
                    ).toISOString(),
                    reason: "Snoozed from issue run ledger",
                  })
                }
                disabled={pendingWatchdogDecision != null}
              >
                Snooze 1h
              </button>
              <button
                type="button"
                className="rounded-md border border-border bg-background/80 px-2 py-1 text-2xs text-foreground hover:bg-background"
                onClick={() =>
                  onWatchdogDecision({
                    runId: latestSilentRun.runId,
                    decision: "dismissed_false_positive",
                    evaluationIssueId:
                      latestSilentRun.outputSilence?.evaluationIssueId ?? null,
                    reason: "Dismissed from issue run ledger",
                  })
                }
                disabled={pendingWatchdogDecision != null}
              >
                Mark false positive
              </button>
            </div>
          ) : null}
          {watchdogDecisionError ? (
            <p className="mt-2 rounded-md border border-err/30 bg-err/10 px-2 py-1 text-2xs text-err">
              {watchdogDecisionError}
            </p>
          ) : null}
        </div>
      ) : null}

      {feedItems.length === 0 ? (
        <div className="rounded-md border border-dashed border-border px-3 py-3 text-ui text-muted-foreground">
          {renderActivityEvent
            ? "Runs and activity will appear here once this task has history."
            : "Historical runs without liveness metadata will appear here once linked to this task."}
        </div>
      ) : (
        <div className="space-y-1.5">
          {feedItems.slice(0, 20).map((item) => {
            if (item.kind === "activity") {
              return <div key={`activity:${item.id}`}>{renderActivityEvent?.(item.event)}</div>;
            }
            const run = item.run;
            const timestamp = runTimestamp(run);
            const liveness = livenessCopyForRun(run);
            const stopReason = stopReasonLabel(run);
            const duration = formatDuration(run.startedAt, run.finishedAt);
            const exhausted = hasExhaustedContinuation(run);
            const continuation = continuationLabel(run);
            const retryState = describeRunRetryState(run);
            const agentName = compactAgentName(run, agentMap);
            const onBehalfOfLabel = run.responsibleUserId
              ? responsibleUserLabel(resolveUserLabel?.(run.responsibleUserId))
              : null;
            const denialCode = isResponsibleUserDenialCode(run.errorCode)
              ? run.errorCode
              : null;
            const sourceResolvedFold = readSourceResolvedWatchdogFold(
              run.resultJson,
            );
            const silenceCopy = run.outputSilence
              ? RUN_OUTPUT_SILENCE_COPY[run.outputSilence.level]
              : undefined;
            return (
              <article
                key={`run:${run.runId}`}
                className="space-y-1.5 rounded-lg border border-border/60 px-3 py-2 text-caption text-muted-foreground"
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-foreground">Run</span>
                  <Link
                    to={teamsDetailUrl("run", run.runId)}
                    className="min-w-0 max-w-full truncate font-mono text-foreground hover:underline"
                  >
                    {run.runId.slice(0, 8)}
                  </Link>
                  <span>by {agentName}</span>
                  {onBehalfOfLabel ? (
                    <span
                      data-testid="run-on-behalf-of"
                      className="min-w-0 max-w-full truncate text-muted-foreground"
                      title={`Acting on behalf of ${onBehalfOfLabel}`}
                    >
                      on behalf of{" "}
                      <span className="text-foreground">{onBehalfOfLabel}</span>
                    </span>
                  ) : null}
                  <span className="rounded-md border border-border px-1.5 py-0.5 text-2xs capitalize text-muted-foreground">
                    {statusLabel(run.status)}
                  </span>
                  {run.isLive ? (
                    <span className="inline-flex items-center gap-1 rounded-md border border-info/30 bg-info/10 px-1.5 py-0.5 text-2xs text-info">
                      <span className="h-1.5 w-1.5 rounded-full bg-info" />
                      live
                    </span>
                  ) : null}
                  <span
                    className={cn(
                      "rounded-md border px-1.5 py-0.5 text-2xs font-medium",
                      liveness.tone,
                    )}
                    title={liveness.description}
                  >
                    {liveness.label}
                  </span>
                  {exhausted ? (
                    <span className="rounded-md border border-err/30 bg-err/10 px-1.5 py-0.5 text-2xs font-medium text-err">
                      Exhausted
                    </span>
                  ) : null}
                  {continuation ? (
                    <span className="text-2xs text-muted-foreground">
                      {continuation}
                    </span>
                  ) : null}
                  {retryState ? (
                    <span
                      className={cn(
                        "rounded-md border px-1.5 py-0.5 text-2xs font-medium",
                        retryState.tone,
                      )}
                    >
                      {retryState.badgeLabel}
                    </span>
                  ) : null}
                  {silenceCopy ? (
                    <span
                      className={cn(
                        "rounded-md border px-1.5 py-0.5 text-2xs font-medium",
                        silenceCopy.tone,
                      )}
                    >
                      {silenceCopy.label}
                    </span>
                  ) : null}
                  {sourceResolvedFold ? <SourceResolvedFoldBadge /> : null}
                  {timestamp ? (
                    <span className="ml-auto shrink-0">
                      {timeAgo(timestamp)}
                    </span>
                  ) : null}
                </div>

                <div className="grid gap-1 text-caption text-muted-foreground">
                  <div className="min-w-0">
                    <span className="text-foreground">Elapsed</span>{" "}
                    {duration ?? "unknown"}
                  </div>
                  <div className="min-w-0">
                    <span className="text-foreground">Last useful action</span>{" "}
                    {lastUsefulActionLabel(run)}
                  </div>
                  <div className="min-w-0">
                    <span className="text-foreground">Stop</span>{" "}
                    {stopStatusLabel(run, stopReason)}
                  </div>
                </div>

                {retryState ? (
                  <div className="rounded-md border border-border/70 bg-accent/20 px-2 py-2 text-caption leading-5 text-muted-foreground">
                    {retryState.detail ? <p>{retryState.detail}</p> : null}
                    {retryState.secondary ? (
                      <p>{retryState.secondary}</p>
                    ) : null}
                    {retryState.retryOfRunId ? (
                      <p>
                        Retry of{" "}
                        <Link
                          to={teamsDetailUrl("run", retryState.retryOfRunId)}
                          className="font-mono text-foreground hover:underline"
                        >
                          {retryState.retryOfRunId.slice(0, 8)}
                        </Link>
                      </p>
                    ) : null}
                  </div>
                ) : null}

                {run.livenessReason ? (
                  <p className="min-w-0 break-words text-caption leading-5 text-muted-foreground">
                    {run.livenessReason}
                  </p>
                ) : null}

                {denialCode ? (
                  <ResponsibleUserDenialNotice
                    code={denialCode}
                    userName={
                      run.responsibleUserId
                        ? resolveUserLabel?.(run.responsibleUserId)
                        : null
                    }
                  />
                ) : null}

                {run.nextAction ? (
                  <div className="min-w-0 rounded-md bg-accent/40 px-2 py-1.5 text-caption leading-5">
                    <span className="font-medium text-foreground">
                      Next action:{" "}
                    </span>
                    <span className="break-words text-muted-foreground">
                      {run.nextAction}
                    </span>
                  </div>
                ) : null}
              </article>
            );
          })}
          {feedItems.length > 20 ? (
            <div className="px-3 py-2 text-caption text-muted-foreground">
              {feedItems.length - 20} older items not shown
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}
