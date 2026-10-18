// apps/frontend/src/components/teams/issues/IssueBlockedNotice.tsx

// Ported from upstream Paperclip's IssueBlockedNotice
// (paperclip/ui/src/components/IssueBlockedNotice.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Copy, props, the amber / blue "Waiting on live
// work" / handoff / in-flight branches and every visibility condition are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types        -> components/teams/shared/types
//   - IssueLinkQuicklook (react-query detail fetch + hover popover)
//                                      -> BlockerLink: a plain in-shell anchor to
//                                         teamsDetailUrl("issue", identifier) with our
//                                         StatusIcon. Its useTeamsApi.read on the
//                                         blocker's detail key renders the identifier
//                                         unlinked when the viewer cannot read it (404);
//                                         the fork has no locked chip, so this is the
//                                         minimal fallback. No hover quicklook.
//   - StatusGlyph                      -> StatusIcon
//   - hooks/useRetryNowMutation        -> IssueScheduledRetryCard's useRetryNowMutation
//                                         (the BFF's /issues/{id}/scheduled-retry/retry-now)
//   - lib/recovery-display, recovery-lineage -> ./lib/recovery
//   - router Link to /agents/{id}/runs/{run} -> teamsDetailUrl("run", runId) +
//                                         handleShellLinkClick (in-shell run detail)
//   - amber / blue / sky / red palette -> brand state tokens warn / info / err
//                                         (.dark re-points them, so dark: variants collapse)
//   - text-sm / text-xs / --text-micro / --text-nano -> text-ui / text-caption
//   - max-w-(--sz-18rem)               -> max-w-72

"use client";

import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Circle, Flag, Loader2, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import type {
  IssueBlockerAttention,
  IssueRecoveryAction,
  IssueRelationIssueSummary,
  IssueScheduledRetry,
  SuccessfulRunHandoffState,
} from "@/components/teams/shared/types";
import { formatMonitorOffset } from "./lib/issue-monitor";
import { RetryErrorBand, useRetryNowMutation } from "./IssueScheduledRetryCard";
import {
  isAssignedBacklogBlocker,
  orderWaitingBlockers,
  type WaitingBlockerStatus,
} from "./lib/issue-blockers";
import { isSuccessfulRunHandoffRequired } from "./lib/successful-run-handoff";
import {
  deriveActiveRecoveryDisplayState,
  formatRecoveryLineageSummary,
  readRecoveryRetryLineage,
  RECOVERY_CHIP_DEFAULT_TONE,
  recoveryChipLabel,
} from "./lib/recovery";

/** Upstream's router Link, as an in-shell anchor (new-tab still works). */
function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  return (
    <a href={to} className={className} onClick={(e) => handleShellLinkClick(e, to)}>
      {children}
    </a>
  );
}

/**
 * Stands in for upstream's IssueLinkQuicklook. The detail read shares the blocker
 * page's SWR key, so opening the blocker afterwards is instant; a 404 means the
 * viewer cannot read it, and the chip shows its identifier without a link.
 */
// ponytail: one detail read per blocker chip; batch it if a task ever lists dozens of blockers.
function BlockerLink({
  blocker,
  className,
  children,
}: {
  blocker: IssueRelationIssueSummary;
  className: string;
  children: ReactNode;
}) {
  const { read } = useTeamsApi();
  const issuePathId = blocker.identifier ?? blocker.id;
  const { error } = read(teamsQueryKeys.issues.detail(issuePathId));
  if ((error as { status?: number } | undefined)?.status === 404) {
    return (
      <span data-testid="issue-blocked-notice-unreadable" className={cn(className, "hover:no-underline")}>
        <span>{blocker.identifier ?? blocker.id.slice(0, 8)}</span>
      </span>
    );
  }
  return (
    <Link to={teamsDetailUrl("issue", issuePathId)} className={className}>
      {children}
    </Link>
  );
}

