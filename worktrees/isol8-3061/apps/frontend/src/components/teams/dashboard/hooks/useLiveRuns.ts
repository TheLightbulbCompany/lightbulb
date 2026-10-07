// apps/frontend/src/components/teams/dashboard/hooks/useLiveRuns.ts

// Live-agent-runs feed for the Dashboard "Agents" panel.
//
// Ported (semantics only) from upstream Paperclip's agent-runs data
// path (paperclip/ui/src/components/ActiveAgentsPanel.tsx +
// api/heartbeats.ts) (MIT, (c) 2025 Paperclip AI). We deliberately DO NOT
// port upstream's raw-log-byte polling hook (useLiveRunTranscripts) — that
// drives a streaming transcript off `/heartbeat-runs/{id}/log` which is out
// of scope for v1. This hook only fetches the live-run METADATA list from
// `/inbox/live-runs` and lets SWR refresh it on an interval so the "Live"
// status stays current.
//
// The row types + pure display helpers (isRunActive, liveRunActivity,
// liveRunTitle, buildLiveIssueState, …) live in shared/lib/liveRuns.ts so
// shared components can use them without importing from this feature dir;
// they are re-exported here for this hook's existing consumers.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { LiveRun } from "@/components/teams/shared/lib/liveRuns";

export type {
  LiveRun,
  LiveRunActivity,
  LiveIssueEntry,
  RunOutputSilence,
} from "@/components/teams/shared/lib/liveRuns";
export {
  isRunActive,
  liveRunActivity,
  liveRunTitle,
  formatSilenceAgeMs,
  buildLiveIssueState,
} from "@/components/teams/shared/lib/liveRuns";

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
