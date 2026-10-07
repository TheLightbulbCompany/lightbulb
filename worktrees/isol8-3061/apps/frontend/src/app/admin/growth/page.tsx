import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getGrowth } from "../_lib/api";
import {
  AcquisitionChart,
  CohortChart,
  GateBlockedDailyChart,
  HumanMessagesChart,
  KpiStrip,
  RetentionChart,
  WeeklyActiveChart,
} from "./charts";
import { Acquisition, Cohorts, Counters, Headline, Messaging, WeeklyActive, Workspaces } from "./sections";

export const dynamic = "force-dynamic";

const DETAIL_SUMMARY = "cursor-pointer text-sm text-muted-foreground py-2";
const DETAIL_WRAPPER = "border-t border-border first:border-t-0";

export default async function AdminGrowthPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getGrowth(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Growth</h1>
        <p className="text-xs text-muted-foreground">
          The five metrics defined in docs/superpowers/plans/2026-09-12-observability-audit.md §6, computed from the
          messaging table and billing rows. External workspaces only; internal accounts below.
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
          <Messaging seg={data.segments.external} data={data} />

          <div className="grid gap-6 xl:grid-cols-2">
            <AcquisitionChart data={data} seg={data.segments.external} />
            <WeeklyActiveChart seg={data.segments.external} />
            <CohortChart seg={data.segments.external} />
            <RetentionChart seg={data.segments.external} />
            <HumanMessagesChart seg={data.segments.external} />
            <GateBlockedDailyChart data={data} />
          </div>

          <Card title="Details">
            <div>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Workspaces</summary>
                <Workspaces seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Weekly active + multiplayer</summary>
                <WeeklyActive seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Cohorts — activation and trial → paid</summary>
                <Cohorts seg={data.segments.external} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Acquisition</summary>
                <Acquisition seg={data.segments.external} data={data} />
              </details>
              <details className={DETAIL_WRAPPER}>
                <summary className={DETAIL_SUMMARY}>Counter-metrics</summary>
                <Counters data={data} />
              </details>
            </div>
          </Card>

          <Card title={`Internal accounts (${data.internal_owner_ids.length}) — excluded from everything above`}>
            <details>
              <summary className={DETAIL_SUMMARY}>Show internal metrics</summary>
              <div className="space-y-6 pt-2">
                <Headline seg={data.segments.internal} audience="Internal accounts" />
                <WeeklyActiveChart seg={data.segments.internal} />
              </div>
            </details>
          </Card>

          <p className="text-xs text-muted-foreground">
            Definitions are binding and live in docs/superpowers/plans/2026-09-12-observability-audit.md §6. Activated =
            within 7 days of the trial start, one human sent ≥5 room messages on ≥2 distinct UTC days AND the workspace
            received ≥1 agent message. Weekly active = ≥1 human room message that ISO week; multiplayer = ≥2 distinct
            human senders. Answered = an agent message in the same room within 120s of an addressed human message.
            Paying = subscription_status &ldquo;active&rdquo;. W4 retention = an activated workspace with ≥1 human
            message in the 4th ISO week after activation.
          </p>
        </>
      ) : null}
    </div>
  );
}
