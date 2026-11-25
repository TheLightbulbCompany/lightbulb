"use client";

import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import { Card } from "@/components/admin/Card";
import { ChartConfig, ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";

import type { SparkData } from "../_lib/api";

const AXIS = { tickLine: false as const, axisLine: false as const, className: "text-2xs" };
const tick = (w: string) => w.slice(5).replace("-", "/");

const DOERS: ChartConfig = { doers: { label: "Weekly Active Doers", color: "var(--chart-1)" } };
const SUCCESS: ChartConfig = { pct: { label: "Task success %", color: "var(--chart-1)" } };
const ACTIVATION: ChartConfig = { activation: { label: "Activation %", color: "var(--chart-2)" } };

/** North star: users with >=3 task_completed in the week. */
export function DoersChart({ data }: { data: SparkData }) {
  return (
    <Card title="Weekly Active Doers (>=3 tasks completed in the week)">
      <ChartContainer config={DOERS} className="h-[220px] w-full">
        <BarChart data={data.wad.weekly} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="week" tickFormatter={tick} {...AXIS} />
          <YAxis width={32} allowDecimals={false} {...AXIS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="doers" fill="var(--color-doers)" isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
    </Card>
  );
}

/** Task success % per week; weeks with no tasks are gaps, not zeros. */
export function SuccessChart({ data }: { data: SparkData }) {
  const rows = data.task_success.weekly.map((w) => ({ week: w.week, pct: w.rate === null ? null : Math.round(w.rate * 100) }));
  return (
    <Card title="Task success rate (completed / completed + failed)">
      <ChartContainer config={SUCCESS} className="h-[220px] w-full">
        <LineChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="week" tickFormatter={tick} {...AXIS} />
          <YAxis width={32} domain={[0, 100]} {...AXIS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Line dataKey="pct" stroke="var(--color-pct)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
        </LineChart>
      </ChartContainer>
    </Card>
  );
}

/** Activation rate per weekly install cohort; cohorts with no installs are gaps. */
export function ActivationChart({ data }: { data: SparkData }) {
  const rows = data.cohorts.map((c) => ({
    week: c.week,
    activation: c.activation_rate === null ? null : Math.round(c.activation_rate * 100),
  }));
  return (
    <Card title="Activation rate by install cohort (first task within 24h)">
      <ChartContainer config={ACTIVATION} className="h-[220px] w-full">
        <BarChart data={rows} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="week" tickFormatter={tick} {...AXIS} />
          <YAxis width={32} domain={[0, 100]} {...AXIS} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="activation" fill="var(--color-activation)" isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
    </Card>
  );
}
