import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getGrowth } from "../_lib/api";
import { Acquisition, Activation, Counters, Headline, NorthStar, Owners, PosthogCrosscheck, RetentionDepth } from "./sections";

export const dynamic = "force-dynamic";

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
          <Headline seg={data.segments.external} />
          <Owners seg={data.segments.external} />
          <Acquisition seg={data.segments.external} data={data} />
          <Activation seg={data.segments.external} />
          <NorthStar seg={data.segments.external} />
          <RetentionDepth seg={data.segments.external} />
          <Counters seg={data.segments.external} data={data} />
          <PosthogCrosscheck data={data} />
          <Card title={`Internal accounts (${data.internal_owner_ids.length}) — excluded from everything above`}>
            <div className="space-y-6">
              <Headline seg={data.segments.internal} />
              <Activation seg={data.segments.internal} />
              <NorthStar seg={data.segments.internal} />
            </div>
          </Card>
        </>
      ) : null}
    </div>
  );
}
