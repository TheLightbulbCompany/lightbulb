// apps/frontend/src/components/teams/issues/IssueRunsSection.tsx
"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl, handleShellLinkClick } from "@/lib/shellNav";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import {
  liveRunActivity,
  liveRunTitle,
  type LiveRun,
} from "@/components/teams/shared/lib/liveRuns";
import type { CompanyAgent, IssueRun } from "@/components/teams/shared/types";

// Status is BFF-forwarded upstream JSON — treat it as possibly absent even
// though the type says string (row shapes drift across upstream versions).
function isLiveStatus(status: string | null | undefined): boolean {
  const s = (status ?? "").toLowerCase();
  return s === "running" || s === "queued";
}

function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--midnight)";
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--err)";
  // In-flight runs read as live (forest), not the neutral grey they fell
  // through to before — so a running/queued run has a visible affordance.
  if (isLiveStatus(s)) return "var(--midnight)";
  return "var(--midnight-muted)";
}

function runTime(r: IssueRun): number {
  const v = r.startedAt ?? r.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function IssueRunsSection({
  issueId,
  agents,
}: {
  issueId: string;
  agents: CompanyAgent[];
}) {
  const { read } = useTeamsApi();
  const { data, error, isLoading } = read<{ runs: IssueRun[] }>(
    `/issues/${issueId}/runs`,
  );
  // The /issues/{id}/runs projection carries no outputSilence, so join live
  // rows to the /inbox/live-runs feed by run id (both are heartbeatRuns.id)
  // for the stalled-vs-running signal — otherwise a run the Inbox flags as
  // stalled would render healthy on the very surface a user opens to
  // investigate it.
  const { runs: liveRuns } = useLiveRuns();
  const liveById = useMemo(() => {
    const map = new Map<string, LiveRun>();
    for (const run of liveRuns) map.set(run.id, run);
    return map;
  }, [liveRuns]);
  const runs = [...(data?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  // Render nothing only when there's genuinely nothing to say — no runs, not
  // loading, no error. A fetch failure or in-flight load gets an affordance.
  if (runs.length === 0 && !error && !isLoading) return null;

  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Runs</b></h2>
      {error && (
        <p className="text-xs" style={{ color: "var(--err)" }}>
          Couldn&apos;t load runs.
        </p>
      )}
      {isLoading && runs.length === 0 && !error && (
        <p className="text-xs italic" style={{ color: "var(--midnight-muted)" }}>
          Loading runs…
        </p>
      )}
      <ul className="flex flex-col">
        {runs.map((r) => {
          const when = r.startedAt ?? r.createdAt;
          const isLive = isLiveStatus(r.status);
          // Feed-lag fallback: a live row missing from /inbox/live-runs (its
          // 15s poll hasn't caught up) still gets an accurate per-status dot.
          const liveRun = isLive
            ? liveById.get(r.runId) ?? { id: r.runId, status: r.status }
            : null;
          const activity = liveRun ? liveRunActivity(liveRun) : null;
          return (
            <li key={r.runId} className="flex items-center gap-2 px-3 py-2">
              <span
                className="inline-flex shrink-0 items-center gap-1.5 text-xs capitalize"
                style={{
                  color: activity === "stalled" ? "var(--warn)" : statusColor(r.status),
                }}
              >
                {liveRun ? (
                  <LiveRunDot state={activity ?? "running"} title={liveRunTitle(liveRun)} />
                ) : null}
                {r.status}
              </span>
              <span className="flex-1 truncate text-[var(--midnight)]">{agentName(r.agentId)}</span>
              {when && <span className="text-xs text-[var(--midnight-muted)]">{timeAgo(when)}</span>}
              <a
                href={teamsDetailUrl("run", r.runId)}
                onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("run", r.runId))}
                aria-label={`View run ${r.runId}`}
                className="text-xs hover:underline"
                style={{ color: "var(--midnight)" }}
              >
                View run
              </a>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
