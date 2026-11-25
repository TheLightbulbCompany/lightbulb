import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getSpark } from "../_lib/api";
import { DoersChart } from "./charts";
import { Channels, Cohorts, Funnel, Headline, People, Reliability } from "./sections";

export const dynamic = "force-dynamic";

export default async function AdminSparkPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getSpark(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Spark</h1>
        <p className="text-xs text-muted-foreground">
          Desktop app metrics from PostHog; team accounts excluded; weeks are UTC, Monday start. Cached up to 5 minutes.
          {data ? ` Generated ${data.generated_at}.` : ""}
        </p>
      </div>

      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Spark metrics read failed — nothing below is trustworthy."
              : `${env.source_state}: ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ") || "no detail"}. No figures are shown.`
          }
        />
      ) : null}

      {data ? (
        <>
          {data.truncated ? (
            <p className="text-xs text-amber-600">Install list hit its 5000-row cap; older installs are missing.</p>
          ) : null}
          <Headline data={data} />
          <People data={data} />
          <div className="grid gap-6 xl:grid-cols-2">
            <DoersChart data={data} />
            <Funnel data={data} />
          </div>
          <Cohorts data={data} />
          <Channels data={data} />
          <Reliability data={data} />
          <p className="text-xs text-muted-foreground">
            Human task = task_completed not from automation. Weekly Active Doer = a person with human tasks on 2+ distinct UTC
            days. Activated 24h = a human task within 24h after launch (settled installs only). Activated d7 = human tasks on 2+
            days within 7 days (installs 7+ days old). Week-4 retained = a human task on days 22-28, among activated installs,
            once the cohort is mature. Partial = the unfinished week.
          </p>
        </>
      ) : null}
    </div>
  );
}
