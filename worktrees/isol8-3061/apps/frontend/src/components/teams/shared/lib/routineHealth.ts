// apps/frontend/src/components/teams/shared/lib/routineHealth.ts
//
// Circuit-breaker auto-pause state. Lives in shared/ (not panels/) because
// both the routines LIST panel and the routine DETAIL panel need it, and
// those are separate next/dynamic() code-split chunks (see
// TeamsPanelRouter's PANEL_LOADERS.routines vs DETAIL_LOADERS.routine) — a
// cross-panel import would pull the whole detail-panel module graph
// (EditRoutineDialog, TriggerDialog, ...) into the list chunk.

// The breaker trips a routine to `status: "paused"` after repeated failed
// runs and stamps `autoPausedAt`; a human pause via the toggle/edit dialog
// never sets that field. This is the single source of truth both the list
// row and the detail banner use to tell the two apart.
export function isAutoPaused(r: {
  status?: string | null;
  autoPausedAt?: string | null;
}): boolean {
  return r.status === "paused" && !!r.autoPausedAt;
}
