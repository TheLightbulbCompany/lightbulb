// Shared live-run types + pure display helpers for the Teams surfaces
// (Inbox rows, Dashboard active-agents, issue detail). The data source is
// the `/inbox/live-runs` feed (see dashboard/hooks/useLiveRuns.ts, which
// re-exports everything here for its existing consumers); these helpers are
// hook-free so shared components can import them without depending on a
// feature directory.

/** How healthy an in-flight run looks: producing output vs gone quiet. */
export type LiveRunActivity = "running" | "stalled";

/**
 * Output-silence watchdog upstream decorates each live run with (see
 * paperclip recovery/service.buildRunOutputSilence). `level` is the
 * authoritative "is this running agent stalled?" signal — driven by how long
 * the process has been silent, not a local heuristic. Only `level` +
 * `silenceAgeMs` are consumed here; the row carries more fields we ignore.
 */
export interface RunOutputSilence {
  level?: "ok" | "suspicious" | "critical" | "snoozed" | "not_applicable" | null;
  /** Milliseconds since the run last produced output (null when not running). */
  silenceAgeMs?: number | null;
}

/**
 * One live (or recently-finished) agent run, as returned by
 * `GET /teams/inbox/live-runs` → upstream `/api/companies/{id}/live-runs`.
 *
 * Mirrors the projection the upstream live-runs query selects (see
 * paperclip-fork server/src/routes/agents.ts `/companies/:companyId/live-runs`).
 * Every field is treated as optional/nullable defensively because the BFF
 * forwards the upstream JSON verbatim and the row shape has drifted across
 * upstream versions. `status` is a free string (queued | running | … ).
 *
 * NOTE: this row carries NO in-progress transcript / output text. The agent
 * transcript lives only behind the raw-log endpoint, which we do not consume
 * here — streaming-transcript on these cards is a deliberate follow-up.
 */
export interface LiveRun {
  id: string;
  status?: string | null;
  agentId?: string | null;
  agentName?: string | null;
  issueId?: string | null;
  createdAt?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  /** Short "what the agent is doing next" hint upstream stamps on the run. */
  nextAction?: string | null;
  /**
   * Per-run liveness classification stamped by upstream's heartbeat watchdog.
   * Real enum (paperclip heartbeatRuns.livenessState): "advanced" |
   * "completed" | "plan_only" | "empty_response" | "blocked" | "failed" |
   * "needs_followup" | null. Display-only.
   */
  livenessState?: string | null;
  /** Human-readable reason paired with `livenessState` (e.g. "Planned without acting"). */
  livenessReason?: string | null;
  /** Live-run output-silence watchdog; the "stalled vs running" source. */
  outputSilence?: RunOutputSilence | null;
}

/** A run is "active" (pulsing dot) while queued or running. */
export function isRunActive(run: LiveRun): boolean {
  return run.status === "queued" || run.status === "running";
}

/**
 * Format a silence duration for tooltips. Mirrors upstream `formatSilenceAgeMs`
 * (paperclip source-resolved-watchdog-fold.ts) so our copy reads the same.
 */
export function formatSilenceAgeMs(ms: number | null | undefined): string | null {
  if (!ms || ms <= 0) return null;
  const totalMinutes = Math.floor(ms / 60_000);
  if (totalMinutes < 1) return "under 1 minute";
  if (totalMinutes < 60) return `${totalMinutes} minute${totalMinutes === 1 ? "" : "s"}`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${hours}h ${minutes}m`;
}

/**
 * Classify an active run as healthy-"running" or "stalled". Mirrors upstream's
 * live-run silence watchdog (RUN_OUTPUT_SILENCE_COPY in IssueRunLedger): the
 * `suspicious` + `critical` output-silence levels are the "agent has gone
 * quiet" signal the Dashboard/Inbox must distinguish from a healthy run.
 */
export function liveRunActivity(run: LiveRun): LiveRunActivity {
  const level = run.outputSilence?.level;
  return level === "suspicious" || level === "critical" ? "stalled" : "running";
}

/** Tooltip copy describing a live run's state (for the row/dot title). */
export function liveRunTitle(run: LiveRun): string {
  const level = run.outputSilence?.level;
  const age = formatSilenceAgeMs(run.outputSilence?.silenceAgeMs);
  if (level === "critical") {
    return age ? `Stale run — no output for ${age}` : "Stale run — no recent output";
  }
  if (level === "suspicious") {
    return age ? `Silence watch — quiet for ${age}` : "Silence watch — output has gone quiet";
  }
  // A queued run hasn't started — never announce it as running. (Upstream's
  // watchdog only stamps ok/suspicious/critical on status === "running", so a
  // queued run always reaches this branch.)
  if (run.status === "queued") return "Queued — waiting to start";
  const next = run.nextAction?.trim();
  return next ? `Running — ${next}` : "Running now";
}

/** Per-issue live-run state for row indicators: activity + tooltip copy. */
export interface LiveIssueEntry {
  activity: LiveRunActivity;
  title: string;
}

/**
 * Severity of a live run's silence signal, for picking which of an issue's
 * runs to surface: critical > suspicious > healthy.
 */
function silenceSeverity(run: LiveRun): number {
  const level = run.outputSilence?.level;
  if (level === "critical") return 2;
  if (level === "suspicious") return 1;
  return 0;
}

/**
 * Fold the live-runs feed into a per-issue map for row indicators. When an
 * issue has more than one live run, the most severe silence signal wins
 * (critical > suspicious > healthy) regardless of feed order — so a hung run
 * is never masked by a healthy or milder-stalled one.
 */
export function buildLiveIssueState(runs: LiveRun[]): Map<string, LiveIssueEntry> {
  const map = new Map<string, LiveIssueEntry>();
  const severity = new Map<string, number>();
  for (const run of runs) {
    if (!run.issueId || !isRunActive(run)) continue;
    const rank = silenceSeverity(run);
    const existing = severity.get(run.issueId);
    if (existing === undefined || rank > existing) {
      severity.set(run.issueId, rank);
      map.set(run.issueId, { activity: liveRunActivity(run), title: liveRunTitle(run) });
    }
  }
  return map;
}
