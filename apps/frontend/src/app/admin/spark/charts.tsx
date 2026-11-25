"use client";

import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts";

import { Card } from "@/components/admin/Card";
import { ChartConfig, ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";

import type { SparkData } from "../_lib/api";

const DOERS: ChartConfig = { doers: { label: "Weekly Active Doers", color: "var(--chart-1)" } };

/** North star by week; the unfinished week is drawn faded. */
export function DoersChart({ data }: { data: SparkData }) {
  return (
    <Card title="Weekly Active Doers (human tasks on 2+ days; faded = partial week)">
      <ChartContainer config={DOERS} className="h-[220px] w-full">
        <BarChart data={data.wad.weekly} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="week" tickFormatter={(w: string) => w.slice(5).replace("-", "/")} tickLine={false} axisLine={false} className="text-2xs" />
          <YAxis width={32} allowDecimals={false} tickLine={false} axisLine={false} className="text-2xs" />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="doers" fill="var(--color-doers)" isAnimationActive={false}>
            {data.wad.weekly.map((w) => (
              <Cell key={w.week} fillOpacity={w.partial ? 0.35 : 1} />
            ))}
          </Bar>
        </BarChart>
      </ChartContainer>
    </Card>
  );
}