function BlockerRecoveryIndicator({
  action,
  scheduledRetry,
}: {
  action: IssueRecoveryAction;
  /** The blocker's own scheduled retry, used to verify that the stored attempt is in flight. */
  scheduledRetry?: IssueScheduledRetry | null;
}) {
  const liveness = { scheduledRetry: scheduledRetry ?? null };
  const state = deriveActiveRecoveryDisplayState(action, liveness);
  if (!state) return null;
  const tone = RECOVERY_CHIP_DEFAULT_TONE[state];
  const Icon = tone.icon;
  // The blocker chip reads the same stored lineage as the source task's recovery card, so
  // a parent view never contradicts the task it is waiting on.
  const lineage = readRecoveryRetryLineage(action, liveness);
  const label = recoveryChipLabel(state, action.kind, lineage);
  const detail = lineage ? formatRecoveryLineageSummary(lineage) : null;
  return (
    <Badge variant="outline"
      data-testid="issue-blocked-notice-recovery-indicator"
      data-recovery-state={state}
      data-recovery-kind={action.kind}
      data-recovery-lane={lineage?.lane}
      role="status"
      aria-label={detail ? `${label} — ${detail}` : label}
      title={detail
        ? `${label} — ${detail}. Open the source task to act.`
        : `${label} — open the source task to act.`}
      className={`[&>svg]:size-2.5 gap-0.5 px-1.5 text-caption ${tone.className}`}
    >
      <Icon className="h-2.5 w-2.5" aria-hidden />
      {label}
    </Badge>
  );
}

