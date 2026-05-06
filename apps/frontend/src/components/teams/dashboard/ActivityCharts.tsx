// apps/frontend/src/components/teams/dashboard/ActivityCharts.tsx

// Ported from upstream Paperclip's ActivityCharts
// (paperclip/ui/src/components/ActivityCharts.tsx) (MIT, (c) 2025 Paperclip AI).
// 4 charts (Run Activity / Priority / Issue Status / Success Rate) +
// shared helpers (ChartCard, DateLabels, ChartLegend, getLast14Days).
// Vanilla flexbox + percentage heights — no chart library.
// Colors mapped onto the editorial palette (forest / forest-soft /
// warn / mute / mute-soft / ink / ink-soft / edge) — class-only swap,
// chart axis + height logic untouched.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import type { DashboardRunActivityDay, HeartbeatRun } from "@/components/teams/shared/types";

/* ---- Editorial color tokens (used inline via CSS var(...)) ----
 * Centralized so every chart pulls from the same semantic mapping.
 * Run-status semantics:
 *   succeeded -> forest      (deep green, success)
 *   failed    -> warn        (amber, alert)
 *   other     -> mute        (neutral)
 * Priority semantics (ordered crit -> low):
 *   critical  -> warn        (amber alert)
 *   high      -> forest      (deep, weighty)
 *   medium    -> mute        (neutral)
 *   low       -> mute-soft   (faint)
 * Status semantics:
 *   todo         -> mute-soft (waiting)
 *   in_progress  -> forest-soft (active)
 *   in_review    -> ink-soft  (consideration)
 *   done         -> forest    (completed)
 *   blocked      -> warn      (alert)
 *   cancelled    -> mute      (neutral)
 *   backlog      -> edge      (parked)
 * Success-rate thresholds:
 *   >= 80%      -> forest
 *   >= 50%      -> warn
 *   < 50%       -> ink        (dark, somber)
 */
