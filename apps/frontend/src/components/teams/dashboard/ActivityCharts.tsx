// apps/frontend/src/components/teams/dashboard/ActivityCharts.tsx

// Ported from upstream Paperclip's ActivityCharts
// (paperclip/ui/src/components/ActivityCharts.tsx) (MIT, (c) 2025 Paperclip AI).
// 2 charts (Run Activity / Success Rate) + shared helpers (ChartCard,
// DateLabels, getLast14Days). The Priority and Issue Status charts came over
// with the port and never gained a caller here; deleted rather than kept warm.
// Vanilla flexbox + percentage heights — no chart library.
// Colors mapped onto the brand tokens (midnight and its steps / brass /
// warn / edge / the state pairs) — class-only swap, chart axis + height
// logic untouched.

import type { DashboardRunActivityDay, HeartbeatRun } from "@/components/teams/shared/types";

/* ---- Brand tokens (used inline via CSS var(...)) ----
 * Centralized so every chart pulls from the same semantic mapping. Weight
 * carries most of the meaning here: the midnight ramp (midnight ->
 * midnight-soft -> midnight-muted -> midnight-faint -> edge) reads
 * heaviest-to-faintest, and a state colour is spent only where one is real.
 * Run-status semantics:
 *   succeeded -> midnight       (heaviest, success)
 *   failed    -> warn           (alert)
 *   other     -> midnight-muted (neutral)
 * Success-rate thresholds:
 *   >= 80%      -> ok
 *   >= 50%      -> warn
 *   < 50%       -> err
 */
const COLOR = {
  succeeded: "var(--midnight)",
  failed: "var(--warn)",
  other: "var(--midnight-muted)",
  emptyBar: "var(--edge-soft)",
  rateHigh: "var(--ok)",
  rateMid: "var(--warn)",
  rateLow: "var(--err)",
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
              className="text-2xs tabular-nums font-mono"
              style={{ color: "var(--midnight-muted)" }}
            >
              {formatDayLabel(day)}
            </span>
          ) : null}
        </div>
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
          style={{ color: "var(--midnight)" }}
        >
          {title}
        </h3>
        {subtitle && (
          <span
            className="text-2xs tabular-nums"
            style={{ color: "var(--midnight-muted)" }}
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
      style={{ color: "var(--midnight-muted)" }}
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
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-sm transition-colors hover:bg-[color:var(--cream-deep)]"
              title={`${day}: ${total} runs`}
            >
              {total > 0 ? (
                <div className="flex flex-col-reverse gap-px overflow-hidden rounded-sm" style={{ height: `${heightPct}%`, minHeight: 3 }}>
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
                  className="rounded-sm"
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
              className="group/bar flex-1 h-full flex flex-col justify-end rounded-sm transition-colors hover:bg-[color:var(--cream-deep)]"
              title={`${day}: ${entry.total > 0 ? Math.round(rate * 100) : 0}% (${entry.succeeded}/${entry.total})`}
            >
              {entry.total > 0 ? (
                <div className="rounded-sm" style={{ height: `${rate * 100}%`, minHeight: 3, backgroundColor: color }} />
              ) : (
                <div
                  className="rounded-sm"
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
