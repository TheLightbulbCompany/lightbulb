import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getLaunchEmailCopy, getLaunchOps } from "../_lib/api";
import { LaunchAdminClient } from "./LaunchAdminClient";
import { LaunchEmailsCard } from "./LaunchEmailsCard";
import { LaunchSettingsCard } from "./LaunchSettingsCard";

export const dynamic = "force-dynamic";

export default async function AdminLaunchPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const [env, emails] = token
    ? await Promise.all([getLaunchOps(token), getLaunchEmailCopy(token)])
    : [null, null];
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Launch competition</h1>
        <p className="text-xs text-muted-foreground">
          The economics sheet with live inputs.{" "}
          {env?.source_state === "ok" && data?.enabled ? `Generated ${data.generated_at}, phase ${data.phase}.` : ""}
        </p>
      </div>
      {/* No remount `key` on either card. Both write the SAME settings row, so
          a key on `updated_at` (or on the overrides) remounted every editor on
          any save and threw away unsaved edits in the others. After a save the
          saving editor's own state already holds what it saved. */}
      {data ? <LaunchSettingsCard settings={data.settings} /> : null}
      {emails ? <LaunchEmailsCard variants={emails.variants} /> : null}
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
