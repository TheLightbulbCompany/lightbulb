// apps/frontend/src/components/teams/runs/useRunTranscript.ts
//
// Full-scrollback transcript read for the run-detail panel.
//
// Unlike the dashboard-card hook (useRunLog: bounded tail, last 12 lines),
// the detail panel wants the whole log — bounded by the BFF's 256 KiB
// per-read ceiling. Rather than walking nextOffset with sequential reads,
// do ONE bounded read aimed at the tail: the run row carries `logBytes`, so
// when the log exceeds the cap we start the read at `logBytes - cap` and
// surface a truncation flag for the panel to render a notice.
"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { RunLogResponse } from "@/components/teams/dashboard/hooks/useRunLog";
import { runLogToMessages } from "./runLogToMessages";
import type { HistoryMessage } from "@lightbulb/shared";

/** Matches the BFF's RUN_LOG_MAX_LIMIT_BYTES ceiling. */
export const RUN_TRANSCRIPT_LIMIT_BYTES = 262_144;

/** Poll cadence while the run is active — same beat as the live-runs feed. */
const REFRESH_MS = 15_000;

export function useRunTranscript(
  runId: string,
  opts: { active: boolean; logBytes?: number | null },
): { messages: HistoryMessage[]; truncated: boolean; isLoading: boolean } {
  const { read } = useTeamsApi();
  const truncated =
    typeof opts.logBytes === "number" && opts.logBytes > RUN_TRANSCRIPT_LIMIT_BYTES;
  const offset = truncated
    ? (opts.logBytes as number) - RUN_TRANSCRIPT_LIMIT_BYTES
    : 0;
  const path = truncated
    ? `/heartbeat-runs/${runId}/log?offset=${offset}&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`
    : `/heartbeat-runs/${runId}/log?limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`;

  const { data, isLoading } = read<RunLogResponse>(path, {
    refreshInterval: opts.active ? REFRESH_MS : 0,
    dedupingInterval: 2000,
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });

  const messages = useMemo(() => {
    let content = data?.content;
    if (!content) return [];
    if (truncated) {
      // A tail read's byte offset can land mid-NDJSON-record; drop everything
      // up to and including the first newline so parsing starts at the first
      // complete record. Trimming raw content (not the first parsed line) is
      // the faithful fix — a record's chunk can contain embedded newlines.
      const firstNewline = content.indexOf("\n");
      content = firstNewline === -1 ? "" : content.slice(firstNewline + 1);
    }
    return runLogToMessages(content);
  }, [data?.content, truncated]);

  return { messages, truncated, isLoading };
}
