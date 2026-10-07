// apps/frontend/src/components/teams/dashboard/hooks/useRunLog.ts
//
// Polled live-transcript read for the Dashboard "Agents" run cards.
//
// Ported (semantics only) from upstream Paperclip's useLiveRunTranscripts
// (paperclip/ui/src/components/transcript/useLiveRunTranscripts.ts) (MIT,
// (c) 2025 Paperclip AI). We deliberately ship a MUCH smaller v1:
//   * POLLED read only — no WebSocket realtime overlay, no incremental
//     byte-offset walking. Each poll re-reads a bounded tail from offset 0
//     and we slice the last N lines client-side. The in-progress run log is
//     small early on, and "recent activity" is all the card needs.
//   * No adapter/transcript-builder machinery. We extract the plain `chunk`
//     text from each NDJSON log record (matching upstream's
//     `parsePersistedLogContent`) and hand back the last N lines verbatim.
//
// Backed by the BFF route `GET /teams/heartbeat-runs/{id}/log` → upstream
// `GET /api/heartbeat-runs/{id}/log`, which returns
// `{ runId, store, logRef, content, nextOffset? }`.

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/** SWR refresh cadence for the live transcript. Matches the live-runs list
 *  feed (15s) so the card body and its status dot update on one beat. */
const RUN_LOG_REFRESH_MS = 15_000;

/** How many trailing log lines to surface in the compact card transcript. */
export const RUN_LOG_TAIL_LINES = 12;

/** Upstream log-read envelope (we only consume `content`). */
export interface RunLogResponse {
  content?: string | null;
}

/**
 * Parse the upstream log `content` blob into display lines.
 *
 * Upstream appends newline-delimited JSON records shaped
 * `{ ts, stream, chunk }` (see `parsePersistedLogContent`). We pull the
 * `chunk` text out of each record. Non-JSON / malformed lines are kept as
 * raw text so a store that ever writes plain lines still renders something
 * rather than dropping silently. Blank lines are filtered.
 */
export function parseLogLines(content: string): string[] {
  const out: string[] = [];
  for (const rawLine of content.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;
    let text = trimmed;
    if (trimmed.startsWith("{")) {
      try {
        const rec = JSON.parse(trimmed) as { chunk?: unknown };
        text = typeof rec.chunk === "string" ? rec.chunk : "";
      } catch {
        // Malformed JSON row — fall back to the raw line.
        text = trimmed;
      }
    }
    // A chunk may itself contain embedded newlines.
    for (const sub of text.split("\n")) {
      const subTrimmed = sub.replace(/\s+$/, "");
      if (subTrimmed.length > 0) out.push(subTrimmed);
    }
  }
  return out;
}

/**
 * Poll the raw log for a single run and return the trailing transcript lines.
 *
 * Gated to ACTIVE runs only: pass `active=false` and the hook holds SWR in a
 * paused state (no initial fetch, no revalidation) so finished/idle cards
 * never poll. The hook is always called (rules of hooks) — only the fetch is
 * gated.
 *
 * Degrades silently: on error or empty content `lines` is `[]` and the card
 * falls back to its existing "Working…" placeholder.
 */
export function useRunLog(
  runId: string,
  active: boolean,
): { lines: string[]; isLoading: boolean } {
  const { read } = useTeamsApi();
  const { data, isLoading } = read<RunLogResponse>(
    `/heartbeat-runs/${runId}/log`,
    {
      refreshInterval: active ? RUN_LOG_REFRESH_MS : 0,
      // The read layer's mount-level dedup window equals the poll cadence,
      // which would race and swallow alternate polls — restore SWR's default
      // so every tick actually fetches.
      dedupingInterval: 2000,
      // Pause ALL fetching for inactive runs — isPaused short-circuits the
      // fetcher entirely (no mount fetch, no revalidation), so an idle card
      // contributes zero BFF traffic.
      isPaused: () => !active,
      revalidateOnFocus: false,
      // A failing/missing log endpoint should not spam retries; the card
      // degrades to the placeholder regardless.
      shouldRetryOnError: false,
    },
  );

  const lines = useMemo(() => {
    const content = data?.content;
    if (!content) return [];
    const all = parseLogLines(content);
    return all.slice(-RUN_LOG_TAIL_LINES);
  }, [data?.content]);

  return { lines, isLoading };
}
