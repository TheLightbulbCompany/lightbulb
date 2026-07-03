// apps/frontend/src/components/teams/dashboard/hooks/useLiveRuns.ts

// Live-agent-runs feed for the Dashboard "Agents" panel.
//
// Ported (semantics only) from upstream Paperclip's ActiveAgentsPanel data
// path (paperclip/ui/src/components/ActiveAgentsPanel.tsx +
// api/heartbeats.ts) (MIT, (c) 2025 Paperclip AI). We deliberately DO NOT
// port upstream's raw-log-byte polling hook (useLiveRunTranscripts) — that
// drives a streaming transcript off `/heartbeat-runs/{id}/log` which is out
// of scope for v1. This hook only fetches the live-run METADATA list from
// `/inbox/live-runs` and lets SWR refresh it on an interval so the "Live"
// status stays current.

import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * One live (or recently-finished) agent run, as returned by
 * `GET /teams/inbox/live-runs` → upstream `/api/companies/{id}/live-runs`.
 *
 * Mirrors the projection the upstream live-runs query selects (see
 * paperclip/server/src/routes/agents.ts `/companies/:companyId/live-runs`).
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
  /** Liveness classification (e.g. "healthy" | "stalled"); display-only. */
  livenessState?: string | null;
}

/** The `/inbox/live-runs` envelope is array | {items} | {runs}. Normalize. */
function normalizeRuns(data: unknown): LiveRun[] {
  if (Array.isArray(data)) return data as LiveRun[];
  if (data && typeof data === "object") {
    const obj = data as { items?: unknown; runs?: unknown };
    if (Array.isArray(obj.items)) return obj.items as LiveRun[];
    if (Array.isArray(obj.runs)) return obj.runs as LiveRun[];
  }
  return [];
}

/** A run is "active" (pulsing dot) while queued or running. */
export function isRunActive(run: LiveRun): boolean {
  return run.status === "queued" || run.status === "running";
}

// SWR refresh cadence for the live feed. Upstream polled its dashboard log at
// 15s; we keep the same beat for the list so "Live now" → "Finished …"
// transitions land within ~15s without hammering the BFF.
const LIVE_RUNS_REFRESH_MS = 15_000;

export function useLiveRuns(): {
  runs: LiveRun[];
  isLoading: boolean;
  isError: boolean;
} {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<unknown>("/inbox/live-runs", {
    refreshInterval: LIVE_RUNS_REFRESH_MS,
    // The read layer's mount-level dedup window equals the poll cadence,
    // which would race and swallow alternate polls — restore SWR's default
    // so every tick actually fetches.
    dedupingInterval: 2000,
  });
  return {
    runs: normalizeRuns(data),
    isLoading,
    isError: !!error,
  };
}
