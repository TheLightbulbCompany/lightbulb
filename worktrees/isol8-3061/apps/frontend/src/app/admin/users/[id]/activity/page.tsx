import { auth } from "@clerk/nextjs/server";

import {
  getCloudwatchUrl,
  getLogs,
  getPosthog,
} from "@/app/admin/_lib/api";
import { asLogEntry } from "@/app/admin/_lib/narrow";
import { formatRelativeTime } from "@/app/admin/_lib/time";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { LogRow } from "@/components/admin/LogRow";

export const metadata = { title: "Activity \u00b7 Admin" };

// ---------------------------------------------------------------------------
// Local response narrowings
//
// `_lib/api.ts` types `events: unknown[]` so the SC degrades if the backend
// shape evolves. We narrow per-render to keep call sites readable.
// ---------------------------------------------------------------------------

interface PosthogEvent {
  timestamp?: string;
  event?: string;
  properties?: Record<string, unknown>;
  session_id?: string | null;
}

// The Watch-session link must target the PostHog UI host, NOT the
// ingestion host. NEXT_PUBLIC_POSTHOG_HOST holds the ingestion URL
// (e.g. `https://us.i.posthog.com`) because the frontend rewrites
// `/ingest/*` → that host for capture. The replay UI lives at
// `https://us.posthog.com` (strip the `.i.` subdomain). PostHogProvider.tsx
// hardcodes `POSTHOG_UI_HOST = "https://us.posthog.com"` for the same
// reason — we mirror that derivation here.
const POSTHOG_UI_HOST_DEFAULT = "https://us.posthog.com";

function posthogUiHost(): string {
  const ingest = process.env.NEXT_PUBLIC_POSTHOG_HOST;
  if (!ingest) return POSTHOG_UI_HOST_DEFAULT;
  // Recognize the PostHog-hosted ingestion pattern and derive the UI host.
  // eu.i.posthog.com → eu.posthog.com ; us.i.posthog.com → us.posthog.com.
  // Self-hosted / unknown shapes pass through unchanged.
  return ingest.replace(/\.i\.posthog\.com(\/|$)/, ".posthog.com$1");
}

function asPosthogEvent(raw: unknown): PosthogEvent {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    timestamp: typeof r.timestamp === "string" ? r.timestamp : undefined,
    event: typeof r.event === "string" ? r.event : undefined,
    properties:
      r.properties && typeof r.properties === "object"
        ? (r.properties as Record<string, unknown>)
        : undefined,
    session_id:
      typeof r.session_id === "string" ? r.session_id : null,
  };
}

// PostHog property previews — render the whole object as JSON inside <details>
// for now. Cheap and good enough: admins typically only care about `path`,
// `current_url`, `$browser`, etc. and can scan the JSON. A polished property
// table can come later without breaking the data shape.
function PropertiesDetails({
  properties,
}: {
  properties?: Record<string, unknown>;
}) {
  if (!properties || Object.keys(properties).length === 0) return null;
  return (
    <details className="mt-1 text-xs text-muted-foreground">
      <summary className="cursor-pointer select-none text-muted-foreground hover:text-foreground">
        Properties
      </summary>
      <pre className="mt-2 max-h-64 overflow-auto rounded-md border border-border bg-background p-2 font-mono text-caption text-muted-foreground">
        {JSON.stringify(properties, null, 2)}
      </pre>
    </details>
  );
}

interface ActivityPageProps {
  params: Promise<{ id: string }>;
}

export default async function ActivityPage({ params }: ActivityPageProps) {
  const { id } = await params;
  const { getToken } = await auth();
  const token = await getToken();
  // Layout already gates admin access, but typeguard for the Server Action API.
  if (!token) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold text-foreground">Activity</h1>
        <ErrorBanner error="Missing Clerk session token." />
      </div>
    );
  }

  // 24-hour CloudWatch window for both the inline error preview AND the
  // "open in CloudWatch" deep link button.
  const end = new Date();
  const start = new Date(end.getTime() - 86_400_000);
  const startIso = start.toISOString();
  const endIso = end.toISOString();

  const [posthog, logs, cwUrl] = await Promise.all([
    getPosthog(token, id),
    getLogs(token, id, { level: "ERROR", hours: 24, limit: 20 }),
    getCloudwatchUrl(token, id, startIso, endIso, "ERROR"),
  ]);

  const logEntries = (logs.events ?? []).map(asLogEntry);
  const posthogEvents = (posthog.events ?? []).map(asPosthogEvent);

  return (
    <div className="space-y-10">
      <h1 className="text-xl font-semibold text-foreground">Activity</h1>

      {/* --- Recent error logs ----------------------------------------- */}
      <section aria-labelledby="recent-logs-heading" className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2
            id="recent-logs-heading"
            className="text-sm font-medium uppercase tracking-caps text-muted-foreground"
          >
            Recent error logs (last 24h)
          </h2>
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

        {logs.error ? (
          <ErrorBanner error={logs.error} source="CloudWatch" />
        ) : null}

        {logs.missing ? (
          <EmptyState
            title="No log group"
            body="The backend log group is not yet created. Run a request to populate it."
          />
        ) : logEntries.length === 0 ? (
          <EmptyState
            title="No errors in the last 24h"
            body="The user hasn't hit any backend errors recently."
          />
        ) : (
          <div className="space-y-1.5">
            {logEntries.slice(0, 20).map((entry, i) => (
              <LogRow
                key={`${entry.timestamp}-${entry.correlation_id ?? i}`}
                entry={entry}
              />
            ))}
          </div>
        )}
      </section>

      {/* --- PostHog timeline ------------------------------------------ */}
      <section aria-labelledby="posthog-heading" className="space-y-3">
        <h2
          id="posthog-heading"
          className="text-sm font-medium uppercase tracking-caps text-muted-foreground"
        >
          PostHog timeline
        </h2>

        {posthog.stubbed ? (
          <ErrorBanner
            error="POSTHOG_PROJECT_API_KEY not configured."
            variant="warning"
            source="PostHog"
          />
        ) : null}

        {posthog.error ? (
          <ErrorBanner error={posthog.error} source="PostHog" />
        ) : null}

        {!posthog.stubbed && posthog.missing ? (
          <EmptyState
            title="No PostHog activity recorded"
            body="This user may not have visited the frontend yet."
          />
        ) : !posthog.stubbed && posthogEvents.length === 0 ? (
          <EmptyState
            title="No recent events"
            body="No PostHog events captured for this user in the recent window."
          />
        ) : (
          <ul className="space-y-1.5">
            {posthogEvents.map((evt, i) => {
              const when = formatRelativeTime(evt.timestamp);
              return (
              <li
                key={`${evt.timestamp ?? "ts"}-${i}`}
                className="rounded-md border border-border bg-card px-3 py-2 text-xs"
              >
                <div className="flex items-center gap-3">
                  <time
                    className="font-mono text-muted-foreground"
                    dateTime={when.iso ?? evt.timestamp ?? ""}
                    title={when.iso ?? undefined}
                  >
                    {when.label}
                  </time>
                  <span className="font-semibold text-foreground">
                    {evt.event ?? "(unnamed)"}
                  </span>
                  {evt.session_id ? (
                    <a
                      href={`${posthogUiHost()}/replay/${encodeURIComponent(evt.session_id)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="ml-auto text-info hover:underline"
                    >
                      {"Watch session \u2192"}
                    </a>
                  ) : null}
                </div>
                <PropertiesDetails properties={evt.properties} />
              </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
