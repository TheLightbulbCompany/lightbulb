// apps/frontend/src/components/teams/shared/lib/runStatus.ts
//
// Canonical run-status vocabulary, shared by every surface that colors an
// agent/routine run's free-string status: RunDetailPanel's own header (the
// vocabulary was first modeled there) and RoutineDetailPanel's Run history
// rows, which used to carry a second, incomplete copy of this same
// classification (no `running`/`queued`/`scheduled_retry`/`timed_out`
// handling — everything but succeeded/failed fell through to `--midnight-muted`,
// including an in-flight OR a genuinely failed timed-out run). One
// classification, reused, so the two surfaces can't drift again.

/** Free upstream string — color the known vocabulary, default to mute. */
export function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--midnight)";
  if (s === "failed" || s === "error" || s === "cancelled" || s === "timed_out") return "var(--err)";
  if (s === "running" || s === "queued" || s === "scheduled_retry") return "var(--midnight)";
  return "var(--midnight-muted)";
}

/**
 * True while a run is still in flight — running, queued to start, or a
 * scheduled retry waiting to fire. This is also the gate for showing a
 * `LiveRunDot`: a terminal row (succeeded/failed/etc.) never gets one, so a
 * retry's own row reads as live while its preceding failed row stays static.
 */
export function isActiveStatus(status: string | null | undefined): boolean {
  const s = (status ?? "").toLowerCase();
  return s === "running" || s === "queued" || s === "scheduled_retry";
}