const COLOR = {
  succeeded: "var(--forest)",
  failed: "var(--warn)",
  other: "var(--mute)",
  emptyBar: "var(--edge-soft)",
  rateHigh: "var(--forest)",
  rateMid: "var(--warn)",
  rateLow: "var(--ink)",
} as const;

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
    <div
      className="flex gap-[2px] mt-2 pt-1.5"
      style={{ borderTop: "1px solid var(--edge)" }}
    >
      {days.map((day, i) => (
        <div key={day} className="flex-1 text-center">
          {(i === 0 || i === 6 || i === 13) ? (
            <span
              className="text-[9px] tabular-nums font-mono"
              style={{ color: "var(--mute)" }}
            >
              {formatDayLabel(day)}
            </span>
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
        <span
          key={item.label}
          className="flex items-center gap-1.5 text-[10px]"
          style={{ color: "var(--mute)" }}
        >
          <span
            className="h-1.5 w-1.5 rounded-full shrink-0"
            style={{ backgroundColor: item.color }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}

export function ChartCard({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="editorial-card group relative p-4 space-y-3 transition-colors">
      <div className="flex items-baseline justify-between gap-2">
        <h3
          className="font-editorial-serif text-2xl"
          style={{ color: "var(--ink)" }}
        >
          {title}
        </h3>
        {subtitle && (
          <span
            className="text-[10px] tabular-nums"
            style={{ color: "var(--mute)" }}
          >
            {subtitle}
          </span>
        )}
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

function EmptyChartCopy({ children }: { children: React.ReactNode }) {
  return (
    <p
      className="font-editorial-serif text-base italic"
      style={{ color: "var(--mute)" }}
    >
      {children}
    </p>
  );
}

export function RunActivityChart(props: RunChartProps) {
  const activity = resolveRunActivity(props);
  const days = activity.length > 0 ? activity.map((day) => day.date) : getLast14Days();
  const grouped = new Map(activity.map((day) => [day.date, day]));

  const maxValue = Math.max(...activity.map(v => v.total), 1);
  const hasData = activity.some(v => v.total > 0);

  if (!hasData) return <EmptyChartCopy>No runs yet</EmptyChartCopy>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day) ?? { date: day, succeeded: 0, failed: 0, other: 0, total: 0 };
          const total = entry.total;
          const heightPct = (total / maxValue) * 100;
          return (
            <div
              key={day}
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] transition-colors hover:bg-[color:var(--cream-deeper)]"
              title={`${day}: ${total} runs`}
            >
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {entry.succeeded > 0 && (
                    <div
                      className="opacity-90 group-hover/bar:opacity-100 transition-opacity"
                      style={{ flex: entry.succeeded, backgroundColor: COLOR.succeeded }}
                    />
                  )}
                  {entry.failed > 0 && (
                    <div
                      className="opacity-90 group-hover/bar:opacity-100 transition-opacity"
                      style={{ flex: entry.failed, backgroundColor: COLOR.failed }}
                    />
                  )}
                  {entry.other > 0 && (
                    <div
                      className="opacity-80 group-hover/bar:opacity-100 transition-opacity"
                      style={{ flex: entry.other, backgroundColor: COLOR.other }}
                    />
                  )}
                </div>
              ) : (
                <div
                  className="rounded-[1px]"
                  style={{ height: 2, backgroundColor: COLOR.emptyBar }}
                />
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
  critical: "var(--warn)",
  high: "var(--forest)",
  medium: "var(--mute)",
  low: "var(--mute-soft)",
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

  if (!hasData) return <EmptyChartCopy>No issues</EmptyChartCopy>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day)!;
          const total = Object.values(entry).reduce((a, b) => a + b, 0);
          const heightPct = (total / maxValue) * 100;
          return (
            <div
              key={day}
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] transition-colors hover:bg-[color:var(--cream-deeper)]"
              title={`${day}: ${total} issues`}
            >
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {priorityOrder.map(p => entry[p] > 0 ? (
                    <div key={p} style={{ flex: entry[p], backgroundColor: priorityColors[p] }} />
                  ) : null)}
                </div>
              ) : (
                <div
                  className="rounded-[1px]"
                  style={{ height: 2, backgroundColor: COLOR.emptyBar }}
                />
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
  todo: "var(--mute-soft)",
  in_progress: "var(--forest-soft)",
  in_review: "var(--ink-soft)",
  done: "var(--forest)",
  blocked: "var(--warn)",
  cancelled: "var(--mute)",
  backlog: "var(--edge)",
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

  if (!hasData) return <EmptyChartCopy>No issues</EmptyChartCopy>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day)!;
          const total = Object.values(entry).reduce((a, b) => a + b, 0);
          const heightPct = (total / maxValue) * 100;
          return (
            <div
              key={day}
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] transition-colors hover:bg-[color:var(--cream-deeper)]"
              title={`${day}: ${total} issues`}
            >
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-[2px]" style={{ height: `${heightPct}%`, minHeight: 3 }}>
                  {statusOrder.map(s => (entry[s] ?? 0) > 0 ? (
                    <div key={s} style={{ flex: entry[s], backgroundColor: statusColors[s] ?? "var(--mute)" }} />
                  ) : null)}
                </div>
              ) : (
                <div
                  className="rounded-[1px]"
                  style={{ height: 2, backgroundColor: COLOR.emptyBar }}
                />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
      <ChartLegend items={statusOrder.map(s => ({ color: statusColors[s] ?? "var(--mute)", label: statusLabels[s] ?? s }))} />
    </div>
  );
}

export function SuccessRateChart(props: RunChartProps) {
  const activity = resolveRunActivity(props);
  const days = activity.length > 0 ? activity.map((day) => day.date) : getLast14Days();
  const grouped = new Map(activity.map((day) => [day.date, day]));

  const hasData = activity.some(v => v.total > 0);
  if (!hasData) return <EmptyChartCopy>No runs yet</EmptyChartCopy>;

  return (
    <div>
      <div className="flex items-end gap-[2px] h-20">
        {days.map(day => {
          const entry = grouped.get(day) ?? { date: day, succeeded: 0, failed: 0, other: 0, total: 0 };
          const rate = entry.total > 0 ? entry.succeeded / entry.total : 0;
          const color = entry.total === 0 ? undefined : rate >= 0.8 ? COLOR.rateHigh : rate >= 0.5 ? COLOR.rateMid : COLOR.rateLow;
          return (
            <div
              key={day}
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-[2px] transition-colors hover:bg-[color:var(--cream-deeper)]"
              title={`${day}: ${entry.total > 0 ? Math.round(rate * 100) : 0}% (${entry.succeeded}/${entry.total})`}
            >
              {entry.total > 0 ? (
                <div className="rounded-[2px]" style={{ height: `${rate * 100}%`, minHeight: 3, backgroundColor: color }} />
              ) : (
                <div
                  className="rounded-[1px]"
                  style={{ height: 2, backgroundColor: COLOR.emptyBar }}
                />
              )}
            </div>
          );
        })}
      </div>
      <DateLabels days={days} />
    </div>
  );
}
