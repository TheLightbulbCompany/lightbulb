// apps/frontend/src/components/teams/issues/task-chat/TaskChatLiveRunPill.tsx

// Ported from upstream Paperclip's TaskChatLiveRunPill
// (paperclip/ui/src/components/task-chat/TaskChatLiveRunPill.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI): the "Working for 12s" status line above a live
// run, settling to "Worked" / "Stopped" once it terminates.
//
// Adaptations: shimmer-text -> animate-pulse, brand tokens, useSecondTick
// returns the ticking clock (render stays pure), formatDurationWords and
// isTerminalRunStatus inlined verbatim. The tool-count summary is dropped: the
// streamed transcript (TaskChatLiveTail + transcript-adapter) is part B, pending
// a Hermes facade events route — today's hermes_gateway logs carry no deltas.
//
// TaskChatRunTail (Lightbulb): the run row stock CommentThread takes as its
// `liveRunSlot` — this pill, the upstream TaskChatThread empty-tail status copy
// ("Waiting to start..." / currentStatusMessage / "Waiting for transcript..."),
// and Stop (POST /heartbeat-runs/{id}/cancel), which stock's task chat puts on
// the composer.

"use client";

import { useEffect, useState } from "react";
import { Loader2, Square } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import type { IssueLiveRun } from "@/components/teams/shared/types";

const TERMINAL_STATUSES = new Set(["failed", "timed_out", "cancelled", "interrupted", "succeeded"]);

export function isTerminalRunStatus(status: string | undefined | null): boolean {
  return status != null && TERMINAL_STATUSES.has(status);
}

/** Upstream lib/issue-chat-messages formatDurationWords (verbatim). */
function formatDurationWords(ms: number | null) {
  if (ms === null || !Number.isFinite(ms) || ms <= 0) return null;
  const totalSeconds = Math.max(1, Math.round(ms / 1000));
  if (totalSeconds < 60) {
    return `${totalSeconds} second${totalSeconds === 1 ? "" : "s"}`;
  }
  const totalMinutes = Math.round(totalSeconds / 60);
  if (totalMinutes < 60) {
    return `${totalMinutes} minute${totalMinutes === 1 ? "" : "s"}`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) {
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  return `${hours} hour${hours === 1 ? "" : "s"} ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/** Upstream useSecondTick, returning the ticking clock so render stays pure. */
function useSecondTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

export function TaskChatLiveRunPill({
  status,
  startedAtMs,
  finishedAtMs,
}: {
  status: string;
  /** Run start (startedAt, falling back to createdAt) in ms, or null if unknown. */
  startedAtMs: number | null;
  /** Run finish in ms once terminal; drives the settled elapsed readout. */
  finishedAtMs?: number | null;
}) {
  const active = !isTerminalRunStatus(status);
  const now = useSecondTick(active && startedAtMs != null);
  const elapsedMs =
    startedAtMs == null ? null : (active ? now : finishedAtMs ?? now) - startedAtMs;
  const elapsed = elapsedMs != null ? formatDurationWords(elapsedMs) : null;
  const failed = ["failed", "timed_out", "cancelled", "interrupted"].includes(status);
  const verb = active ? "Working" : failed ? "Stopped" : "Worked";
  const suffix = elapsed ? `for ${elapsed}` : null;

  return (
    <div className="flex min-w-0 items-center gap-2 py-2" data-testid="task-chat-live-run-pill">
      <span className="inline-flex items-center gap-2 text-ui font-medium" style={{ color: "var(--midnight-soft)" }}>
        <span className="flex size-5 shrink-0 items-center justify-center">
          {active ? (
            <Loader2 className="size-4 animate-spin" style={{ color: "var(--midnight-muted)" }} />
          ) : (
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: failed ? "var(--midnight-faint)" : "var(--ok)" }}
            />
          )}
        </span>
        {active ? <span className="animate-pulse">{verb}</span> : verb}
      </span>
      {suffix ? (
        <span className="text-caption" style={{ color: "var(--midnight-muted)" }}>
          {suffix}
        </span>
      ) : null}
    </div>
  );
}

function toMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/** The live run row: pill + status copy + Stop. */
export function TaskChatRunTail({ run, onStop }: { run: IssueLiveRun; onStop?: () => Promise<void> }) {
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const stop = async () => {
    if (!onStop || stopping) return;
    setStopping(true);
    setStopError(null);
    try {
      await onStop();
    } catch (err) {
      setStopError(err instanceof Error ? err.message : "Unable to stop. Try again.");
    } finally {
      setStopping(false);
    }
  };
  return (
    <div
      className="rounded-sm px-3 py-1"
      style={{ border: "1px solid var(--edge)" }}
      data-testid="task-chat-live-transcript"
    >
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <TaskChatLiveRunPill
            status={run.status}
            startedAtMs={toMs(run.startedAt) ?? toMs(run.createdAt)}
            finishedAtMs={toMs(run.finishedAt)}
          />
        </div>
        {onStop ? (
          <EditorialButton type="button" size="sm" variant="secondary" disabled={stopping} onClick={() => void stop()}>
            {stopping ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Square className="h-3.5 w-3.5 fill-current" aria-hidden />}
            {stopping ? "Stopping…" : "Stop"}
          </EditorialButton>
        ) : null}
      </div>
      <div className="px-1 pb-1 text-caption" style={{ color: "var(--midnight-muted)" }}>
        {run.status === "queued" ? "Waiting to start..." : run.currentStatusMessage || "Waiting for transcript..."}
      </div>
      {stopError ? (
        <p role="alert" className="px-1 pb-1 text-caption" style={{ color: "var(--err)" }}>
          {stopError}
        </p>
      ) : null}
    </div>
  );
}
