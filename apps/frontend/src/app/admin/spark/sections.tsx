import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";

import type { Kn, SparkData } from "../_lib/api";
import { Table } from "../growth/sections";

/** "k / n (p%)"; a dash when there is nothing to divide by (never a fake 0%). */
export const kn = (v: Kn | null | undefined) =>
  !v || v.n === 0 ? "—" : `${v.k} / ${v.n} (${Math.round((v.k / v.n) * 100)}%)`;

const weekLabel = (w: string, partial: boolean) => (partial ? `${w} (partial)` : w);

export function Headline({ data }: { data: SparkData }) {
  const unavailable = data.brain_unavailable_installs_24h;
  return (
    <Card title="Headline">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
        <MetricStat label="Weekly Active Doers" value={data.wad.last7} hint="last 7 days, human tasks on 2+ days" />
        <MetricStat label="previous 7 days" value={data.wad.prev7} />
        <MetricStat
          label="installs with brain.unavailable"
          value={unavailable}
          hint="last 24h"
          tone={unavailable > 0 ? "error" : "default"}
        />
      </div>
    </Card>
  );
}

export function Funnel({ data }: { data: SparkData }) {
  return (
    <Card title="Funnel: installs in the last 30 days reaching each step">
      <Table head={["Step", "Installs"]} rows={data.funnel.map((f) => [f.step, f.k])} />
      <p className="mt-2 text-xs text-muted-foreground">
        Download clicks (anonymous web traffic, not a step):{" "}
        {data.downloads.map((d) => `${d.key} ${d.count}`).join(", ") || "none"}. Channel attribution for non-referred
        users is not yet measurable.
      </p>
    </Card>
  );
}

export function Cohorts({ data }: { data: SparkData }) {
  return (
    <Card title="Install cohorts by week and platform">
      <Table
        head={["Week", "Platform", "Installs", "Activated 24h", "Activated d7", "Week-4 retained"]}
        rows={data.cohorts.map((c) => [
          weekLabel(c.week, c.partial),
          c.platform,
          c.installs,
          kn(c.activated_24h),
          kn(c.activated_d7),
          c.w4 ? kn(c.w4) : "too early",
        ])}
      />
    </Card>
  );
}

export function Channels({ data }: { data: SparkData }) {
  return (
    <Card title="Activated in 24h by channel (installs in the last 60 days)">
      <Table head={["Channel", "Activated 24h"]} rows={data.channels.map((c) => [c.channel, kn(c.activated_24h)])} />
    </Card>
  );
}

export function People({ data }: { data: SparkData }) {
  return (
    <Card title="People (installs in the last 60 days, newest first)">
      <Table
        head={["Installed", "Platform", "Version", "Brain", "Channel", "Furthest step", "Last task", "Email"]}
        rows={data.people.map((p) => [p.installed, p.platform, p.version, p.provider, p.channel, p.furthest, p.last_task ?? "—", p.email ?? "—"])}
      />
    </Card>
  );
}

/** Reliability guardrail, shown below the growth numbers. */
export function Reliability({ data }: { data: SparkData }) {
  const s = data.success;
  return (
    <div className="space-y-6">
      <Card title="Task success: human completed / (completed + failed)">
        <Table
          head={["Week", "Platform", "Success"]}
          rows={s.weekly.map((w) => [weekLabel(w.week, w.partial), w.platform, kn(w.success)])}
        />
      </Card>
      <div className="grid gap-6 md:grid-cols-2">
        <Card title="By app version, 30d">
          <Table head={["Version", "Success"]} rows={s.by_version.map((r) => [r.key, kn(r.success)])} />
        </Card>
        <Card title="By brain provider, 30d">
          <Table head={["Provider", "Success"]} rows={s.by_provider.map((r) => [r.key, kn(r.success)])} />
        </Card>
        <Card title="Top task_failed reasons, 7d">
          <Table head={["Reason", "Count"]} rows={s.failed_reasons.map((r) => [r.key, r.count])} />
        </Card>
        <Card title="Top problem_raised kinds, 7d">
          <Table head={["Kind", "Count"]} rows={s.problem_kinds.map((r) => [r.key, r.count])} />
        </Card>
      </div>
    </div>
  );
}
