"use client";

import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, XAxis, YAxis } from "recharts";

import { Card } from "@/components/admin/Card";
import { ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";

import type { GrowthData, GrowthSegment } from "../_lib/api";
import { num, pct } from "./sections";

// Growth-dashboard series. globals.css maps --chart-1..5 to ok, info, warn, err,
// NEUTRAL in BOTH grounds. Single-meaning series:
//   CHART_1 ok      — the healthy outcome: activated, multiplayer, answered
//   CHART_4 err     — alert: the largest gate-blocked reason. A series that is
//                     not an alert never takes it.
//   CHART_5 neutral — secondary / the denominator a CHART_1 series sits inside.
//                     Not a state colour: state colour only ever means state.
// CHART_2 (info) and CHART_3 (warn) also serve as plain distinct hues where a
// multi-series chart needs one more line than there are meanings — so
// `trial_started` is warn for separation, not status. Read the legend, not the
// colour, on the funnel chart.
const CHART_1 = "var(--chart-1)";
const CHART_2 = "var(--chart-2)";
const CHART_3 = "var(--chart-3)";
const CHART_4 = "var(--chart-4)";
const CHART_5 = "var(--chart-5)";

const GRID_STROKE = "var(--border)";
const AXIS_TICK_PROPS = { tickLine: false as const, axisLine: false as const, className: "text-2xs" };
const NUMERIC_Y_PROPS = { ...AXIS_TICK_PROPS, width: 32, allowDecimals: false as const, domain: [0, "auto"] as [number, "auto"] };

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Parses a `YYYY-MM-DD` (or ISO datetime) string as a UTC date. */
function parseIsoDate(value: string): Date {
  return new Date(value.length <= 10 ? `${value}T00:00:00Z` : value);
}

/** `M/D` tick label, e.g. `8/26`. Used for both daily and weekly X axes. */
function tickDayMonth(value: string): string {
  const d = parseIsoDate(value);
  if (Number.isNaN(d.getTime())) return value;
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
}

/** Full tooltip label, e.g. `Tue 26 Aug`. */
function tooltipDayLabel(value: unknown): string {
  const s = typeof value === "string" ? value : String(value);
  const d = parseIsoDate(s);
  if (Number.isNaN(d.getTime())) return s;
  return `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** A row of `{ bucket, [series]: number }` produced by {@link pivot}. */
type PivotRow = { bucket: string } & Record<string, string | number>;

/**
 * Pivots a sparse `{ [bucketField]: string; [seriesField]: string; [valueField]: number }[]`
 * fact table into one row per bucket (day or week), one column per series,
 * zero-filled for buckets/series with no row — a chart needs a continuous
 * axis and a fixed set of series per point.
 */
function pivot(
  buckets: readonly string[],
  rows: readonly Record<string, unknown>[],
  bucketField: string,
  seriesField: string,
  valueField: string,
  seriesOrder?: readonly string[],
): PivotRow[] {
  const grid = new Map<string, Map<string, number>>();
  const seen = new Set<string>(seriesOrder ?? []);
  for (const r of rows) {
    const bucket = String(r[bucketField]);
    const series = String(r[seriesField]);
    const value = Number(r[valueField]) || 0;
    seen.add(series);
    if (!grid.has(bucket)) grid.set(bucket, new Map());
    grid.get(bucket)!.set(series, value);
  }
  const seriesList = seriesOrder ?? [...seen];
  return buckets.map((bucket) => {
    const row: PivotRow = { bucket };
    for (const s of seriesList) row[s] = grid.get(bucket)?.get(s) ?? 0;
    return row;
  });
}

/** Distinct series values present in `rows`, sorted for deterministic color cycling. */
function distinctSeries(rows: readonly Record<string, unknown>[], seriesField: string): string[] {
  return [...new Set(rows.map((r) => String(r[seriesField])))].sort();
}

// ---------------------------------------------------------------------------
// 1. KPI strip
// ---------------------------------------------------------------------------

/** Running sum of `signups`, shifted so the last point equals `total` — a
 * shape for the sparkline, not an exact historical count (the 56-day window
 * doesn't cover a workspace's whole lifetime). */
function cumulativeOffsetToTotal(signups: readonly { day: string; n: number }[], total: number): number[] {
  const cumulative: number[] = [];
  let running = 0;
  for (const s of signups) {
    running += s.n;
    cumulative.push(running);
  }
  const last = cumulative.length ? cumulative[cumulative.length - 1] : 0;
  const offset = total - last;
  return cumulative.map((v) => v + offset);
}

function Sparkline({ values, color }: { values: number[]; color: string }) {
  const data = values.map((v, i) => ({ i, v }));
  // A flat series (min === max) would otherwise fill the whole tile as a
  // solid block — line only, no fill, when there's no variation to shade.
  const flat = values.length > 0 && Math.min(...values) === Math.max(...values);
  return (
    <ChartContainer config={{}} className="aspect-auto h-10 w-full">
      <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 2, left: 0 }}>
        <Area dataKey="v" type="monotone" stroke={color} strokeOpacity={0.6} fill={color} fillOpacity={flat ? 0 : 0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
      </AreaChart>
    </ChartContainer>
  );
}

function KpiTile({ label, value, tone, sparkline }: { label: string; value: React.ReactNode; tone?: "alert"; sparkline: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${tone === "alert" ? "text-err" : "text-foreground"}`}>{value}</div>
      <div className="mt-2 h-10 w-full">{sparkline}</div>
    </div>
  );
}

