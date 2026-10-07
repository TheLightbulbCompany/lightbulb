import Link from "next/link";
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
  tone: "error" | "warn" | "unknown";
  sub?: string;
  href?: string;
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
        key: `container:${g.owner_id}`,
        title: g.display_name ?? g.owner_id,
        label: g.status,
        tone: (g.status === "error" ? "error" : "unknown") as "error" | "unknown",
        sub: g.owner_id,
        href: `/admin/users/${encodeURIComponent(g.owner_id)}`,
      })),
    ...(deploys?.data ?? [])
      .filter((d) => d.status.endsWith("_FAILED") || d.status.includes("ROLLBACK"))
      .map((d) => ({
        key: `deploy:${d.stack_name}`,
        title: d.stack_name,
        label: d.status,
        tone: "error" as const,
        sub: d.root_cause ?? undefined,
        href: "/admin/deploys",
      })),
    ...(alarms?.data ?? []).map((a) => ({
      key: `alarm:${a.name}`,
      title: a.description ?? a.name,
      // Alarms outside the P/W naming convention have unknown severity: they
      // stay amber (an active alarm never renders calm zinc) without claiming
      // the red reserved for known-critical pages.
      label:
        a.severity === "critical" || a.severity === "warning"
          ? a.severity
          : "in alarm",
      tone: (a.severity === "critical" ? "error" : "warn") as "warn" | "error",
      sub: a.name,
      href: a.console_url ?? undefined,
    })),
  ];

  return (
    <div className="space-y-6">
      <AutoRefresh intervalMs={20000} />
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-foreground">Operations</h1>
        {degraded ? <StatusChip status="unknown" label="data degraded" /> : null}
      </div>

      {/* Attention strip */}
      <Card title="Needs attention">
        {attention.length > 0 ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {attention.map((a) => (
              <Tile
                key={a.key}
                title={a.title}
                label={a.label}
                tone={a.tone}
                sub={a.sub}
                href={a.href}
              />
            ))}
          </div>
        ) : degraded ? (
          <p className="text-sm text-muted-foreground">
            Fleet data unavailable — unable to read one or more ops sources.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">All clear — nothing needs attention.</p>
        )}
      </Card>

      {/* Summary tiles */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {(["running", "provisioning", "stopped", "error", "unknown"] as const).map(
          (k) => {
            const value = counts ? (counts[k] ?? 0) : 0;
            // Non-zero error counts go loud; non-zero unknowns stay calm zinc.
            const tone =
              value > 0 && (k === "error" || k === "unknown") ? k : "default";
            return (
              <Card key={k}>
                <MetricStat label={k} value={counts ? value : "—"} tone={tone} />
              </Card>
            );
          },
        )}
      </div>

      {/* Live fleet grid */}
      <Card title="Fleet">
        {grid.length > 0 ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {grid.map((g) => (
              <Link
                key={g.owner_id}
                href={`/admin/users/${encodeURIComponent(g.owner_id)}`}
                title={g.owner_id}
                className="flex items-center justify-between rounded-sm border border-border p-2 hover:bg-accent"
              >
                <span className="truncate text-xs text-muted-foreground">
                  {g.display_name ?? g.owner_id}
                </span>
                <StatusChip status={g.status} />
              </Link>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            {degraded
              ? "Fleet data unavailable."
              : "No containers provisioned yet."}
          </p>
        )}
      </Card>
    </div>
  );
}
