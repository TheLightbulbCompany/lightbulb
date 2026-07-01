import { auth } from "@clerk/nextjs/server";

import { AutoRefresh } from "@/components/admin/AutoRefresh";
import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { StatusChip } from "@/components/admin/StatusChip";
import { Tile } from "@/components/admin/Tile";

import { getFleet } from "./_lib/api";

export default async function AdminOverviewPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const fleet = token ? await getFleet(token) : null;

  const degraded = !fleet || fleet.source_state !== "ok";
  const counts = fleet?.data.counts ?? null;
  const grid = fleet?.data.grid ?? [];
  const attention = grid.filter(
    (g) => g.status === "error" || g.status === "unknown",
  );

  return (
    <div className="space-y-6">
      <AutoRefresh intervalMs={20000} />
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-zinc-100">Operations</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>

      {/* Attention strip */}
      <Card title="Needs attention">
        {attention.length === 0 ? (
          <p className="text-sm text-zinc-500">All containers healthy.</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {attention.map((g) => (
              <Tile
                key={g.service_name}
                title={g.owner_id}
                label={g.status}
                tone={g.status === "error" ? "error" : "unknown"}
              />
            ))}
          </div>
        )}
      </Card>

      {/* Summary tiles */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {(["running", "provisioning", "stopped", "error", "unknown"] as const).map(
          (k) => (
            <Card key={k}>
              <MetricStat label={k} value={counts ? (counts[k] ?? 0) : "—"} />
            </Card>
          ),
        )}
      </div>

      {/* Live fleet grid */}
      <Card title="Fleet">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {grid.map((g) => (
            <div
              key={g.service_name}
              className="flex items-center justify-between rounded border border-zinc-800 p-2"
            >
              <span className="truncate text-xs text-zinc-400">{g.owner_id}</span>
              <StatusChip status={g.status} />
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
