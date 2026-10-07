import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { StatusChip } from "@/components/admin/StatusChip";
import { getDeploys } from "../_lib/api";

export const metadata = { title: "Deploys · Admin" };

function deployTone(status: string): string {
  if (status.endsWith("_FAILED") || status.includes("ROLLBACK")) return "error";
  if (status.endsWith("_IN_PROGRESS")) return "provisioning";
  if (status.endsWith("_COMPLETE")) return "running";
  return "unknown";
}

export default async function DeploysPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const res = token ? await getDeploys(token) : null;
  const rows = res?.data ?? [];
  const degraded = !res || res.source_state !== "ok";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-foreground">Deploys</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>
      <Card>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {degraded ? "Deploy data unavailable." : "No Lightbulb stacks found."}
          </p>
        ) : (
          <div className="space-y-2">
            {rows.map((r) => (
              <div key={r.stack_name} className="rounded-sm border border-border p-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm text-foreground">{r.stack_name}</span>
                  <StatusChip status={deployTone(r.status)} label={r.status} />
                </div>
                {r.root_cause ? (
                  <p className="mt-2 text-xs text-err">Root cause: {r.root_cause}</p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
