import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getGrowth } from "../_lib/api";
import {
  AcquisitionChart,
  ActivationGateChart,
  AutomationSurvivalChart,
  GateBlockedDailyChart,
  KpiStrip,
  NorthStarRunsChart,
  PosthogCrosscheckChart,
  SignupsPerDayChart,
  WeeklyActiveOwnersChart,
} from "./charts";
import { Acquisition, Activation, Counters, Headline, NorthStar, Owners, PosthogCrosscheck, RetentionDepth } from "./sections";

export const dynamic = "force-dynamic";

const DETAIL_SUMMARY = "cursor-pointer text-sm text-zinc-300 py-2";
const DETAIL_WRAPPER = "border-t border-zinc-800 first:border-t-0";

export default async function AdminGrowthPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getGrowth(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Growth</h1>
        <p className="text-xs text-zinc-500">
          Metric set from docs/growth/2026-08-17-blitzscaling-strategy.md §3. External owners only; internal accounts below.
          {data ? ` Generated ${data.generated_at}, ${data.window_weeks}-week window.` : ""}
        </p>
      </div>

      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Growth read failed — nothing below is trustworthy."
              : `Partial read (${env.source_state}): ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ")}. Sections fed by a failed source render empty, not zero.`
          }
        />
      ) : null}

      {data ? (
        <>
          <KpiStrip seg={data.segments.external} />

          <div className="grid gap-6 xl:grid-cols-2">
            <AcquisitionChart data={data} />
            <SignupsPerDayChart seg={data.segments.external} />
            <NorthStarRunsChart seg={data.segments.external} />
            <WeeklyActiveOwnersChart seg={data.segments.external} />
            <ActivationGateChart seg={data.segments.external} />
            <AutomationSurvivalChart seg={data.segments.external} />
            <GateBlockedDailyChart data={data} />
            <PosthogCrosscheckChart data={data} />
          </div>

          <Card title="Details">
            <div>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Owners</summary>
                <Owners seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Acquisition</summary>
                <Acquisition seg={data.segments.external} data={data} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Activation</summary>
                <Activation seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>North Star</summary>
                <NorthStar seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Depth</summary>
                <RetentionDepth seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Counter-metrics</summary>
                <Counters seg={data.segments.external} data={data} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>PostHog cross-check</summary>
                <PosthogCrosscheck data={data} />
              </details>
            </div>
          </Card>

          <Card title={`Internal accounts (${data.internal_owner_ids.length}) — excluded from everything above`}>
            <details>
              <summary className={DETAIL_SUMMARY}>Show internal metrics</summary>
              <div className="space-y-6 pt-2">
                <Headline seg={data.segments.internal} audience="Internal accounts" />
                <ActivationGateChart seg={data.segments.internal} />
                <NorthStarRunsChart seg={data.segments.internal} />
              </div>
            </details>
          </Card>
        </>
      ) : null}
    </div>
  );
}
