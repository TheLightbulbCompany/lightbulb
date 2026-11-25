import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";

import type { SparkData } from "../_lib/api";
import { Table } from "../growth/sections";

/** A share, or an em dash when there is nothing to divide by (never a fake 0%). */
export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

const STAGE_LABEL: Record<string, string> = {
  spark_download_clicked: "Download clicked",
  app_first_launch: "First launch",
  signin_completed: "Signed in",
  onboarding_completed: "Onboarding done",
  task_completed: "First task completed",
  subscription_started: "Paid",
};

export function Headline({ data }: { data: SparkData }) {
  const last = data.cohorts[data.cohorts.length - 1];
  const rate = data.task_success.weekly[data.task_success.weekly.length - 1]?.rate ?? null;
  const unhealthy = data.reliability.unhealthy_installs_24h;
  return (
    <Card title="Headline">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricStat label="Weekly Active Doers" value={data.wad.rolling_7d} hint="last 7 days, >=3 tasks" />
        <MetricStat label="installs this week" value={last?.installs ?? 0} hint={`activation ${pct(last?.activation_rate)}`} />
        <MetricStat label="task success this week" value={pct(rate)} />
        <MetricStat
          label="installs with brain != ok"
          value={unhealthy}
          hint="last 24h, from health events"
          tone={unhealthy > 0 ? "error" : "default"}
        />
      </div>
    </Card>
  );
}

export function Funnel({ data }: { data: SparkData }) {
  const top = data.funnel[0]?.users ?? 0;
  return (
    <Card title="Funnel, last 30 days (distinct users per stage)">
      <Table
        head={["Stage", "Users", "of first stage"]}
        rows={data.funnel.map((f) => [STAGE_LABEL[f.stage] ?? f.stage, f.users, top ? pct(f.users / top) : "—"])}
      />
    </Card>
  );
}

export function Cohorts({ data }: { data: SparkData }) {
  const rows = data.cohorts
    .filter((c) => c.installs > 0)
    .map((c) => [
      c.week,
      c.installs,
      c.activated,
      pct(c.activation_rate),
      data.platforms.map((p) => `${p} ${c.activated_by_platform[p] ?? 0}`).join(", ") || "—",
      c.w4_retention === null ? "too early" : pct(c.w4_retention),
    ]);
  return (
    <Card title="Install cohorts">
      <Table head={["Week", "Installs", "Activated", "Activation", "Activated by platform", "Week-4 retention"]} rows={rows} />
    </Card>
  );
}

export function Failures({ data }: { data: SparkData }) {
  const t = data.task_success;
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card title="Top task_failed reasons, 7d">
        <Table head={["Reason", "Count"]} rows={t.top_failed_reasons.map((r) => [r.key, r.count])} />
      </Card>
      <Card title="Top problem_raised kinds, 7d">
        <Table head={["Kind", "Count"]} rows={t.top_problem_kinds.map((r) => [r.key, r.count])} />
      </Card>
    </div>
  );
}
