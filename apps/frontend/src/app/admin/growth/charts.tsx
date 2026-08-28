"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts";

import { Card } from "@/components/admin/Card";
import { ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";

import type { GrowthData, GrowthOwnerStage, GrowthSegment } from "../_lib/api";
import { STAGE_LABEL } from "./sections";

// Growth-dashboard series. globals.css maps --chart-1..5 to ok, info, warn, err,
// brass in BOTH grounds. Single-meaning series:
//   CHART_1 ok    — autonomous / healthy / the ladder
//   CHART_4 err   — alert: auto-paused, the largest gate-blocked reason
//   CHART_5 brass — secondary / unknown / order-independent
// CHART_2 (info), CHART_3 (warn) and CHART_4 (err) also serve as plain distinct
// hues where a multi-series chart needs one more line than there are meanings —
// so `trial_started` is err and `archived` is info for separation, not status.
// Read the legend, not the colour, on the funnel and survival charts.
// CHART_3 (warn) and CHART_5 (brass) are both warm golds and are the weakest
// pair to tell apart, so a four-line chart takes one of them, not both.
const CHART_1 = "var(--chart-1)";
const CHART_2 = "var(--chart-2)";
const CHART_3 = "var(--chart-3)";
const CHART_4 = "var(--chart-4)";
const CHART_5 = "var(--chart-5)";

const GRID_STROKE = "var(--border)";
const AXIS_TICK_PROPS = { tickLine: false as const, axisLine: false as const, className: "text-[10px]" };
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
 * doesn't cover an owner's whole lifetime). */
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