function SuccessfulRunRetryNowControl({
  issueId,
  scheduledRetry,
}: {
  issueId: string;
  scheduledRetry: IssueScheduledRetry;
}) {
  const retryNow = useRetryNowMutation(issueId);
  const dueAtIso = scheduledRetry.scheduledRetryAt
    ? new Date(scheduledRetry.scheduledRetryAt).toISOString()
    : null;
  const relative = dueAtIso ? formatMonitorOffset(dueAtIso) : null;
  const scheduleLabel = relative === "now"
    ? "due now"
    : relative
      ? `scheduled ${relative}`
      : "scheduled";
  const success = retryNow.isSuccess
    && (retryNow.data?.outcome === "promoted" || retryNow.data?.outcome === "already_promoted");

  return (
    <div className="mt-2 rounded-md border border-warn/40 bg-background/80 p-2">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 text-caption text-foreground">
          Paperclip will ask the assignee to choose the next step {scheduleLabel}. Retry now starts that follow-up immediately.
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="shrink-0 border-warn/50 bg-background/80 text-foreground shadow-none hover:bg-warn/10"
          onClick={() => retryNow.mutate()}
          disabled={retryNow.isPending || success}
          data-testid="issue-next-step-retry-now"
        >
          {retryNow.isPending ? (
            <span className="inline-flex items-center gap-1.5">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              Retrying...
            </span>
          ) : success ? (
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
      </div>
      <RetryErrorBand
        error={retryNow.lastError}
        className="mt-2"
        onRetry={() => {
          retryNow.reset();
          retryNow.mutate();
        }}
      />
    </div>
  );
}

const EMPTY_LIVE_IDS: ReadonlySet<string> = new Set<string>();

function WaitingChipLink({
  blocker,
  running = false,
}: {
  blocker: IssueRelationIssueSummary;
  running?: boolean;
}) {
  return (
    <BlockerLink
      blocker={blocker}
      className="inline-flex max-w-full items-center gap-1 rounded-md border border-info/40 bg-background/80 px-2 py-1 font-mono text-caption text-foreground transition-colors hover:border-info hover:bg-info/10 hover:underline"
    >
      <StatusIcon status={blocker.status} className="h-3 w-3" />
      <span>{blocker.identifier ?? blocker.id.slice(0, 8)}</span>
      <span className="max-w-72 truncate font-sans text-caption text-info">
        {blocker.title}
      </span>
      {running ? (
        <span className="ml-0.5 rounded-full bg-info/15 px-1.5 py-0.5 text-caption font-medium uppercase tracking-wide text-info">
          running
        </span>
      ) : null}
    </BlockerLink>
  );
}

function WaitingStepGlyph({ status }: { status: WaitingBlockerStatus }) {
  if (status === "done") {
    return <CheckCircle2 className="h-3.5 w-3.5 text-info" aria-hidden />;
  }
  if (status === "running") {
    return (
      <span className="flex h-3.5 w-3.5 items-center justify-center" aria-hidden>
        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-info" />
      </span>
    );
  }
  return <Circle className="h-3.5 w-3.5 text-info/50" aria-hidden />;
}

/**
 * Calm in-flight counterpart to the amber "still needs a next step" alarm.
 * The handoff is still `required`, but a correction run is live on the issue,
 * so the alarm would be crying wolf while an agent is already working. Saying
 * it quietly beats saying nothing: the reader still learns a disposition is
 * outstanding, and learns that the alarm comes back if the run ends without
 * choosing one.
 */
function SuccessfulRunHandoffInFlightNotice({
  liveRunId,
  assigneeAgentId,
}: {
  liveRunId?: string | null;
  assigneeAgentId?: string | null;
}) {
  const shortRunId = liveRunId ? liveRunId.slice(0, 8) : null;
  return (
    <div
      data-testid="issue-next-step-in-flight"
      data-successful-run-handoff="in_flight"
      className="mb-3 rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-caption text-muted-foreground"
    >
      <div className="flex items-start gap-2">
        <span className="mt-1 flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden>
          <span className="h-2 w-2 animate-pulse rounded-full bg-info" />
        </span>
        <p className="min-w-0 leading-5">
          A correction run is in progress — the agent is working. This alert returns if the run
          stops without choosing a next step.
          {shortRunId && liveRunId ? (
            <>
              {" "}
              {assigneeAgentId ? (
                <Link
                  to={teamsDetailUrl("run", liveRunId)}
                  className="font-mono underline underline-offset-2 hover:text-foreground"
                >
                  run {shortRunId}
                </Link>
              ) : (
                <span className="font-mono">run {shortRunId}</span>
              )}
            </>
          ) : null}
        </p>
      </div>
    </div>
  );
}

/**
 * Blue "Waiting on live work" variant — rendered in place of the
 * amber notice when `blockerAttention.state === "covered"`: the blocker chain
 * is a healthy plan executing in order and something in it is live.
 */
function WaitingOnLiveWorkNotice({
  blockerAttentionState,
  chainBlockers,
  terminalBlockers,
  liveIds,
  parkedBlockers,
  renderParkedChip,
}: {
  blockerAttentionState?: string;
  chainBlockers: IssueRelationIssueSummary[];
  terminalBlockers: IssueRelationIssueSummary[];
  liveIds: ReadonlySet<string>;
  parkedBlockers: IssueRelationIssueSummary[];
  renderParkedChip: (blocker: IssueRelationIssueSummary) => ReactNode;
}) {
  const steps = orderWaitingBlockers(chainBlockers, liveIds);
  const total = steps.length;
  const doneCount = steps.filter((step) => step.status === "done").length;
  const runningCount = steps.filter((step) => step.status === "running").length;

  // "Now running" replaces "Ultimately waiting on": prefer live terminal
  // leaves that are not already shown in the ordered queue list.
  const stepIds = new Set(steps.map((step) => step.blocker.id));
  const nowRunningSeen = new Set<string>();
  const nowRunning: IssueRelationIssueSummary[] = [];
  for (const blocker of [...terminalBlockers, ...chainBlockers]) {
    if (!liveIds.has(blocker.id)) continue;
    if (stepIds.has(blocker.id)) continue;
    if (nowRunningSeen.has(blocker.id)) continue;
    nowRunningSeen.add(blocker.id);
    nowRunning.push(blocker);
  }

  const queuedNoun = total === 1 ? "task" : "tasks";

  return (
    <div
      data-blocker-attention-state={blockerAttentionState}
      data-testid="issue-blocked-notice-live"
      className="mb-3 rounded-md border border-info/40 bg-info/10 px-3 py-2.5 text-ui text-foreground shadow-sm"
    >
      <div className="flex items-start gap-2">
        <span className="mt-1.5 flex h-4 w-4 shrink-0 items-center justify-center" aria-hidden>
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-info" />
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="space-y-1">
            <p className="font-medium leading-5">Waiting on live work</p>
            <p className="leading-5">
              Queued behind {total} {queuedNoun} being worked in order. This task
              resumes automatically when the chain is done. Comments still notify the
              assignee.
            </p>
          </div>

          <div className="space-y-1" data-testid="issue-blocked-notice-progress">
            <div className="text-caption font-medium text-info">
              {doneCount} of {total} done
              {runningCount > 0 ? ` · ${runningCount} running` : null}
            </div>
            <div
              role="progressbar"
              aria-label="Blocker chain progress"
              aria-valuemin={0}
              aria-valuenow={doneCount}
              aria-valuemax={total}
              className="flex h-2 w-full overflow-hidden rounded-full bg-info/15"
            >
              {steps.map(({ blocker, status }) => (
                <span
                  key={blocker.id}
                  className={cn(
                    "h-full border-r border-background/80 last:border-r-0",
                    status === "done"
                      ? "bg-info"
                      : status === "running"
                        ? "animate-pulse bg-info/70"
                        : "bg-info/25",
                  )}
                  style={{ width: `${100 / total}%` }}
                  title={`${blocker.identifier ?? blocker.id.slice(0, 8)}: ${status}`}
                  aria-hidden
                />
              ))}
            </div>
          </div>

          <div data-testid="issue-blocked-notice-steps">
            {steps.map(({ blocker, status }) => (
              <div key={blocker.id} className="flex items-stretch gap-2">
                <div className="flex w-3.5 flex-col items-center">
                  <span className="flex min-h-6 items-center">
                    <WaitingStepGlyph status={status} />
                  </span>
                  <span className="w-px flex-1 bg-info/30" aria-hidden />
                </div>
                <div className="min-w-0 pb-1.5">
                  <WaitingChipLink blocker={blocker} running={status === "running"} />
                </div>
              </div>
            ))}
            <div className="flex items-stretch gap-2">
              <div className="flex w-3.5 flex-col items-center">
                <span
                  className="mt-1.5 h-3 w-3 rounded-full border border-dashed border-info/60"
                  aria-hidden
                />
              </div>
              <div className="min-w-0 pb-0.5">
                <span className="inline-block rounded-md border border-dashed border-info/40 px-2 py-1 text-caption text-info">
                  This task — resumes automatically when the chain is done
                </span>
              </div>
            </div>
          </div>

          {nowRunning.length > 0 ? (
            <div
              data-testid="issue-blocked-notice-now-running"
              className="space-y-1 pt-0.5"
            >
              <div className="text-caption font-medium text-info">
                Now running
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                {nowRunning.map((blocker) => (
                  <WaitingChipLink key={blocker.id} blocker={blocker} running />
                ))}
              </div>
            </div>
          ) : null}

          {parkedBlockers.length > 0 ? (
            <div
              data-testid="issue-blocked-notice-parked-row"
              className="flex flex-wrap items-center gap-1.5 pt-0.5"
            >
              <span className="inline-flex items-center gap-1 text-caption font-medium text-warn">
                <Flag className="h-3 w-3" aria-hidden />
                Blocked by parked work
              </span>
              {parkedBlockers.map((blocker) => renderParkedChip(blocker))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function IssueBlockedNotice({
  issueId,
  issueStatus,
  blockers,
  allBlockers,
  liveIssueIds,
  blockerAttention,
  successfulRunHandoff,
  scheduledRetry,
  agentName,
}: {
  issueId?: string | null;
  issueStatus?: string;
  /** Unresolved blockers (drives the amber notice; unchanged). */
  blockers: IssueRelationIssueSummary[];
  /**
   * Full blocker list (resolved + unresolved). Used by the blue "Waiting on
   * live work" variant to render done steps and progress counts. Falls back to
   * {@link blockers} when not supplied.
   */
  allBlockers?: IssueRelationIssueSummary[];
  /** Company-wide set of issue ids with a queued/running run (own or blocker). */
  liveIssueIds?: ReadonlySet<string>;
  blockerAttention?: IssueBlockerAttention | null;
  successfulRunHandoff?: SuccessfulRunHandoffState | null;
  scheduledRetry?: IssueScheduledRetry | null;
  agentName?: string | null;
}) {
  if (issueStatus === "done" || issueStatus === "cancelled") return null;
  // A live run on this issue means an agent is already handling it — the
  // missing-disposition complaint only applies when the issue is stuck.
  // `hasLiveContinuation` is the server's view; `liveIssueIds` catches runs
  // that started after the issue payload was fetched.
  const issueHasLiveRun = Boolean(issueId && liveIssueIds?.has(issueId));
  const showSuccessfulRunHandoff =
    successfulRunHandoff != null
    && isSuccessfulRunHandoffRequired({ successfulRunHandoff, scheduledRetry })
    && !issueHasLiveRun;
  // Outstanding handoff + a live run on the issue: the alarm is suppressed, so
  // render the quiet in-flight line in its place rather than nothing at all.
  // The unpromoted-scheduled-retry carve-out keeps `showSuccessfulRunHandoff`
  // true, so the amber notice (and its "Retry now" control) still wins there.
  // This stands in for the handoff alarm only. When the issue also has
  // blockers, the blocker notice below is the stronger signal and owns the
  // slot, exactly as it did before this line existed.
  const handoffInFlightNotice =
    successfulRunHandoff != null
    && successfulRunHandoff.required === true
    && !showSuccessfulRunHandoff
    && (successfulRunHandoff.hasLiveContinuation || issueHasLiveRun)
      ? (
        <SuccessfulRunHandoffInFlightNotice
          liveRunId={successfulRunHandoff.liveRunId}
          assigneeAgentId={successfulRunHandoff.assigneeAgentId}
        />
      )
      : null;
  if (!showSuccessfulRunHandoff && blockers.length === 0 && issueStatus !== "blocked") {
    return handoffInFlightNotice;
  }
  const successfulRunRetryNow = showSuccessfulRunHandoff
    && issueId
    && scheduledRetry?.status === "scheduled_retry"
      ? { issueId, scheduledRetry }
      : null;

  const blockerLabel = blockers.length === 1 ? "the linked task" : "the linked tasks";
  const terminalBlockers = blockers
    .flatMap((blocker) => blocker.terminalBlockers ?? [])
    .filter((blocker, index, all) => all.findIndex((candidate) => candidate.id === blocker.id) === index);

  const isStalled = blockerAttention?.state === "stalled";
  const parkedBlockers = (() => {
    const seen = new Set<string>();
    const collected: IssueRelationIssueSummary[] = [];
    const sources: IssueRelationIssueSummary[] = [...blockers];
    for (const blocker of blockers) {
      for (const terminal of blocker.terminalBlockers ?? []) {
        sources.push(terminal);
      }
    }
    for (const blocker of sources) {
      if (!isAssignedBacklogBlocker(blocker)) continue;
      if (seen.has(blocker.id)) continue;
      seen.add(blocker.id);
      collected.push(blocker);
    }
    return collected;
  })();
  const showParkedRow = parkedBlockers.length > 0;
  const stalledLeafIdentifier =
    blockerAttention?.sampleStalledBlockerIdentifier ?? blockerAttention?.sampleBlockerIdentifier ?? null;
  const stalledLeafBlockers = (() => {
    const candidates: IssueRelationIssueSummary[] = [];
    for (const blocker of [...blockers, ...terminalBlockers]) {
      if (blocker.status !== "in_review") continue;
      if (candidates.some((existing) => existing.id === blocker.id)) continue;
      candidates.push(blocker);
    }
    if (stalledLeafIdentifier) {
      const preferred = candidates.find(
        (blocker) => (blocker.identifier ?? blocker.id) === stalledLeafIdentifier,
      );
      if (preferred) {
        return [preferred, ...candidates.filter((blocker) => blocker.id !== preferred.id)];
      }
    }
    return candidates;
  })();
  const showStalledRow = isStalled && stalledLeafBlockers.length > 0;

  // Rule C (PAP-13554 / plan §Rule C): when the issue is `blocked` and a
  // blocker edge is genuinely not done, a human comment does NOT reopen it —
  // the reopen gate keeps it blocked. `blockers` here is the *unresolved* set
  // (status ≠ done/cancelled), so a non-empty list on a `blocked` issue is
  // exactly the case the human's message can't move to todo. Done-but-pending-
  // finalize blockers are `done`, so they fall out of this set and into the
  // Rule B reopen path — we must not claim "a message won't reopen" for those.
  // Name the deepest unresolved leaf (prefer terminal leaves) with its status
  // so "I sent a message and nothing happened" can't recur silently.
  const responsibleName = agentName ?? "the assignee";
  const reopenSuppressed = issueStatus === "blocked" && !isStalled && blockers.length > 0;
  const unresolvedLeafBlockers = (() => {
    if (!reopenSuppressed) return [] as IssueRelationIssueSummary[];
    const seen = new Set<string>();
    const collected: IssueRelationIssueSummary[] = [];
    for (const blocker of blockers) {
      const terminals = (blocker.terminalBlockers ?? []).filter(
        (leaf) => leaf.status !== "done" && leaf.status !== "cancelled",
      );
      const leaves = terminals.length > 0 ? terminals : [blocker];
      for (const leaf of leaves) {
        if (seen.has(leaf.id)) continue;
        seen.add(leaf.id);
        collected.push(leaf);
      }
    }
    return collected;
  })();
  const reopenSuppressedLeaf = unresolvedLeafBlockers[0] ?? null;
  const reopenSuppressedLeafId = reopenSuppressedLeaf
    ? reopenSuppressedLeaf.identifier ?? reopenSuppressedLeaf.id.slice(0, 8)
    : null;
  const reopenSuppressedLeafStatus = reopenSuppressedLeaf
    ? reopenSuppressedLeaf.status.replace(/_/g, " ")
    : null;
  const reopenSuppressedOtherCount = Math.max(unresolvedLeafBlockers.length - 1, 0);

  const renderBlockerChip = (blocker: IssueRelationIssueSummary) => {
    const recoveryAction = blocker.activeRecoveryAction ?? null;
    return (
      <BlockerLink
        key={blocker.id}
        blocker={blocker}
        className="inline-flex max-w-full items-center gap-1 rounded-md border border-warn/40 bg-background/80 px-2 py-1 font-mono text-caption text-foreground transition-colors hover:border-warn hover:bg-warn/10 hover:underline"
      >
        <StatusIcon status={blocker.status} className="h-3 w-3" />
        <span>{blocker.identifier ?? blocker.id.slice(0, 8)}</span>
        <span className="max-w-72 truncate font-sans text-caption text-warn">
          {blocker.title}
        </span>
        {recoveryAction ? (
          <BlockerRecoveryIndicator
            action={recoveryAction}
            scheduledRetry={blocker.scheduledRetry}
          />
        ) : null}
      </BlockerLink>
    );
  };

  // Blue "Waiting on live work" variant: the blocker chain is a healthy plan
  // executing in order and something in it is live. `covered` is
  // the only state that goes blue — stalled / needs_attention / none keep the
  // amber notice byte-for-byte. The successful-run handoff notice is about this
  // task's own finished run, so it always keeps its amber priority styling.
  const liveIds = liveIssueIds ?? EMPTY_LIVE_IDS;
  const chainBlockers = allBlockers ?? blockers;
  const hasLiveWaitingBlocker = [...chainBlockers, ...terminalBlockers].some((blocker) => (
    liveIds.has(blocker.id)
  ));
  const waitingOnLiveWork =
    !showSuccessfulRunHandoff
    && blockerAttention?.state === "covered"
    && chainBlockers.length > 0
    && hasLiveWaitingBlocker;

  if (waitingOnLiveWork) {
    return (
      <WaitingOnLiveWorkNotice
        blockerAttentionState={blockerAttention?.state}
        chainBlockers={chainBlockers}
        terminalBlockers={terminalBlockers}
        liveIds={liveIds}
        parkedBlockers={showParkedRow ? parkedBlockers : []}
        renderParkedChip={renderBlockerChip}
      />
    );
  }

  return (
    <div
      data-blocker-attention-state={blockerAttention?.state}
      data-successful-run-handoff={showSuccessfulRunHandoff ? "required" : undefined}
      data-testid="issue-blocked-notice"
      className="mb-3 rounded-md border border-warn/40 bg-warn/10 px-3 py-2.5 text-ui text-foreground shadow-sm"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
        <div className="min-w-0 space-y-1.5">
          {showSuccessfulRunHandoff ? (
            <>
              <p className="font-medium leading-5">This task still needs a next step.</p>
              <p className="leading-5">
                A run finished successfully, but the task is still open. Paperclip needs someone to choose
                what happens next.
              </p>
              <ul className="list-disc space-y-1 pl-5 text-caption leading-5">
                <li>Mark it done or cancelled.</li>
                <li>Send it for review or ask for input.</li>
                <li>Record what is blocking it and who owns that blocker.</li>
                <li>Delegate follow-up work or queue a continuation.</li>
              </ul>
              <div className="flex flex-wrap gap-1.5 text-caption">
                {successfulRunHandoff.sourceRunId && successfulRunHandoff.assigneeAgentId ? (
                  <Link
                    to={teamsDetailUrl("run", successfulRunHandoff.sourceRunId)}
                    className="rounded-md border border-warn/40 bg-background/80 px-2 py-1 font-mono text-foreground hover:border-warn hover:bg-warn/10 hover:underline"
                  >
                    run {successfulRunHandoff.sourceRunId.slice(0, 8)}
                  </Link>
                ) : successfulRunHandoff.sourceRunId ? (
                  <span className="rounded-md border border-warn/40 bg-background/80 px-2 py-1 font-mono text-foreground">
                    run {successfulRunHandoff.sourceRunId.slice(0, 8)}
                  </span>
                ) : null}
                <span className="rounded-md border border-warn/40 bg-background/80 px-2 py-1 text-foreground">
                  Asked {agentName ?? "the assignee"} to choose the next step
                </span>
              </div>
              {successfulRunHandoff.detectedProgressSummary ? (
                <p className="text-caption leading-5 text-warn">
                  Detected progress: {successfulRunHandoff.detectedProgressSummary}
                </p>
              ) : null}
              {successfulRunRetryNow ? (
                <SuccessfulRunRetryNowControl
                  issueId={successfulRunRetryNow.issueId}
                  scheduledRetry={successfulRunRetryNow.scheduledRetry}
                />
              ) : null}
            </>
          ) : null}
          {showSuccessfulRunHandoff && (blockers.length > 0 || issueStatus === "blocked") ? (
            <div className="border-t border-warn/30 pt-1.5" />
          ) : null}
          {blockers.length > 0 || issueStatus === "blocked" ? (
            <>
              <p className="leading-5">
                {blockers.length > 0
                  ? isStalled
                    ? stalledLeafBlockers.length > 1
                      ? <>Work on this task is blocked by {blockerLabel}, but the chain is stalled in review without a clear next step. Resolve the stalled reviews below or remove them as blockers.</>
                      : <>Work on this task is blocked by {blockerLabel}, but the chain is stalled in review without a clear next step. Resolve the stalled review below or remove it as a blocker.</>
                    : reopenSuppressed
                      ? <>A message won&rsquo;t restart this task yet — it stays blocked by {blockerLabel} until {blockers.length === 1 ? "it is" : "they are"} done, then it reopens automatically. Comments still notify {responsibleName} for questions or triage in the meantime.</>
                      : <>Work on this task is blocked by {blockerLabel} until {blockers.length === 1 ? "it is" : "they are"} complete. Comments still notify the assignee for questions or triage.</>
                  : <>Work on this task is blocked until someone moves it back to To do. Comments still notify the assignee for questions or triage.</>}
              </p>
              {reopenSuppressed && reopenSuppressedLeafId ? (
                <p
                  data-testid="issue-blocked-notice-reopen-suppressed"
                  className="text-caption font-medium leading-5"
                >
                  Still blocked by{" "}
                  <span className="font-mono">{reopenSuppressedLeafId}</span>
                  {reopenSuppressedLeafStatus ? <> ({reopenSuppressedLeafStatus})</> : null}
                  {reopenSuppressedOtherCount > 0
                    ? ` and ${reopenSuppressedOtherCount} other ${
                        reopenSuppressedOtherCount === 1 ? "task" : "tasks"
                      }`
                    : null}
                  .
                </p>
              ) : null}
              {blockers.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {blockers.map(renderBlockerChip)}
                </div>
              ) : null}
              {showStalledRow ? (
                <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                  <span className="text-caption font-medium text-warn">
                    Stalled in review
                  </span>
                  {stalledLeafBlockers.map(renderBlockerChip)}
                </div>
              ) : terminalBlockers.length > 0 ? (
                <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                  <span className="text-caption font-medium text-warn">
                    Ultimately waiting on
                  </span>
                  {terminalBlockers.map(renderBlockerChip)}
                </div>
              ) : null}
              {showParkedRow ? (
                <div
                  data-testid="issue-blocked-notice-parked-row"
                  className="flex flex-wrap items-center gap-1.5 pt-0.5"
                >
                  <span className="inline-flex items-center gap-1 text-caption font-medium text-warn">
                    <Flag className="h-3 w-3" aria-hidden />
                    Blocked by parked work
                  </span>
                  {parkedBlockers.map(renderBlockerChip)}
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
