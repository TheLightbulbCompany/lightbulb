// apps/frontend/src/components/teams/dashboard/ActivityCharts.tsx

// Ported from upstream Paperclip's ActivityCharts
// (paperclip/ui/src/components/ActivityCharts.tsx) (MIT, (c) 2025 Paperclip AI).
// 4 charts (Run Activity / Priority / Issue Status / Success Rate) +
// shared helpers (ChartCard, DateLabels, ChartLegend, getLast14Days).
// Vanilla flexbox + percentage heights — no chart library.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import type { DashboardRunActivityDay, HeartbeatRun } from "@/components/teams/shared/types";

/* ---- Utilities ---- */

export function getLast14Days(): string[] {
  return Array.from({ length: 14 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - (13 - i));
    return d.toISOString().slice(0, 10);
  });
}

function formatDayLabel(dateStr: string): string {
  const d = new Date(dateStr + "T12:00:00");
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

/* ---- Sub-components ---- */

function DateLabels({ days }: { days: string[] }) {
  return (
    <div className="flex gap-[2px] mt-2 border-t border-border/40 pt-1.5">
      {days.map((day, i) => (
        <div key={day} className="flex-1 text-center">
          {(i === 0 || i === 6 || i === 13) ? (
            <span className="text-[9px] text-muted-foreground/80 tabular-nums font-mono">{formatDayLabel(day)}</span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ChartLegend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2.5">
      {items.map(item => (
        <span key={item.label} className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <span className="h-1.5 w-1.5 rounded-full shrink-0 ring-1 ring-inset ring-black/5 dark:ring-white/5" style={{ backgroundColor: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

export function ChartCard({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="group relative border border-border/70 rounded-lg p-4 space-y-3 bg-card transition-colors hover:border-border">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-[11px] font-semibold text-foreground uppercase tracking-[0.06em]">{title}</h3>
        {subtitle && <span className="text-[10px] text-muted-foreground/70 tabular-nums">{subtitle}</span>}
      </div>
      {children}
    </div>
  );
}

/* ---- Chart Components ---- */

type RunChartProps =
  | { activity?: DashboardRunActivityDay[] | null; runs?: never }
  | { runs?: HeartbeatRun[] | null; activity?: never };

function aggregateRuns(runs: readonly HeartbeatRun[] = []): DashboardRunActivityDay[] {
  const days = getLast14Days();
  const grouped = new Map<string, DashboardRunActivityDay>();
  for (const day of days) grouped.set(day, { date: day, succeeded: 0, failed: 0, other: 0, total: 0 });
  for (const run of runs) {
    const startedAt = run.startedAt ?? run.completedAt;
    if (!startedAt) continue;
    const day = new Date(startedAt).toISOString().slice(0, 10);
    const entry = grouped.get(day);
    if (!entry) continue;
    if (run.status === "succeeded") entry.succeeded++;
    else if (run.status === "failed") entry.failed++;
    else entry.other++;
    entry.total++;
  }
  return Array.from(grouped.values());
}

function resolveRunActivity(props: RunChartProps): DashboardRunActivityDay[] {
  if (Array.isArray(props.activity)) return props.activity;
  if (Array.isArray(props.runs)) return aggregateRuns(props.runs);
  return [];
}

export function RunActivityChart(props: RunChartProps) {
  const activity = resolveRunActivity(props);
  const days = activity.length > 0 ? activity.map((day) => day.date) : getLast14Days();
  const grouped = new Map(activity.map((day) => [day.date, day]));

  const maxValue = Math.max(...activity.map(v => v.total), 1);
  const hasData = activity.some(v => v.total > 0);

  if (!hasData) return <p className="text-xs text-muted-foreground/70 italic">No runs yet</p>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day) ?? { date: day, succeeded: 0, failed: 0, other: 0, total: 0 };
          const total = entry.total;
          const heightPct = (total / maxValue) * 100;
          return (
            <div key={day} className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] hover:bg-foreground/[0.04] transition-colors" title={`${day}: ${total} runs`}>
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {entry.succeeded > 0 && <div className="bg-emerald-500 opacity-90 group-hover/bar:opacity-100 transition-opacity" style={{ flex: entry.succeeded }} />}
                  {entry.failed > 0 && <div className="bg-red-500 opacity-90 group-hover/bar:opacity-100 transition-opacity" style={{ flex: entry.failed }} />}
                  {entry.other > 0 && <div className="bg-neutral-500 opacity-80 group-hover/bar:opacity-100 transition-opacity" style={{ flex: entry.other }} />}
                </div>
              ) : (
                <div className="bg-muted/40 rounded-[1px]" style={{ height: 2 }} />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
    </div>
  );
}

const priorityColors: Record<string, string> = {
  critical: "#ef4444",
  high: "#f97316",
  medium: "#eab308",
  low: "#6b7280",
};

const priorityOrder = ["critical", "high", "medium", "low"] as const;

export function PriorityChart({ issues }: { issues: { priority: string; createdAt: Date }[] }) {
  const days = getLast14Days();
  const grouped = new Map<string, Record<string, number>>();
  for (const day of days) grouped.set(day, { critical: 0, high: 0, medium: 0, low: 0 });
  for (const issue of issues) {
    const day = new Date(issue.createdAt).toISOString().slice(0, 10);
    const entry = grouped.get(day);
    if (!entry) continue;
    if (issue.priority in entry) entry[issue.priority]++;
  }

  const maxValue = Math.max(...Array.from(grouped.values()).map(v => Object.values(v).reduce((a, b) => a + b, 0)), 1);
  const hasData = Array.from(grouped.values()).some(v => Object.values(v).reduce((a, b) => a + b, 0) > 0);

  if (!hasData) return <p className="text-xs text-muted-foreground/70 italic">No issues</p>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day)!;
          const total = Object.values(entry).reduce((a, b) => a + b, 0);
          const heightPct = (total / maxValue) * 100;
          return (
            <div key={day} className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] hover:bg-foreground/[0.04] transition-colors" title={`${day}: ${total} issues`}>
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {priorityOrder.map(p => entry[p] > 0 ? (
                    <div key={p} style={{ flex: entry[p], backgroundColor: priorityColors[p] }} />
                  ) : null)}
                </div>
              ) : (
                <div className="bg-muted/40 rounded-[1px]" style={{ height: 2 }} />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
      <ChartLegend items={priorityOrder.map(p => ({ color: priorityColors[p], label: p.charAt(0).toUpperCase() + p.slice(1) }))} />
    </div>
  );
}

const statusColors: Record<string, string> = {
  todo: "#3b82f6",
  in_progress: "#8b5cf6",
  in_review: "#a855f7",
  done: "#10b981",
  blocked: "#ef4444",
  cancelled: "#6b7280",
  backlog: "#64748b",
};

const statusLabels: Record<string, string> = {
  todo: "To Do",
  in_progress: "In Progress",
  in_review: "In Review",
  done: "Done",
  blocked: "Blocked",
  cancelled: "Cancelled",
  backlog: "Backlog",
};

export function IssueStatusChart({ issues }: { issues: { status: string; createdAt: Date }[] }) {
  const days = getLast14Days();
  const allStatuses = new Set<string>();
  const grouped = new Map<string, Record<string, number>>();
  for (const day of days) grouped.set(day, {});
  for (const issue of issues) {
    const day = new Date(issue.createdAt).toISOString().slice(0, 10);
    const entry = grouped.get(day);
    if (!entry) continue;
    entry[issue.status] = (entry[issue.status] ?? 0) + 1;
    allStatuses.add(issue.status);
  }

  const statusOrder = ["todo", "in_progress", "in_review", "done", "blocked", "cancelled", "backlog"].filter(s => allStatuses.has(s));
  const maxValue = Math.max(...Array.from(grouped.values()).map(v => Object.values(v).reduce((a, b) => a + b, 0)), 1);
  const hasData = allStatuses.size > 0;

  if (!hasData) return <p className="text-xs text-muted-foreground/70 italic">No issues</p>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day)!;
          const total = Object.values(entry).reduce((a, b) => a + b, 0);
          const heightPct = (total / maxValue) * 100;
          return (
            <div key={day} className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] hover:bg-foreground/[0.04] transition-colors" title={`${day}: ${total} issues`}>
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {statusOrder.map(s => (entry[s] ?? 0) > 0 ? (
                    <div key={s} style={{ flex: entry[s], backgroundColor: statusColors[s] ?? "#6b7280" }} />
                  ) : null)}
                </div>
              ) : (
                <div className="bg-muted/40 rounded-[1px]" style={{ height: 2 }} />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
      <ChartLegend items={statusOrder.map(s => ({ color: statusColors[s] ?? "#6b7280", label: statusLabels[s] ?? s }))} />
    </div>
  );
}

export function SuccessRateChart(props: RunChartProps) {
  const activity = resolveRunActivity(props);
  const days = activity.length > 0 ? activity.map((day) => day.date) : getLast14Days();
  const grouped = new Map(activity.map((day) => [day.date, day]));

  const hasData = activity.some(v => v.total > 0);
  if (!hasData) return <p className="text-xs text-muted-foreground/70 italic">No runs yet</p>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day) ?? { date: day, succeeded: 0, failed: 0, other: 0, total: 0 };
          const rate = entry.total > 0 ? entry.succeeded / entry.total : 0;
          const color = entry.total === 0 ? undefined : rate >= 0.8 ? "#10b981" : rate >= 0.5 ? "#eab308" : "#ef4444";
          return (
            <div key={day} className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] hover:bg-foreground/[0.04] transition-colors" title={`${day}: ${entry.total > 0 ? Math.round(rate * 100) : 0}% (${entry.succeeded}/${entry.total})`}>
              {entry.total > 0 ? (
                <div className="rounded-[2px]" style={{ height: `${rate * 100}%`, minHeight: 3, backgroundColor: color }} />
              ) : (
                <div className="bg-muted/40 rounded-[1px]" style={{ height: 2 }} />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
    </div>
  );
}
