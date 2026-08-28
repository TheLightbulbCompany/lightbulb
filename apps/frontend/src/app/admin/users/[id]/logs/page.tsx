import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { getCloudwatchUrl, getLogs } from "@/app/admin/_lib/api";
import { asLogEntry } from "@/app/admin/_lib/narrow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { LogRow } from "@/components/admin/LogRow";

export const metadata = { title: "Logs \u00b7 Admin" };

// ---------------------------------------------------------------------------
// Filter dimensions
// ---------------------------------------------------------------------------

const ALLOWED_LEVELS = ["ERROR", "WARN", "INFO"] as const;
type LogLevel = (typeof ALLOWED_LEVELS)[number];

const HOURS_OPTIONS = [
  { value: 1, label: "1h" },
  { value: 24, label: "24h" },
  { value: 168, label: "7d" },
] as const;

function parseLevel(input: string | string[] | undefined): LogLevel {
  const v = Array.isArray(input) ? input[0] : input;
  if (v && (ALLOWED_LEVELS as readonly string[]).includes(v.toUpperCase())) {
    return v.toUpperCase() as LogLevel;
  }
  return "ERROR";
}

function parseHours(input: string | string[] | undefined): number {
  const v = Array.isArray(input) ? input[0] : input;
  const n = Number(v);
  if (Number.isFinite(n) && HOURS_OPTIONS.some((o) => o.value === n)) return n;
  return 24;
}

function parseCursor(input: string | string[] | undefined): string | undefined {
  const v = Array.isArray(input) ? input[0] : input;
  return v && v.length > 0 ? v : undefined;
}

// ---------------------------------------------------------------------------
// Filter form (server-rendered)
//
// `<form method="GET" action="/admin/users/{id}/logs">` keeps the page a
// pure Server Component — no "use client" needed for the filter UI. URL is
// the source of truth, so deep-linked filters / browser back work for free.
// ---------------------------------------------------------------------------

interface LogsFiltersProps {
  userId: string;
  level: LogLevel;
  hours: number;
}

function LogsFilters({ userId, level, hours }: LogsFiltersProps) {
  return (
    <form
      method="GET"
      action={`/admin/users/${encodeURIComponent(userId)}/logs`}
      className="flex flex-wrap items-end gap-3 rounded-md border border-border bg-card p-3"
    >
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Level
        <select
          name="level"
          defaultValue={level}
          className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground"
        >
          {ALLOWED_LEVELS.map((lvl) => (
            <option key={lvl} value={lvl}>
              {lvl}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Time window
        <select
          name="hours"
          defaultValue={String(hours)}
          className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground"
        >
          {HOURS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>

      <button
        type="submit"
        className="h-9 rounded-md border border-border bg-card px-4 text-sm text-foreground hover:bg-accent"
      >
        Apply
      </button>
    </form>
  );
}

interface LogsPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function LogsPage({ params, searchParams }: LogsPageProps) {
  const { id } = await params;
  const sp = await searchParams;
  const level = parseLevel(sp.level);
  const hours = parseHours(sp.hours);
  const cursor = parseCursor(sp.cursor);

  const { getToken } = await auth();
  const token = await getToken();
  if (!token) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold text-foreground">Logs</h1>
        <ErrorBanner error="Missing Clerk session token." />
      </div>
    );
  }

  const end = new Date();
  const start = new Date(end.getTime() - hours * 3_600_000);

  const [logs, cwUrl] = await Promise.all([
    getLogs(token, id, { level, hours, limit: 50, cursor }),
    getCloudwatchUrl(token, id, start.toISOString(), end.toISOString(), level),
  ]);

  const entries = (logs.events ?? []).map(asLogEntry);

  // Build the next-page link, preserving level + hours.
  const nextCursorHref = logs.cursor
    ? `/admin/users/${encodeURIComponent(id)}/logs?level=${encodeURIComponent(level)}&hours=${hours}&cursor=${encodeURIComponent(logs.cursor)}`
    : null;

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold text-foreground">Logs</h1>
        {cwUrl?.url ? (
          <a
            href={cwUrl.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-info hover:underline"
          >
            {"Open full search in CloudWatch \u2192"}
          </a>
        ) : null}
      </div>

      <LogsFilters userId={id} level={level} hours={hours} />

      {logs.error ? (
        <ErrorBanner error={logs.error} source="CloudWatch" />
      ) : null}

      {logs.missing ? (
        <EmptyState
          title="No log group"
          body="The backend log group does not exist yet — typically because no
            requests have run in this environment. The group is auto-created on
            the first request."
        />
      ) : entries.length === 0 ? (
        <EmptyState
          title="No matching log entries"
          body="No log lines match the current level + window. Widen the window or change the level to see more."
        />
      ) : (
        <div className="space-y-1.5">
          {entries.map((entry, i) => (
            <LogRow
              key={`${entry.timestamp}-${entry.correlation_id ?? i}`}
              entry={entry}
            />
          ))}
        </div>
      )}

      {nextCursorHref ? (
        <div className="flex justify-center">
          <Link
            href={nextCursorHref}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm text-foreground hover:bg-accent"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}
