import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { getSystemHealth } from "@/app/admin/_lib/api";

export const metadata = { title: "Health \u00b7 Admin" };

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

function statusChipClasses(status?: string): string {
  switch (status) {
    case "ok":
      return "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
    case "degraded":
      return "bg-amber-500/15 text-amber-300 border-amber-500/30";
    case "down":
      return "bg-red-500/15 text-red-300 border-red-500/30";
    case "unconfigured":
      return "bg-zinc-500/15 text-zinc-300 border-zinc-500/30";
    default:
      return "bg-zinc-500/15 text-zinc-300 border-zinc-500/30";
  }
}

function taskBadgeClasses(status?: string): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-300";
    case "stopped":
    case "cancelled":
      return "bg-amber-500/15 text-amber-300";
    case "unregistered":
      return "bg-zinc-500/15 text-zinc-300";
    default:
      return "bg-zinc-500/15 text-zinc-300";
  }
}

function truncate(input: string, max: number): string {
  if (input.length <= max) return input;
  return `${input.slice(0, max)}\u2026`;
}

const FLEET_TILES: Array<{ key: keyof FleetCounts; label: string; tone: string }> = [
  { key: "running", label: "Running", tone: "text-emerald-300" },
  { key: "provisioning", label: "Provisioning", tone: "text-sky-300" },
  { key: "stopped", label: "Stopped", tone: "text-zinc-300" },
  { key: "error", label: "Error", tone: "text-red-300" },
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
        <h1 className="text-2xl font-semibold text-zinc-100">System health</h1>
        <ErrorBanner error="System health endpoint unreachable" variant="error" />
      </div>
    );
  }

  const upstreams = health.upstreams ?? {};
  const fleet = health.fleet ?? {};
  const tasks = health.background_tasks ?? {};
  const recentErrors = (health.recent_errors ?? []).slice(0, 10);

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold text-zinc-100">System health</h1>

      {/* Upstream probes */}
      <section aria-labelledby="upstreams-heading" className="space-y-3">
        <h2 id="upstreams-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Upstreams
        </h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {UPSTREAM_ORDER.map((key) => {
            const probe = upstreams[key] ?? {};
            const label = UPSTREAM_LABELS[key] ?? key;
            return (
              <div
                key={key}
                className={`flex items-center justify-between rounded-md border px-4 py-3 ${statusChipClasses(probe.status)}`}
              >
                <div className="flex flex-col">
                  <span className="text-sm font-semibold">{label}</span>
                  <span className="text-xs uppercase tracking-wide opacity-80">
                    {probe.status ?? "unknown"}
                  </span>
                </div>
                <div className="text-right text-xs opacity-80">
                  {probe.latency_ms !== undefined ? (
                    <span className="font-mono">{probe.latency_ms}ms</span>
                  ) : null}
                  {probe.error ? (
                    <div title={probe.error} className="mt-0.5 max-w-[10rem] truncate font-mono">
                      {probe.error}
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Fleet tiles */}
      <section aria-labelledby="fleet-heading" className="space-y-3">
        <h2 id="fleet-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Container fleet
        </h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {FLEET_TILES.map((tile) => (
            <div
              key={tile.key}
              className="rounded-md border border-white/10 bg-white/[0.02] p-4"
            >
              <div className={`text-2xl font-semibold ${tile.tone}`}>
                {fleet[tile.key] ?? 0}
              </div>
              <div className="mt-1 text-xs uppercase tracking-wide text-zinc-400">
                {tile.label}
              </div>
            </div>
          ))}
        </div>
        <div className="text-xs text-zinc-500">
          Total containers: <span className="font-mono text-zinc-300">{fleet.total ?? 0}</span>
        </div>
      </section>

      {/* Background tasks */}
      <section aria-labelledby="tasks-heading" className="space-y-3">
        <h2 id="tasks-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Background tasks
        </h2>
        {Object.keys(tasks).length === 0 ? (
          <p className="text-sm text-zinc-500">No background tasks registered.</p>
        ) : (
          <div className="overflow-hidden rounded-md border border-white/10 bg-white/[0.02]">
            {Object.entries(tasks).map(([name, state], idx) => (
              <div
                key={name}
                className={`flex items-center justify-between px-4 py-2 text-sm ${
                  idx > 0 ? "border-t border-white/5" : ""
                }`}
                title={state.error ?? undefined}
              >
                <span className="font-mono text-zinc-200">{name}</span>
                <div className="flex items-center gap-3">
                  {state.error ? (
                    <span className="max-w-[20rem] truncate font-mono text-xs text-red-300">
                      {state.error}
                    </span>
                  ) : null}
                  <span
                    className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${taskBadgeClasses(state.status)}`}
                  >
                    {state.status ?? "unknown"}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Recent errors */}
      <section aria-labelledby="recent-errors-heading" className="space-y-3">
        <h2 id="recent-errors-heading" className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Recent errors (last 24h)
        </h2>
        {recentErrors.length === 0 ? (
          <p className="text-sm text-zinc-500">No recent errors logged.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-white/10">
            <table className="w-full min-w-[720px] table-fixed text-xs sm:text-sm">
              <thead className="bg-zinc-900 text-left text-zinc-400">
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
                    className="border-t border-white/5 align-top"
                  >
                    <td className="px-3 py-2 font-mono text-zinc-300">
                      {err.timestamp ?? "\u2014"}
                    </td>
                    <td className="px-3 py-2 font-mono text-zinc-300">
                      {err.user_id ? (
                        <Link
                          href={`/admin/users/${encodeURIComponent(err.user_id)}`}
                          className="text-sky-300 hover:underline"
                        >
                          {truncate(err.user_id, 16)}
                        </Link>
                      ) : (
                        <span className="text-zinc-500">{"\u2014"}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-zinc-200" title={err.message ?? ""}>
                      {truncate(err.message ?? "", 120)}
                    </td>
                    <td className="px-3 py-2 font-mono text-zinc-400">
                      {err.correlation_id ?? "\u2014"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
