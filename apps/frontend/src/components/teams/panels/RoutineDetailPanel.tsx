// apps/frontend/src/components/teams/panels/RoutineDetailPanel.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { RoutineDetail, RoutineRun } from "@/components/teams/shared/types";

function runStatusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  if (s === "succeeded" || s === "completed" || s === "done") return "var(--forest)";
  return "var(--mute)";
}

function runTime(r: RoutineRun): number {
  const v = r.triggeredAt ?? r.startedAt ?? r.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function RoutineDetailPanel({ routineId }: { routineId: string }) {
  const { read } = useTeamsApi();
  const { data: routine, isLoading } = read<RoutineDetail>(`/routines/${routineId}`);
  const { data: runsData } = read<{ runs: RoutineRun[] }>(`/routines/${routineId}/runs`);

  if (isLoading) return <div className="p-8">Loading…</div>;
  if (!routine) return <div className="p-8 text-[var(--mute)]">Routine not found.</div>;

  const runs = [...(runsData?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  const triggers = routine.triggers ?? [];

  return (
    <div className="p-8 max-w-3xl flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold">{routine.title}</h1>
          {routine.status && (
            <span className="text-xs capitalize px-2 py-0.5 rounded"
              style={{ background: "var(--edge)", color: "var(--mute)" }}>
              {routine.status}
            </span>
          )}
        </div>
        {routine.description && (
          <p className="text-sm text-[var(--ink-soft)]">{routine.description}</p>
        )}
        <div className="text-sm text-[var(--mute)]">
          Assignee:{" "}
          <span className="text-[var(--ink)]">
            {routine.assignee?.name ?? routine.assigneeAgentId ?? "—"}
          </span>
        </div>
      </div>

      <section className="flex flex-col gap-2 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
        <h2 className="section-label"><b>Schedule</b></h2>
        {triggers.length === 0 ? (
          <p className="text-sm text-[var(--mute)]">No triggers.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {triggers.map((t) => (
              <li key={t.id} className="flex items-center gap-2 text-sm">
                <span className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
                  style={{ background: "var(--edge)", color: "var(--ink)" }}>
                  {t.kind}
                </span>
                {t.cronExpression && (
                  <span className="font-mono text-xs text-[var(--ink)]">{t.cronExpression}</span>
                )}
                {t.timezone && <span className="text-xs text-[var(--mute)]">{t.timezone}</span>}
                {t.nextRunAt && (
                  <span className="text-xs text-[var(--mute)]">· next {timeAgo(t.nextRunAt)}</span>
                )}
                {t.enabled === false && <span className="text-xs text-[var(--mute)]">· paused</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-2 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
        <h2 className="section-label"><b>Run history</b></h2>
        {runs.length === 0 ? (
          <p className="text-sm text-[var(--mute)]">No runs yet.</p>
        ) : (
          <ul className="flex flex-col">
            {runs.map((r) => {
              const when = r.triggeredAt ?? r.startedAt ?? r.createdAt;
              return (
              <li key={r.id} className="flex flex-col gap-0.5 px-3 py-2"
                  style={{ borderBottom: "1px solid var(--edge)" }}>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0"
                    style={{ background: "var(--edge)", color: "var(--mute)" }}>
                    {r.source}
                  </span>
                  <span className="text-xs capitalize flex-shrink-0" style={{ color: runStatusColor(r.status) }}>
                    {r.status.replace(/_/g, " ")}
                  </span>
                  {r.trigger && (
                    <span className="text-xs text-[var(--mute)] truncate">
                      {r.trigger.label ?? r.trigger.kind}
                    </span>
                  )}
                  {r.linkedIssue && (
                    <a href={`/teams/issues/${r.linkedIssue.id}`}
                      className="text-xs hover:underline" style={{ color: "var(--forest)" }}>
                      {r.linkedIssue.identifier ?? r.linkedIssue.id.slice(0, 8)}
                    </a>
                  )}
                  <span className="flex-1" />
                  {when && (
                    <span className="text-xs text-[var(--mute)] flex-shrink-0">{timeAgo(when)}</span>
                  )}
                </div>
                {r.failureReason && (
                  <div className="text-xs pl-1" style={{ color: "var(--danger)" }}>{r.failureReason}</div>
                )}
              </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
