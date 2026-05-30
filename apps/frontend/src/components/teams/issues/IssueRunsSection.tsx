// apps/frontend/src/components/teams/issues/IssueRunsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, IssueRun } from "@/components/teams/shared/types";

function statusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--forest)";
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  return "var(--mute)";
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
  const { data } = read<{ runs: IssueRun[] }>(`/issues/${issueId}/runs`);
  const runs = [...(data?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  if (runs.length === 0) return null;

  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Runs</b></h2>
      <ul className="flex flex-col">
        {runs.map((r) => {
          const when = r.startedAt ?? r.createdAt;
          return (
            <li key={r.runId} className="flex items-center gap-2 px-3 py-2">
              <span
                className="text-xs capitalize flex-shrink-0"
                style={{ color: statusColor(r.status) }}
              >
                {r.status}
              </span>
              <span className="flex-1 truncate text-[var(--ink)]">{agentName(r.agentId)}</span>
              {when && <span className="text-xs text-[var(--mute)]">{timeAgo(when)}</span>}
              {r.agentId && (
                <a
                  href={`/teams/agents/${r.agentId}/runs/${r.runId}`}
                  className="text-sm hover:underline"
                  style={{ color: "var(--forest)" }}
                >
                  View
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
