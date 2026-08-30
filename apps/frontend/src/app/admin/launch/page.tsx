import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getLaunchOps } from "../_lib/api";
import { LaunchAdminClient } from "./LaunchAdminClient";
import { LaunchSettingsCard } from "./LaunchSettingsCard";

export const dynamic = "force-dynamic";

export default async function AdminLaunchPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getLaunchOps(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Launch competition</h1>
        <p className="text-xs text-muted-foreground">
          The economics sheet with live inputs. {data?.enabled ? `Generated ${data.generated_at}, phase ${data.phase}.` : ""}
        </p>
      </div>
      {data ? <LaunchSettingsCard settings={data.settings} enabled={data.enabled} /> : null}
      {!env ? (
        <ErrorBanner error="Launch read failed — nothing below is trustworthy." />
      ) : data && !data.enabled ? (
        <p className="text-xs text-muted-foreground">Competition disabled — set the dates and enable it above.</p>
      ) : env.source_state !== "ok" ? (
        <ErrorBanner
          error={`Not available (${env.source_state}): ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ")}`}
        />
      ) : null}
      {data && data.enabled && env && env.source_state === "ok" ? <LaunchAdminClient data={data} /> : null}
    </div>
  );
}
