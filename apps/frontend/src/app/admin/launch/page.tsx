import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getLaunchOps } from "../_lib/api";
import { LaunchAdminClient } from "./LaunchAdminClient";

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
          The economics sheet with live inputs. {data ? `Generated ${data.generated_at}, phase ${data.phase}.` : ""}
        </p>
      </div>
      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Launch read failed — nothing below is trustworthy."
              : `Not available (${env.source_state}): ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ")}`
          }
        />
      ) : null}
      {data ? <LaunchAdminClient data={data} /> : null}
    </div>
  );
}
