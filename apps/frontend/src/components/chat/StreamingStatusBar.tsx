import * as React from "react";
import { AgentGlyph } from "./MessageList";

/**
 * Run telemetry, shown beneath the transcript.
 *
 * This used to render ONLY in silent gaps (`!isStreaming && active`), on the
 * reasoning that while tokens are arriving the transcript is its own status.
 * That argument defeats a duplicate status LABEL; it does not defeat a clock.
 * Elapsed time is orthogonal to the transcript — the transcript says WHAT is
 * happening, this says HOW LONG it has been happening — and hiding it during
 * streaming removed it from exactly the long runs where "has this hung?" gets
 * asked. It also made a post-run recap structurally impossible: the bar had
 * unmounted by the time the run settled.
 *
 * It still does NOT narrate the current step. ToolActivityTrace does that in
 * place, in plain language, where the work actually appears.
 */

/**
 * The gateway's closed startup-phase enum, plus our two compaction states, in
 * the owner's language. Anything unlisted falls back to "Working…" rather than
 * printing a raw enum value at someone who does not know what a worktree is.
 *
 * Only three of the startup phases reach our fleet — preparing_workspace,
 * preparing_context and starting_model. The worktree/setup/environment phases
 * come from project sessions and worker environments, neither of which we run;
 * they are here because the enum is closed and the entries are free, not
 * because we expect them.
 */
const PHASE_LABEL: Record<string, string> = {
  preparing_workspace: "Preparing the workspace…",
  naming_worktree: "Preparing the workspace…",
  creating_worktree: "Preparing the workspace…",
  running_setup: "Running setup…",
  provisioning_environment: "Provisioning the environment…",
  preparing_context: "Reading the conversation…",
  starting_model: "Starting the model…",
  // Compaction is a stall of tens of seconds that otherwise looks like a hang;
  // a failed one is worth saying out loud because the reply that follows may
  // have lost context.
  compacting: "Compacting the conversation…",
  compaction_failed: "Couldn't compact the conversation — continuing anyway…",
};

function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

interface StreamingStatusBarProps {
  /** Server-truth "a run is active" (useAgentChat's `busy`). */
  active?: boolean;
  /** Raw phase enum; mapped to copy here, never rendered directly. */
  runPhase?: string | null;
  /**
   * Epoch ms of the send that started this run. Null for a run this client did
   * not start (adopted mid-run, or picked up after a reconnect) — there is no
   * honest start time for those, so no clock is shown rather than a wrong one.
   */
  runStartedAt?: number | null;
  /** Set once a run settles successfully; sticky until the next send. */
  lastTurnRecap?: { runtimeMs: number } | null;
}

export function StreamingStatusBar({
  active,
  runPhase = null,
  runStartedAt = null,
  lastTurnRecap = null,
}: StreamingStatusBarProps) {
  const running = Boolean(active);
  const [now, setNow] = React.useState(() => Date.now());

  // Tick only while a run is live AND we know when it started. An interval
  // behind a settled recap is pure wakeups.
  React.useEffect(() => {
    if (!running || runStartedAt === null) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running, runStartedAt]);

  if (!running && !lastTurnRecap) return null;

  // A live run always outranks a previous run's recap.
  const label = running
    ? ((runPhase && PHASE_LABEL[runPhase]) ?? "Working…")
    : `Done in ${formatElapsed(lastTurnRecap?.runtimeMs ?? 0)}`;

  return (
    <div
      data-testid="streaming-status-bar"
      className="flex justify-center pointer-events-none px-4 pt-2 pb-1"
      role="status"
      // The label changes rarely — a phase transition or the final recap — and
      // is worth announcing. The clock inside it is not; see the elapsed span.
      aria-live="polite"
    >
      <div className="flex items-center gap-2 text-xs text-[var(--midnight-muted)] bg-[var(--cream-deep)]/80 backdrop-blur-sm rounded-full px-3 py-1.5 shadow-sm">
        <AgentGlyph state={running ? "thinking" : "idle"} size={16} />
        <span className="font-medium">{label}</span>
        {running && runStartedAt !== null && (
          <span
            data-testid="status-elapsed"
            // REQUIRED: this ticks once per second, and a live region
            // containing a 1 Hz counter floods assistive tech and buries the
            // label's own announcements. The label carries the news.
            aria-live="off"
            className="tabular-nums opacity-70"
          >
            {formatElapsed(Math.max(0, now - runStartedAt))}
          </span>
        )}
      </div>
    </div>
  );
}
