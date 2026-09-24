// apps/frontend/src/components/teams/issues/task-chat/useLiveRunTranscript.ts

// Ported (semantics) from upstream Paperclip's useLiveRunTranscripts
// (paperclip/ui/src/components/transcript/useLiveRunTranscripts.ts @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI).
//
// Adaptation: upstream merges an incremental offset poller with the company
// live-events WebSocket (`heartbeat.run.log` chunks). We do not expose
// Paperclip's socket, so this is the poller half only — one bounded tail read
// of `GET /teams/heartbeat-runs/{id}/log` through SWR, re-read every 3s while
// the run is live (TeamsEventsProvider revalidates the live-runs list that
// decides when it is). Same 256 KiB tail rule as runs/useRunTranscript.

"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { RunLogResponse } from "@/components/teams/dashboard/hooks/useRunLog";
import { RUN_TRANSCRIPT_LIMIT_BYTES } from "@/components/teams/runs/useRunTranscript";
import { buildTranscript, parseRunLogContent, type TranscriptEntry } from "./transcript-adapter";

const LIVE_REFRESH_MS = 3_000;

export function useLiveRunTranscript(
  runId: string | null,
  opts: { active: boolean; logBytes?: number | null },
): TranscriptEntry[] {
  const { read } = useTeamsApi();
  const truncated = typeof opts.logBytes === "number" && opts.logBytes > RUN_TRANSCRIPT_LIMIT_BYTES;
  const offset = truncated ? (opts.logBytes as number) - RUN_TRANSCRIPT_LIMIT_BYTES : 0;
  const path = runId
    ? `/heartbeat-runs/${runId}/log?offset=${offset}&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`
    : null;
  const { data } = read<RunLogResponse>(path, {
    refreshInterval: opts.active ? LIVE_REFRESH_MS : 0,
    dedupingInterval: 1_000,
    shouldRetryOnError: false,
  });
  return useMemo(() => {
    let content = data?.content ?? "";
    // A tail read can start mid-record; drop the partial first line.
    if (truncated) {
      const nl = content.indexOf("\n");
      content = nl === -1 ? "" : content.slice(nl + 1);
    }
    return content ? buildTranscript(parseRunLogContent(content)) : [];
  }, [data?.content, truncated]);
}
