import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { OrgBanner } from "@/components/admin/OrgBanner";
import {
  listAgents,
  type AdminOrgContext,
  type AgentSummary,
} from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";

export const metadata = { title: "Agents \u00b7 Admin" };

interface PageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string }>;
}

// ---------------------------------------------------------------------------
// Local narrowing — `AgentSummary` from the shared API client is intentionally
// permissive (`unknown`/index signature). The list view reads a small set of
// well-known fields; we pick them out here so the JSX doesn't need casts.
// ---------------------------------------------------------------------------
interface AgentRow {
  agent_id: string;
  name: string;
  model: string;
  skills_count: number;
  last_active: string | null;
  sessions_count: number;
}

function pickString(source: AgentSummary, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = source[k];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

function pickNumber(source: AgentSummary, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = source[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (Array.isArray(v)) return v.length;
  }
  return null;
}

function toRow(raw: AgentSummary, fallbackId: string): AgentRow {
  const id = pickString(raw, "agent_id", "id") ?? fallbackId;
  return {
    agent_id: id,
    name: pickString(raw, "name", "display_name") ?? id,
    model: pickString(raw, "model", "model_id") ?? "\u2014",
    skills_count: pickNumber(raw, "skills_count", "skills") ?? 0,
    last_active: pickString(raw, "last_active", "last_active_at", "updated_at"),
    sessions_count: pickNumber(raw, "sessions_count", "sessions") ?? 0,
  };
}

export default async function AdminUserAgentsPage({ params, searchParams }: PageProps) {
  const { id } = await params;
  const { cursor } = await searchParams;

  const { getToken } = await auth();
  const token = await getToken();
  const result = token
    ? await listAgents(token, id, cursor, 50)
    : {
        agents: [],
        cursor: null,
        container_status: "unknown" as string,
        org: null as AdminOrgContext | null,
      };

  const orgBanner = result.org ? <OrgBanner org={result.org} /> : null;

  // Container in a non-running state — render explanatory empty/error states
  // rather than an empty table (CEO U1).
  if (result.container_status === "stopped") {
    return (
      <div className="space-y-6">
        <Header />
        {orgBanner}
        <EmptyState
          title="Container is stopped"
          body="The user's runtime is not running, so their agents can't be read."
        />
      </div>
    );
  }

  if (result.container_status === "none") {
    return (
      <div className="space-y-6">
        <Header />
        {orgBanner}
        <EmptyState
          title="No container provisioned"
          body="This user hasn't provisioned a container yet."
        />
      </div>
    );
  }

  if (result.container_status === "timeout" || result.container_status === "error") {
    return (
      <div className="space-y-6">
        <Header />
        {orgBanner}
        <ErrorBanner
          error={result.error || "Gateway RPC failed"}
          source="OpenClaw"
          variant="error"
        />
      </div>
    );
  }

  if (result.agents.length === 0) {
    return (
      <div className="space-y-6">
        <Header />
        {orgBanner}
        <EmptyState title="No agents yet" body="The user hasn't created any agents." />
      </div>
    );
  }

  const rows = result.agents.map((raw, i) => toRow(raw, `agent-${i}`));

  return (
    <div className="space-y-6">
      <Header />
      {orgBanner}
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full min-w-[720px] table-fixed text-sm">
          <thead className="bg-card text-left text-xs uppercase tracking-caps text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="w-48 px-3 py-2 font-medium">Model</th>
              <th className="w-20 px-3 py-2 font-medium">Skills</th>
              <th className="w-44 px-3 py-2 font-medium">Last active</th>
              <th className="w-24 px-3 py-2 font-medium">Sessions</th>
              <th className="w-20 px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const lastActive = formatRelativeTime(row.last_active);
              return (
              <tr
                key={row.agent_id}
                className="border-t border-border align-top text-foreground"
              >
                <td className="truncate px-3 py-2" title={row.name}>
                  {row.name}
                </td>
                <td className="truncate px-3 py-2 font-mono text-xs text-muted-foreground" title={row.model}>
                  {row.model}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {row.skills_count}
                </td>
                <td
                  className="px-3 py-2 font-mono text-xs text-muted-foreground"
                  title={lastActive.iso ?? undefined}
                >
                  {lastActive.label}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {row.sessions_count}
                </td>
                <td className="px-3 py-2 text-right">
                  <Link
                    href={`/admin/users/${encodeURIComponent(id)}/agents/${encodeURIComponent(row.agent_id)}`}
                    className="text-info hover:underline"
                  >
                    View
                  </Link>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {result.cursor ? (
        <div className="flex justify-center">
          <Link
            href={`/admin/users/${encodeURIComponent(id)}/agents?cursor=${encodeURIComponent(result.cursor)}`}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm text-foreground hover:bg-accent"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-foreground">Agents</h1>;
}

