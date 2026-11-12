"use client";

import { Bar, BarChart as ReBar, CartesianGrid, Line, LineChart as ReLine, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { RenderProps } from "./shared";

type SeriesProps = { labels: string[]; values: number[]; title?: string };

const config: ChartConfig = { value: { label: "Value", color: "var(--link)" } };
const data = (p: SeriesProps) => p.labels.map((label, i) => ({ label, value: p.values[i] ?? 0 }));
const summary = (p: SeriesProps) => p.labels.map((l, i) => `${l} ${p.values[i] ?? 0}`).join(", ");

function Figure({ p, children }: { p: SeriesProps; children: React.ReactNode }) {
  return (
    <figure aria-label={p.title ?? "Chart"} className="grid gap-1">
      {p.title && <figcaption className="text-caption text-muted-foreground">{p.title}</figcaption>}
      <ChartContainer config={config} className="h-40 w-full">{children as React.ReactElement}</ChartContainer>
      <p className="sr-only">{summary(p)}</p>
    </figure>
  );
}

export function BarChartView({ props }: RenderProps<SeriesProps>) {
  return (
    <Figure p={props}>
      <ReBar data={data(props)}>
        <CartesianGrid vertical={false} stroke="var(--edge)" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} />
        <YAxis width={36} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Bar dataKey="value" fill="var(--color-value)" radius={2} />
      </ReBar>
    </Figure>
  );
}

export function LineChartView({ props }: RenderProps<SeriesProps>) {
  return (
    <Figure p={props}>
      <ReLine data={data(props)}>
        <CartesianGrid vertical={false} stroke="var(--edge)" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} />
        <YAxis width={36} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line type="monotone" dataKey="value" stroke="var(--color-value)" dot={false} strokeWidth={2} />
      </ReLine>
    </Figure>
  );
}
