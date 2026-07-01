import { auth } from "@clerk/nextjs/server";

import { AutoRefresh } from "@/components/admin/AutoRefresh";
import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { StatusChip } from "@/components/admin/StatusChip";
import { Tile } from "@/components/admin/Tile";

import { getAlarms, getDeploys, getFleet } from "./_lib/api";

type AttentionItem = {
  key: string;
  title: string;
  label: string;
  tone: "error" | "unknown";
};

export default async function AdminOverviewPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const [fleet, deploys, alarms] = token
    ? await Promise.all([getFleet(token), getDeploys(token), getAlarms(token)])
    : [null, null, null];

  // Degraded when ANY of the three sources failed to read cleanly. On degraded
  // we must never print a green all-clear (a null/partial read yields empty
  // data that would otherwise look healthy).
  const degraded =
    !fleet ||
    fleet.source_state !== "ok" ||
    !deploys ||
    deploys.source_state !== "ok" ||
    !alarms ||
    alarms.source_state !== "ok";

  const counts = fleet?.data.counts ?? null;
  const grid = fleet?.data.grid ?? [];

  const attention: AttentionItem[] = [
    ...grid
      .filter((g) => g.status === "error" || g.status === "unknown")
      .map((g) => ({
        key: `container:${g.service_name}`,
        title: g.owner_id,
        label: g.status,
        tone: (g.status === "error" ? "error" : "unknown") as "error" | "unknown",
      })),
    ...(deploys?.data ?? [])
      .filter((d) => d.status.endsWith("_FAILED") || d.status.includes("ROLLBACK"))
      .map((d) => ({
        key: `deploy:${d.stack_name}`,
        title: d.stack_name,
        label: d.status,
        tone: "error" as const,
      })),
    ...(alarms?.data ?? []).map((a) => ({
      key: `alarm:${a.name}`,
      title: a.name,
      label: "in alarm",
      tone: "error" as const,
    })),
  ];

  return (
    <div className="space-y-6">
      <AutoRefresh intervalMs={20000} />
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-zinc-100">Operations</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>

      {/* Attention strip */}
      <Card title="Needs attention">
        {attention.length > 0 ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {attention.map((a) => (
              <Tile key={a.key} title={a.title} label={a.label} tone={a.tone} />
            ))}
          </div>
        ) : degraded ? (
          <p className="text-sm text-zinc-500">
            Fleet data unavailable — unable to read one or more ops sources.
          </p>
        ) : (
          <p className="text-sm text-zinc-500">All clear — nothing needs attention.</p>
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
