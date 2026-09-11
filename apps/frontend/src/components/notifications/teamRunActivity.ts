"use client";

import { useSyncExternalStore } from "react";
import { TEAMS_WORKING_STATUSES } from "./runToastReducer";

const activeRunIds = new Set<string>();
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function setActive(runId: string, active: boolean): void {
  const changed = active ? !activeRunIds.has(runId) : activeRunIds.has(runId);
  if (!changed) return;
  if (active) activeRunIds.add(runId);
  else activeRunIds.delete(runId);
  emit();
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Keep the non-blocking global activity indicator in sync with Paperclip run events. */
export function trackTeamRunActivity(eventName: string, payload: unknown): void {
  if (eventName !== "teams.heartbeat.run.queued" && eventName !== "teams.heartbeat.run.status") return;
  const data = (payload ?? {}) as Record<string, unknown>;
  const runId = readString(data.runId);
  if (!runId) return;
  const status = eventName === "teams.heartbeat.run.queued" ? "queued" : readString(data.status);
  if (!status) return;
  setActive(runId, TEAMS_WORKING_STATUSES.has(status));
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): number {
  return activeRunIds.size;
}

export function useActiveTeamRunCount(): number {
  return useSyncExternalStore(subscribe, snapshot, () => 0);
}