function NoSparkline() {
  return <div className="h-10 w-full" />;
}

/** The five §6 metrics, in order: activation, weekly active + multiplayer,
 * answered + p50, paying, W4 retention. */
export function KpiStrip({ seg }: { seg: GrowthSegment }) {
  const cumulativeWorkspaces = cumulativeOffsetToTotal(seg.daily.signups, seg.workspaces.total);
  const activeByWeek = seg.weekly_active.map((w) => w.workspaces ?? 0);
  const thisWeek = seg.weekly_active[seg.weekly_active.length - 1];
  const messagesByDay = seg.daily.human_messages.map((d) => d.n ?? 0);

  return (
    <Card>
      <div className="grid grid-cols-2 gap-6 md:grid-cols-3 xl:grid-cols-6">
        <KpiTile label="Workspaces" value={seg.workspaces.total} sparkline={<Sparkline values={cumulativeWorkspaces} color={CHART_5} />} />
        <KpiTile label="Paying" value={seg.workspaces.paying} sparkline={<NoSparkline />} />
        <KpiTile
          label="Weekly active workspaces"
          value={num(thisWeek?.workspaces)}
          sparkline={<Sparkline values={activeByWeek} color={CHART_1} />}
        />
        <KpiTile
          label="Multiplayer share (this week)"
          value={pct(thisWeek?.multiplayer_share)}
          sparkline={<Sparkline values={seg.weekly_active.map((w) => Math.round((w.multiplayer_share ?? 0) * 100))} color={CHART_1} />}
        />
        <KpiTile
          label="Activated rate"
          value={pct(seg.activation.rate)}
          sparkline={<Sparkline values={seg.daily.activated.map((d) => d.n ?? 0)} color={CHART_1} />}
        />
        <KpiTile
          label="Answered rate"
          value={pct(seg.answered.rate)}
          tone={seg.answered.rate !== null && seg.answered.rate < 0.95 ? "alert" : undefined}
          sparkline={<Sparkline values={messagesByDay} color={CHART_5} />}
        />
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Charts grid
// ---------------------------------------------------------------------------

// The funnel's last stage is SERVER-side: `activated` is the §6 definition
// computed from the messaging table, not a client event. The three before it
// are PostHog, which counts dev and prod together.
const FUNNEL_POSTHOG_EVENTS = ["landing_viewed", "signup_started", "trial_started"] as const;
const FUNNEL_CONFIG: ChartConfig = {
  landing_viewed: { label: "landing viewed", color: CHART_5 },
  signup_started: { label: "signup started", color: CHART_2 },
  trial_started: { label: "trial started", color: CHART_3 },
  activated: { label: "activated (server)", color: CHART_1 },
};
const FUNNEL_SERIES = [...FUNNEL_POSTHOG_EVENTS, "activated"] as const;

/** 1. Acquisition (daily) — landing → signup → trial from PostHog, activation from us. */
export function AcquisitionChart({ data, seg }: { data: GrowthData; seg: GrowthSegment }) {
  const activatedByDay = new Map(seg.daily.activated.map((d) => [d.day, d.n]));
  const rows = pivot(data.days, data.posthog.funnel_daily, "day", "event", "owners", FUNNEL_POSTHOG_EVENTS).map((row) => ({
    ...row,
    activated: activatedByDay.get(String(row.bucket)) ?? 0,
  }));
  return (
    <Card title="Acquisition — landing viewed → signup started → trial started → activated">
      <ChartContainer config={FUNNEL_CONFIG} className="h-[220px] w-full">
        <LineChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="bucket" tickFormatter={tickDayMonth} interval={6} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          {FUNNEL_SERIES.map((series) => (
            <Line key={series} dataKey={series} type="monotone" stroke={FUNNEL_CONFIG[series].color} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        The first three stages are PostHog unique persons — dev and prod share one project, so they count both
        environments. The last is the audit §6 activation definition, computed server-side from this environment&apos;s
        messages only; the stages are not directly comparable.
      </div>
    </Card>
  );
}

const WEEKLY_ACTIVE_CONFIG: ChartConfig = {
  multiplayer: { label: "≥2 human senders", color: CHART_1 },
  single: { label: "1 human sender", color: CHART_5 },
};

/** 2. Weekly active workspaces, split by whether more than one human spoke. */
export function WeeklyActiveChart({ seg }: { seg: GrowthSegment }) {
  const rows = seg.weekly_active.map((w) => ({
    week: w.week,
    // null (messaging read failed) leaves a gap in the bar; 0 would read as
    // "nobody was active", which is a different and false claim.
    multiplayer: w.multiplayer,
    single: w.workspaces === null || w.multiplayer === null ? null : w.workspaces - w.multiplayer,
  }));
  return (
    <Card title="Weekly active workspaces (≥1 human room message) — multiplayer split">
      <ChartContainer config={WEEKLY_ACTIVE_CONFIG} className="h-[220px] w-full">
        <BarChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="week" tickFormatter={tickDayMonth} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="multiplayer" stackId="waw" fill={CHART_1} isAnimationActive={false} />
          <Bar dataKey="single" stackId="waw" fill={CHART_5} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        Agent messages never make a week active — only humans do.
      </div>
    </Card>
  );
}

const COHORT_CONFIG: ChartConfig = {
  signups: { label: "signups", color: CHART_5 },
  activated: { label: "activated", color: CHART_1 },
  paying: { label: "paying now", color: CHART_2 },
};

/** 3. Signup-week cohorts — how many of each week's workspaces activated and pay. */
export function CohortChart({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="Signup cohorts — activated and paying">
      <ChartContainer config={COHORT_CONFIG} className="h-[220px] w-full">
        <BarChart data={seg.cohorts} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="week" tickFormatter={tickDayMonth} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="signups" fill={CHART_5} radius={[2, 2, 0, 0]} isAnimationActive={false} />
          <Bar dataKey="activated" fill={CHART_1} radius={[2, 2, 0, 0]} isAnimationActive={false} />
          <Bar dataKey="paying" fill={CHART_2} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        &ldquo;Paying now&rdquo; is the cohort&apos;s CURRENT status, not a recorded trial→paid transition: a workspace
        that converted and then cancelled reads here as never converted.
      </div>
    </Card>
  );
}

/** 4. Human room messages per day — the volume behind the weekly-active count. */
export function HumanMessagesChart({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="Human room messages per day">
      <ChartContainer config={{ n: { label: "human messages", color: CHART_2 } }} className="h-[220px] w-full">
        <BarChart data={seg.daily.human_messages} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="day" tickFormatter={tickDayMonth} interval={6} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <Bar dataKey="n" fill={CHART_2} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
    </Card>
  );
}

// err first (a blocked gate IS the alert), then four hues no two of which are
// adjacent-and-alike in the stack: warn gold, neutral grey, info blue, ok green.
const GATE_BLOCKED_CYCLE = [CHART_4, CHART_3, CHART_5, CHART_2, CHART_1];

const GATE_BLOCKED_TITLE = "provision.gate.blocked per day (CloudWatch, all workspaces) — last 7 days";

/** 5. provision.gate.blocked per day — one stacked series per code present.
 * CloudWatch only supplies the last 7 days, so this chart (unlike the other
 * daily ones) plots `days.slice(-7)`, not the full 56-day window. */
export function GateBlockedDailyChart({ data }: { data: GrowthData }) {
  const codes = distinctSeries(data.gate_blocked_daily, "code");
  if (codes.length === 0) {
    return (
      <Card title={GATE_BLOCKED_TITLE}>
        <div className="flex h-[220px] items-center justify-center text-xs text-muted-foreground">none</div>
      </Card>
    );
  }
  const last7Days = data.days.slice(-7);
  const rows = pivot(last7Days, data.gate_blocked_daily, "day", "code", "n", codes);
  const config: ChartConfig = Object.fromEntries(
    codes.map((code, i) => [code, { label: code, color: GATE_BLOCKED_CYCLE[i % GATE_BLOCKED_CYCLE.length] }]),
  );
  return (
    <Card title={GATE_BLOCKED_TITLE}>
      <ChartContainer config={config} className="h-[220px] w-full">
        <BarChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="bucket" tickFormatter={tickDayMonth} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          {codes.length > 1 ? <ChartLegend content={<ChartLegendContent />} /> : null}
          {codes.map((code, i) => (
            <Bar
              key={code}
              dataKey={code}
              stackId="codes"
              fill={GATE_BLOCKED_CYCLE[i % GATE_BLOCKED_CYCLE.length]}
              radius={i === codes.length - 1 ? [2, 2, 0, 0] : undefined}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ChartContainer>
    </Card>
  );
}

const RETENTION_ROWS: { key: "retained" | "lapsed"; label: string; color: string }[] = [
  { key: "retained", label: "still messaging in week 4", color: CHART_1 },
  { key: "lapsed", label: "silent in week 4", color: CHART_5 },
];

/** 6. Week-4 retention of activated workspaces — two bars, so the denominator is visible. */
export function RetentionChart({ seg }: { seg: GrowthSegment }) {
  const r = seg.retention_w4;
  const values = {
    retained: r.retained,
    lapsed: r.eligible === null || r.retained === null ? null : r.eligible - r.retained,
  };
  const rows = RETENTION_ROWS.map((row) => ({ label: row.label, value: values[row.key], color: row.color }));
  return (
    <Card title={`Week-4 retention of activated workspaces — ${pct(r.rate)}`}>
      <ChartContainer config={{}} className="h-[220px] w-full">
        <BarChart data={rows} layout="vertical" margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis type="number" tickMargin={8} {...AXIS_TICK_PROPS} allowDecimals={false} domain={[0, "auto"]} />
          <YAxis type="category" dataKey="label" width={170} {...AXIS_TICK_PROPS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="value" radius={[0, 2, 2, 0]} isAnimationActive={false}>
            {rows.map((row) => (
              <Cell key={row.label} fill={row.color} />
            ))}
          </Bar>
        </BarChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        Only workspaces whose 4th week after activation has fully passed are counted — an in-flight week is not a miss.
      </div>
    </Card>
  );
}
