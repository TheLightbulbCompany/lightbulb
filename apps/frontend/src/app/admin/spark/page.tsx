import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { type SparkData, getSpark } from "../_lib/api";
import { DoersChart } from "./charts";
import { Channels, Cohorts, Funnel, Headline, Money, People, Reliability, Setup, Signals, Sources } from "./sections";

export const dynamic = "force-dynamic";
// The backend sends PostHog 3 queries at a time, so a load with nothing cached can take up to 45 seconds.
export const maxDuration = 60;

/** A section whose query failed shows a placeholder; the rest of the page still renders. */
function Section({ title, keys, off, children }: { title: string; keys: string[]; off: string[]; children: React.ReactNode }) {
  if (!keys.some((k) => off.includes(k))) return <>{children}</>;
  return (
    <Card title={title}>
      <div className="text-xs text-muted-foreground">couldn&apos;t load</div>
    </Card>
  );
}

export default async function AdminSparkPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getSpark(token) : null;
  const data = env?.data ? (env.data as SparkData & { unavailable?: string[] }) : null;
  const off = data?.unavailable ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Spark</h1>
        <p className="text-xs text-muted-foreground">
          Desktop app metrics from PostHog. Installs count every first launch of a released app. Everything else counts signed-in people only. Team accounts excluded. Weeks are UTC, Monday start. Cached up to 5 minutes.
          {data ? ` Generated ${data.generated_at}.` : ""}
        </p>
      </div>

      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Spark metrics read failed — nothing below is trustworthy."
              : `${env.source_state}: ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ") || "no detail"}.${data ? " Sections that failed are marked below." : " No figures are shown."}`
          }
        />
      ) : null}

      {data ? (
        <>
          {data.truncated ? (
            <p className="text-xs text-destructive">Install list hit its 5000-row cap; older installs are missing.</p>
          ) : null}
          <Section title="Headline" keys={["wad", "brain_unavailable_installs_24h"]} off={off}>
            <Headline data={data} />
          </Section>
          <Section title="People" keys={["people"]} off={off}>
            <People data={data} />
          </Section>
          <div className="grid gap-6 xl:grid-cols-2">
            <Section title="Weekly Active Doers" keys={["wad"]} off={off}>
              <DoersChart data={data} />
            </Section>
            <Section title="Funnel" keys={["funnel", "downloads"]} off={off}>
              <Funnel data={data} />
            </Section>
          </div>
          <Section title="Setup" keys={["setup", "time_to_first_task"]} off={off}>
            <Setup data={data} />
          </Section>
          <Section title="Install cohorts" keys={["cohorts"]} off={off}>
            <Cohorts data={data} />
          </Section>
          <div className="grid gap-6 xl:grid-cols-2">
            <Section title="Channels" keys={["channels"]} off={off}>
              <Channels data={data} />
            </Section>
            <Section title="Sources" keys={["heard_from"]} off={off}>
              <Sources data={data} />
            </Section>
          </div>
          <Section title="Money" keys={["money"]} off={off}>
            <Money data={data} />
          </Section>
          <Section title="Reliability" keys={["success"]} off={off}>
            <Reliability data={data} />
          </Section>
          <Section title="Signals" keys={["signals"]} off={off}>
            <Signals data={data} />
          </Section>
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
