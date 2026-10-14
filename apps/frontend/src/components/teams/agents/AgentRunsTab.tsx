// apps/frontend/src/components/teams/agents/AgentRunsTab.tsx

// Ported from upstream Paperclip's AgentDetail page — the Runs view
// (`RunsTab` + `RunListItem` + `runStatusIcons` / `sourceLabels` / `runMetrics`)
// (paperclip/ui/src/pages/AgentDetail.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Row layout, copy, sort order and the
// status/source/summary/token readout are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - react-query heartbeatsApi.list(companyId, agentId) -> useTeamsApi read of
//                                       /teams/agents/{id}/runs; the BFF builds the
//                                       company-scoped upstream call and 404s an
//                                       agent outside the caller's workspace
//   - the side-by-side `RunDetail` pane (transcript, workspace ops, provider
//     trace) is not ported: a row opens our existing run-detail panel in-shell,
//     so there is no selected state and no mobile list/detail toggle
//   - run cost is dropped from the metrics line: billing is Metronome/the gate,
//     not Paperclip's run cost; token counts stay
//   - `SourceResolvedFoldBadge` (watchdog fold marker) is not ported
//   - status icon colours / source badge palette -> brand tokens (runStatus.ts)

"use client";

import { CheckCircle2, Clock, Loader2, Slash, Timer, XCircle } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { statusColor } from "@/components/teams/shared/lib/runStatus";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { RunDetail as HeartbeatRun } from "@/components/teams/shared/types";

export const runStatusIcons: Record<string, typeof CheckCircle2> = {
  succeeded: CheckCircle2,
  failed: XCircle,
  running: Loader2,
  queued: Clock,
  scheduled_retry: Clock,
  timed_out: Timer,
  cancelled: Slash,
};

export const sourceLabels: Record<string, string> = {
  timer: "Timer",
  assignment: "Assignment",
  on_demand: "On-demand",
  automation: "Automation",
};

/** Newest first, as upstream sorts before rendering. */
export function sortRunsNewestFirst(runs: HeartbeatRun[]): HeartbeatRun[] {
  return [...runs].sort(
    (a, b) => new Date(b.createdAt ?? 0).getTime() - new Date(a.createdAt ?? 0).getTime(),
  );
}

/** Upstream's summary read: resultJson.summary ?? resultJson.result, else the error. */
export function runSummaryText(run: HeartbeatRun): string {
  return run.resultJson
    ? String(run.resultJson.summary ?? run.resultJson.result ?? "")
    : run.error ?? "";
}

function usageNumber(usage: Record<string, unknown> | null, ...keys: string[]) {
  if (!usage) return 0;
  for (const key of keys) {
    const value = usage[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return 0;
}

function runTotalTokens(run: HeartbeatRun): number {
  const usage = (run.usageJson ?? null) as Record<string, unknown> | null;
  return (
    usageNumber(usage, "inputTokens", "input_tokens") +
    usageNumber(usage, "outputTokens", "output_tokens")
  );
}

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function RunStatusIcon({ status, className }: { status?: string | null; className?: string }) {
  const Icon = runStatusIcons[status ?? ""] ?? Clock;
  return (
    <Icon
      aria-hidden
      className={cn("h-3.5 w-3.5 shrink-0", status === "running" && "animate-spin", className)}
      style={{ color: statusColor(status) }}
    />
  );
}

export function SourceBadge({ source }: { source?: string | null }) {
  if (!source) return null;
  return (
    <span className="rounded bg-[var(--cream-deep)] px-1.5 text-2xs text-[var(--midnight-soft)]">
      {sourceLabels[source] ?? source}
    </span>
  );
}

function RunListItem({ run }: { run: HeartbeatRun }) {
  const href = teamsDetailUrl("run", run.id);
  const summary = runSummaryText(run);
  const totalTokens = runTotalTokens(run);

  return (
    <a
      href={href}
      onClick={(e) => handleShellLinkClick(e, href)}
      aria-label={`View run ${run.id}`}
      className="flex w-full flex-col gap-1 border-b border-[var(--edge)] px-3 py-2.5 text-left text-inherit no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deep)]"
    >
      <div className="flex items-center gap-2">
        <RunStatusIcon status={run.status} />
        <span className="font-mono text-caption text-[var(--midnight-muted)]">{run.id.slice(0, 8)}</span>
        <SourceBadge source={run.invocationSource} />
        <span className="ml-auto shrink-0 text-2xs text-[var(--midnight-muted)]">
          {run.createdAt ? timeAgo(run.createdAt) : ""}
        </span>
      </div>
      {summary && (
        <span className="truncate pl-5.5 text-caption text-[var(--midnight-muted)]">{summary.slice(0, 60)}</span>
      )}
      {totalTokens > 0 && (
        <div className="flex items-center gap-2 pl-5.5 text-2xs tabular-nums text-[var(--midnight-muted)]">
          <span>{formatTokens(totalTokens)} tok</span>
        </div>
      )}
    </a>
  );
}

export function AgentRunsTab({ agentId }: { agentId: string }) {
  const { read } = useTeamsApi();
  const { data, error, isLoading } = read<{ runs: HeartbeatRun[] }>(
    `/agents/${encodeURIComponent(agentId)}/runs`,
  );

  if (error) {
    return <p className="py-4 text-ui text-[var(--err)]">Couldn&apos;t load runs.</p>;
  }
  if (isLoading && !data) {
    return <p className="py-4 text-ui italic text-[var(--midnight-muted)]">Loading runs…</p>;
  }
  const runs = data?.runs ?? [];
  if (runs.length === 0) {
    return <p className="py-4 text-ui text-[var(--midnight-muted)]">No runs yet.</p>;
  }

  return (
    <div className="my-4 overflow-x-hidden rounded-lg border border-[var(--edge)]">
      {sortRunsNewestFirst(runs).map((run) => (
        <RunListItem key={run.id} run={run} />
      ))}
    </div>
  );
}
