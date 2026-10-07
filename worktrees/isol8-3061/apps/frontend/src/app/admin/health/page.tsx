import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { MetricStat } from "@/components/admin/MetricStat";
import { StatusChip } from "@/components/admin/StatusChip";
import { Tile } from "@/components/admin/Tile";
import { getSystemHealth } from "@/app/admin/_lib/api";
import { truncate } from "@/lib/utils";

export const metadata = { title: "Health · Admin" };

// ---------------------------------------------------------------------------
// Local response shape — the shared `SystemHealth` type in
// `_lib/api.ts` is intentionally permissive (`unknown`/index signature) so the
// SC can degrade if the backend evolves. We narrow here so the rendering code
// stays readable without sprinkling casts everywhere.
// ---------------------------------------------------------------------------

type UpstreamProbe = {
  status?: string;
  latency_ms?: number;
  error?: string;
  http_status?: number;
};

type FleetCounts = {
  running?: number;
  provisioning?: number;
  stopped?: number;
  error?: number;
  total?: number;
};

type BackgroundTaskState = {
  status?: string;
  error?: string | null;
};

type RecentError = {
  timestamp?: string;
  user_id?: string | null;
  message?: string;
  correlation_id?: string | null;
};

type HealthShape = {
  upstreams?: Record<string, UpstreamProbe>;
  fleet?: FleetCounts;
  background_tasks?: Record<string, BackgroundTaskState>;
  recent_errors?: RecentError[];
};

const UPSTREAM_LABELS: Record<string, string> = {
  clerk: "Clerk",
  stripe: "Stripe",
  ddb: "DynamoDB",
};

const UPSTREAM_ORDER = ["clerk", "stripe", "ddb"];

const UPSTREAM_TONE: Record<string, "ok" | "warn" | "error" | "unknown"> = {
  ok: "ok",
  degraded: "warn",
  down: "error",
  unconfigured: "unknown",
};

const FLEET_TILES: Array<{ key: keyof FleetCounts; label: string }> = [
  { key: "running", label: "Running" },
  { key: "provisioning", label: "Provisioning" },
  { key: "stopped", label: "Stopped" },
  { key: "error", label: "Error" },
];

export default async function HealthPage() {
  const { getToken } = await auth();
  const token = await getToken();
  // The admin layout already enforces an authenticated admin session, so
  // `token` should always be present here. Defensive null-handling keeps the
  // SC happy in tests.
  const health = token ? ((await getSystemHealth(token)) as HealthShape | null) : null;

  if (!health) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-foreground">System health</h1>
        <ErrorBanner error="System health endpoint unreachable" variant="error" />
      </div>
    );
  }

  const upstreams = health.upstreams ?? {};
  const fleet = health.fleet ?? {};
  const tasks = health.background_tasks ?? {};
  const recentErrors = (health.recent_errors ?? []).slice(0, 10);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-foreground">System health</h1>

      {/* Upstream probes */}
      <Card title="Upstreams">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {UPSTREAM_ORDER.map((key) => {
            const probe = upstreams[key] ?? {};
            const label = UPSTREAM_LABELS[key] ?? key;
            return (
              <Tile
                key={key}
                title={label}
                label={probe.status ?? "unknown"}
                sub={probe.error}
                tone={UPSTREAM_TONE[probe.status ?? ""] ?? "unknown"}
              >
                {probe.latency_ms !== undefined ? (
                  <div className="mt-1 font-mono text-xs text-muted-foreground">
                    {probe.latency_ms}ms
                  </div>
                ) : null}
              </Tile>
            );
          })}
        </div>
      </Card>

      {/* Fleet counts */}
      <Card title="Container fleet">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {FLEET_TILES.map((tile) => {
            const value = fleet[tile.key] ?? 0;
            return (
              <MetricStat
                key={tile.key}
                label={tile.label}
                value={value}
                tone={tile.key === "error" && value > 0 ? "error" : "default"}
              />
            );
          })}
        </div>
        <div className="mt-3 text-xs text-muted-foreground">
          Total containers: <span className="font-mono text-foreground">{fleet.total ?? 0}</span>
        </div>
      </Card>

      {/* Background tasks */}
      <Card title="Background tasks">
        {Object.keys(tasks).length === 0 ? (
          <p className="text-sm text-muted-foreground">No background tasks registered.</p>
        ) : (
          <div className="overflow-hidden rounded-sm border border-border">
            {Object.entries(tasks).map(([name, state], idx) => (
              <div
                key={name}
                className={`flex items-center justify-between px-4 py-2 text-sm ${
                  idx > 0 ? "border-t border-border" : ""
                }`}
                title={state.error ?? undefined}
              >
                <span className="font-mono text-foreground">{name}</span>
                <div className="flex items-center gap-3">
                  {state.error ? (
                    <span className="max-w-[20rem] truncate font-mono text-xs text-err">
                      {state.error}
                    </span>
                  ) : null}
                  <StatusChip status={state.status ?? "unknown"} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Recent errors */}
      <Card title="Recent errors (last 24h)">
        {recentErrors.length === 0 ? (
          <p className="text-sm text-muted-foreground">No recent errors logged.</p>
        ) : (
          <div className="overflow-x-auto rounded-sm border border-border">
            <table className="w-full min-w-[720px] table-fixed text-xs sm:text-sm">
              <thead className="bg-secondary text-left text-muted-foreground">
                <tr>
                  <th className="w-48 px-3 py-2 font-medium">Timestamp</th>
                  <th className="w-40 px-3 py-2 font-medium">User</th>
                  <th className="px-3 py-2 font-medium">Message</th>
                  <th className="w-48 px-3 py-2 font-medium">Correlation ID</th>
                </tr>
              </thead>
              <tbody>
                {recentErrors.map((err, i) => (
                  <tr
                    key={`${err.timestamp ?? "ts"}-${i}`}
                    className="border-t border-border align-top"
                  >
                    <td className="px-3 py-2 font-mono text-muted-foreground">
                      {err.timestamp ?? "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-muted-foreground">
                      {err.user_id ? (
                        <Link
                          href={`/admin/users/${encodeURIComponent(err.user_id)}`}
                          className="text-info hover:underline"
                        >
                          {truncate(err.user_id, 16)}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">{"—"}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-foreground" title={err.message ?? ""}>
                      {truncate(err.message ?? "", 120)}
                    </td>
                    <td className="px-3 py-2 font-mono text-muted-foreground">
                      {err.correlation_id ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