export function KpiStrip({ seg }: { seg: GrowthSegment }) {
  const cumulativeOwners = cumulativeOffsetToTotal(seg.daily.signups, seg.owners.total);
  const activeOwnersByWeek = seg.north_star.weeks.map((w) => w.owners_active);
  const autonomousByDay = seg.daily.runs.map((r) => r.autonomous_ok);
  const armedAutomations = seg.depth.armed_per_owner.reduce((sum, o) => sum + o.armed, 0);
  const autonomous7d = seg.daily.runs.slice(-7).reduce((sum, r) => sum + r.autonomous_ok, 0);

  return (
    <Card>
      <div className="grid grid-cols-2 gap-6 md:grid-cols-3 xl:grid-cols-6">
        <KpiTile label="Owners" value={seg.owners.total} sparkline={<Sparkline values={cumulativeOwners} color={CHART_5} />} />
        <KpiTile label="Paying" value={seg.owners.paid} sparkline={<NoSparkline />} />
        <KpiTile
          label="Active owners this week"
          value={activeOwnersByWeek[activeOwnersByWeek.length - 1] ?? 0}
          sparkline={<Sparkline values={activeOwnersByWeek} color={CHART_1} />}
        />
        <KpiTile label="Autonomous runs (7d)" value={autonomous7d} sparkline={<Sparkline values={autonomousByDay} color={CHART_1} />} />
        <KpiTile label="Armed automations" value={armedAutomations} sparkline={<NoSparkline />} />
        <KpiTile
          label="Nightly loops never armed"
          value={seg.nightly_ceo.never_armed}
          tone={seg.nightly_ceo.never_armed > 0 ? "alert" : undefined}
          sparkline={<NoSparkline />}
        />
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Charts grid
// ---------------------------------------------------------------------------

const FUNNEL_DAILY_EVENTS = ["landing_viewed", "signup_started", "trial_started", "trial_converted"] as const;
const FUNNEL_DAILY_CONFIG: ChartConfig = {
  landing_viewed: { label: "landing viewed", color: CHART_5 },
  signup_started: { label: "signup started", color: CHART_2 },
  trial_started: { label: "trial started", color: CHART_4 },
  trial_converted: { label: "trial converted", color: CHART_1 },
};

/** 1. Acquisition (daily) — PostHog client funnel, one line per event. */
export function AcquisitionChart({ data }: { data: GrowthData }) {
  const rows = pivot(data.days, data.posthog.funnel_daily, "day", "event", "owners", FUNNEL_DAILY_EVENTS);
  return (
    <Card title="Acquisition — client funnel per day">
      <ChartContainer config={FUNNEL_DAILY_CONFIG} className="h-[220px] w-full">
        <LineChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="bucket" tickFormatter={tickDayMonth} interval={6} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          {FUNNEL_DAILY_EVENTS.map((event) => (
            <Line key={event} dataKey={event} type="monotone" stroke={FUNNEL_DAILY_CONFIG[event].color} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        PostHog, unique persons — dev and prod share one PostHog project, so these count both environments.
      </div>
    </Card>
  );
}

/** 2. Signups per day — billing rows, single series. */
export function SignupsPerDayChart({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="Signups per day (billing rows)">
      <ChartContainer config={{ n: { label: "signups", color: CHART_2 } }} className="h-[220px] w-full">
        <BarChart data={seg.daily.signups} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
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

const RUNS_CONFIG: ChartConfig = {
  autonomous_ok: { label: "autonomous", color: CHART_1 },
  human_ok: { label: "human", color: CHART_2 },
  nightly_ok: { label: "nightly CEO", color: CHART_3 },
};

/** 3. North Star — successful runs per day, stacked autonomous+human beside a separate nightly-CEO bar. */
export function NorthStarRunsChart({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="North Star — successful runs per day">
      <ChartContainer config={RUNS_CONFIG} className="h-[220px] w-full">
        <BarChart data={seg.daily.runs} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="day" tickFormatter={tickDayMonth} interval={6} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="autonomous_ok" stackId="runs" fill={CHART_1} isAnimationActive={false} />
          <Bar dataKey="human_ok" stackId="runs" fill={CHART_2} radius={[2, 2, 0, 0]} isAnimationActive={false} />
          <Bar dataKey="nightly_ok" stackId="nightly" fill={CHART_3} radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        autonomous = dispatched by a schedule or webhook. human = everything else, including the nightly CEO&apos;s
        follow-on work. The nightly CEO&apos;s own runs are shown and never counted.
      </div>
    </Card>
  );
}

/** 4. Weekly active owners — owners with ≥1 successful autonomous run. */
export function WeeklyActiveOwnersChart({ seg }: { seg: GrowthSegment }) {
  return (
    <Card title="Owners with ≥1 successful autonomous run, per week">
      <ChartContainer config={{ n: { label: "active owners", color: CHART_1 } }} className="h-[220px] w-full">
        <LineChart data={seg.retention.weekly_active_owners} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="week" tickFormatter={tickDayMonth} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <Line dataKey="n" type="monotone" stroke={CHART_1} strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
        </LineChart>
      </ChartContainer>
    </Card>
  );
}

const LADDER: GrowthOwnerStage["stage"][] = [
  "signed_up",
  "subscribed",
  "container_ready",
  "friday_first_run_delivered",
  "automation_armed",
  "first_autonomous_success",
];
const ACTIVATION_CONFIG: ChartConfig = {
  stage: { label: "ladder", color: CHART_1 },
  flag: { label: "order-independent", color: CHART_5 },
};

/** 5. Activation gate — ladder vs. order-independent counts per stage, horizontal. */
export function ActivationGateChart({ seg }: { seg: GrowthSegment }) {
  const a = seg.activation;
  const rows = LADDER.map((stage) => ({
    label: STAGE_LABEL[stage],
    stage: a.stages[stage],
    flag: stage === "signed_up" ? a.stages.signed_up : a.flags[stage],
  }));
  return (
    <Card title="Activation gate">
      <ChartContainer config={ACTIVATION_CONFIG} className="h-[220px] w-full">
        <BarChart data={rows} layout="vertical" margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis type="number" tickMargin={8} {...AXIS_TICK_PROPS} allowDecimals={false} domain={[0, "auto"]} />
          <YAxis type="category" dataKey="label" width={150} {...AXIS_TICK_PROPS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="stage" fill={CHART_1} radius={[0, 2, 2, 0]} isAnimationActive={false} />
          <Bar dataKey="flag" fill={CHART_5} radius={[0, 2, 2, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        Ladder (green) counts owners who cleared every earlier step; order-independent (brass) counts each step on
        its own — a gap between them is a stalled owner, not a missing one.
      </div>
    </Card>
  );
}

const SURVIVAL_ROWS: { key: "armed" | "paused_manual" | "auto_paused" | "archived"; label: string; color: string }[] = [
  { key: "armed", label: "armed", color: CHART_1 },
  { key: "paused_manual", label: "paused by owner", color: CHART_5 },
  { key: "auto_paused", label: "auto-paused", color: CHART_4 },
  { key: "archived", label: "archived", color: CHART_2 },
];

/** 6. Automation survival — routines ≥30 days old, one bar per outcome. */
export function AutomationSurvivalChart({ seg }: { seg: GrowthSegment }) {
  const s = seg.retention.automation_survival;
  const rows = SURVIVAL_ROWS.map((r) => ({ label: r.label, value: s[r.key], color: r.color }));
  return (
    <Card title="Automation survival (routines ≥30 days old)">
      <ChartContainer config={{}} className="h-[220px] w-full">
        <BarChart data={rows} layout="vertical" margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis type="number" tickMargin={8} {...AXIS_TICK_PROPS} allowDecimals={false} domain={[0, "auto"]} />
          <YAxis type="category" dataKey="label" width={150} {...AXIS_TICK_PROPS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="value" radius={[0, 2, 2, 0]} isAnimationActive={false}>
            {rows.map((r) => (
              <Cell key={r.label} fill={r.color} />
            ))}
          </Bar>
        </BarChart>
      </ChartContainer>
    </Card>
  );
}

const GATE_BLOCKED_CYCLE = [CHART_4, CHART_3, CHART_5, CHART_2, CHART_1];

const GATE_BLOCKED_TITLE = "provision.gate.blocked per day (CloudWatch, all owners) — last 7 days";

/** 7. provision.gate.blocked per day — one stacked series per code present.
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

const POSTHOG_RUNS_ORDER = ["autonomous", "human", "unknown"] as const;
const POSTHOG_RUNS_CONFIG: ChartConfig = {
  autonomous: { label: "autonomous", color: CHART_1 },
  human: { label: "human", color: CHART_2 },
  unknown: { label: "unknown", color: CHART_5 },
};

/** 8. PostHog cross-check — agent_run_completed by initiation, weekly. */
export function PosthogCrosscheckChart({ data }: { data: GrowthData }) {
  const rows = pivot(data.weeks, data.posthog.runs, "week", "kind", "n", POSTHOG_RUNS_ORDER);
  return (
    <Card title="PostHog cross-check — agent_run_completed by initiation">
      <ChartContainer config={POSTHOG_RUNS_CONFIG} className="h-[220px] w-full">
        <LineChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_STROKE} />
          <XAxis dataKey="bucket" tickFormatter={tickDayMonth} tickMargin={8} {...AXIS_TICK_PROPS} />
          <YAxis {...NUMERIC_Y_PROPS} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={tooltipDayLabel} />} />
          <ChartLegend content={<ChartLegendContent />} />
          {POSTHOG_RUNS_ORDER.map((kind) => (
            <Line key={kind} dataKey={kind} type="monotone" stroke={POSTHOG_RUNS_CONFIG[kind].color} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ChartContainer>
      <div className="mt-2 text-xs text-muted-foreground">
        Attribution here is lossy; the Paperclip DB above is the ledger. Counts both environments.
      </div>
    </Card>
  );
}
