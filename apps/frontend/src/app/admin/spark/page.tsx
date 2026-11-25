import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getSpark } from "../_lib/api";
import { ActivationChart, DoersChart, SuccessChart } from "./charts";
import { Cohorts, Failures, Funnel, Headline } from "./sections";

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
          Desktop app metrics from PostHog; team accounts excluded. Cached up to 5 minutes.
          {data ? ` Generated ${data.generated_at}.` : ""}
        </p>
      </div>

      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Spark metrics read failed — nothing below is trustworthy."
              : `${env.source_state}: ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ") || "no detail"}. Sections fed by a failed query render empty, not zero.`
          }
        />
      ) : null}

      {data ? (
        <>
          <Headline data={data} />
          <div className="grid gap-6 xl:grid-cols-2">
            <DoersChart data={data} />
            <ActivationChart data={data} />
            <SuccessChart data={data} />
            <Funnel data={data} />
          </div>
          <Failures data={data} />
          <Cohorts data={data} />
          <p className="text-xs text-muted-foreground">
            Weekly Active Doer = a user with at least 3 task_completed in the week (Monday start). Activated = first
            task_completed within 24h of app_first_launch. Week-4 retention = a task_completed on days 22-28 after launch,
            shown once the cohort is old enough. Task success counts events. Funnel stages are distinct users in the
            window, not strictly sequential; the download stage is anonymous web traffic so it is not comparable to the rest.
          </p>
        </>
      ) : null}
    </div>
  );
}
