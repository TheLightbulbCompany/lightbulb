import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { Card } from "@/components/admin/Card";
import { CodeBlock } from "@/components/admin/CodeBlock";
import { DefRow } from "@/components/admin/DefRow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { asArray, asRecord, asString } from "@/app/admin/_lib/narrow";
import { getAgentDetail } from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";

import { AgentActionsFooter } from "./AgentActionsFooter";

export const metadata = { title: "Agent detail \u00b7 Admin" };

interface PageProps {
  params: Promise<{ id: string; agent_id: string }>;
}

// ---------------------------------------------------------------------------
// Local narrowing of `AgentDetail` (intentionally permissive in the shared
// client). We pluck typed fields out per-section so the JSX below stays
// readable.
// ---------------------------------------------------------------------------

interface AgentMeta {
  agent_id?: string;
  name?: string;
  owner_id?: string;
  model?: string;
  tier?: string;
  last_active?: string;
}

interface SkillRow {
  name: string;
  version: string;
  source: string;
}

interface SessionRow {
  timestamp: string;
  status: string;
  excerpt: string;
}

function toAgentMeta(raw: unknown): AgentMeta {
  const r = asRecord(raw);
  return {
    agent_id: asString(r.agent_id) ?? asString(r.id),
    name: asString(r.name) ?? asString(r.display_name),
    owner_id: asString(r.owner_id) ?? asString(r.user_id),
    model: asString(r.model) ?? asString(r.model_id),
    tier: asString(r.tier) ?? asString(r.subscription_status),
    last_active: asString(r.last_active) ?? asString(r.updated_at),
  };
}

function toSkillRow(raw: unknown): SkillRow {
  const r = asRecord(raw);
  return {
    name: asString(r.name) ?? asString(r.id) ?? "\u2014",
    version: asString(r.version) ?? "\u2014",
    source: asString(r.source) ?? asString(r.origin) ?? "\u2014",
  };
}

function toSessionRow(raw: unknown): SessionRow {
  const r = asRecord(raw);
  return {
    timestamp:
      asString(r.timestamp) ??
      asString(r.created_at) ??
      asString(r.started_at) ??
      "\u2014",
    status: asString(r.status) ?? asString(r.state) ?? "\u2014",
    excerpt:
      asString(r.last_message_excerpt) ??
      asString(r.excerpt) ??
      asString(r.preview) ??
      "",
  };
}

export default async function AdminAgentDetailPage({ params }: PageProps) {
  const { id, agent_id: agentId } = await params;

  const { getToken } = await auth();
  const token = await getToken();
  const detail = token ? await getAgentDetail(token, id, agentId) : null;

  if (!detail) {
    return (
      <div className="space-y-6">
        <Header userId={id} agentId={agentId} title={agentId} />
        <ErrorBanner error="Agent detail unreachable" variant="error" />
      </div>
    );
  }

  if (detail.error === "container_not_running") {
    return (
      <div className="space-y-6">
        <Header userId={id} agentId={agentId} title={agentId} />
        <EmptyState
          title="Container is stopped"
          body="The user's runtime is not running, so this agent's detail can't be read."
        />
      </div>
    );
  }

  if (detail.error) {
    return (
      <div className="space-y-6">
        <Header userId={id} agentId={agentId} title={agentId} />
        <ErrorBanner error={detail.error} source="Agent runtime" variant="error" />
      </div>
    );
  }

  const meta = toAgentMeta(detail.agent);
  const skills = asArray(detail.skills).map(toSkillRow);
  const sessions = asArray(detail.sessions).slice(0, 20).map(toSessionRow);
  const config = detail.config_redacted;

  return (
    <div className="space-y-6">
      <Header
        userId={id}
        agentId={agentId}
        title={meta.name ?? agentId}
        subtitle={meta.model}
        lastActive={meta.last_active}
      />

      {/* Identity */}
      <Card title="Identity">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow label="Agent ID" value={meta.agent_id ?? agentId} mono />
          <DefRow
            label="Owner ID"
            value={
              <Link
                href={`/admin/users/${encodeURIComponent(meta.owner_id ?? id)}`}
                className="text-info hover:underline"
                title={meta.owner_id ?? id}
              >
                {meta.owner_id ?? id}
              </Link>
            }
            mono
          />
          <DefRow label="Model" value={meta.model ?? "\u2014"} mono />
          <DefRow label="Tier" value={meta.tier ?? "\u2014"} />
        </dl>
      </Card>

      {/* Skills */}
      <Card title="Skills">
        {skills.length === 0 ? (
          <p className="text-sm text-muted-foreground">No skills installed.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full min-w-[560px] table-fixed text-xs sm:text-sm">
              <thead className="bg-secondary text-left text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="w-32 px-3 py-2 font-medium">Version</th>
                  <th className="w-40 px-3 py-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((skill, i) => (
                  <tr key={`${skill.name}-${i}`} className="border-t border-border">
                    <td className="truncate px-3 py-2 text-foreground" title={skill.name}>
                      {skill.name}
                    </td>
                    <td className="px-3 py-2 font-mono text-muted-foreground">{skill.version}</td>
                    <td className="px-3 py-2 font-mono text-muted-foreground">{skill.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Config */}
      <Card title="openclaw.json (secrets redacted)">
        {config === undefined || config === null ? (
          <p className="text-sm text-muted-foreground">Config unavailable.</p>
        ) : (
          <CodeBlock
            value={
              typeof config === "string"
                ? config
                : (config as object)
            }
            language="json"
            maxHeight={500}
          />
        )}
      </Card>

      {/* Recent sessions */}
      <Card title="Recent sessions (top 20)">
        {sessions.length === 0 ? (
          <p className="text-sm text-muted-foreground">No recent sessions.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full min-w-[640px] table-fixed text-xs sm:text-sm">
              <thead className="bg-secondary text-left text-muted-foreground">
                <tr>
                  <th className="w-48 px-3 py-2 font-medium">Timestamp</th>
                  <th className="w-28 px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Last message</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((session, i) => {
                  const when = formatRelativeTime(session.timestamp);
                  return (
                  <tr key={`${session.timestamp}-${i}`} className="border-t border-border align-top">
                    <td
                      className="px-3 py-2 font-mono text-muted-foreground"
                      title={when.iso ?? undefined}
                    >
                      {when.label}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{session.status}</td>
                    <td className="px-3 py-2 text-foreground" title={session.excerpt}>
                      {session.excerpt
                        ? session.excerpt.length > 120
                          ? `${session.excerpt.slice(0, 120)}\u2026`
                          : session.excerpt
                        : "\u2014"}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Destructive actions footer */}
      <AgentActionsFooter userId={id} agentId={agentId} agentName={meta.name} />
    </div>
  );
}

function Header({
  userId,
  agentId,
  title,
  subtitle,
  lastActive,
}: {
  userId: string;
  agentId: string;
  title: string;
  subtitle?: string;
  lastActive?: string;
}) {
  const when = formatRelativeTime(lastActive);
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="space-y-1">
        <p className="text-xs uppercase tracking-caps text-muted-foreground">
          <Link
            href={`/admin/users/${encodeURIComponent(userId)}/agents`}
            className="text-info hover:underline"
          >
            Agents
          </Link>
          <span className="mx-2 text-muted-foreground">/</span>
          <span className="font-mono text-foreground">{agentId}</span>
        </p>
        <h1 className="text-2xl font-semibold text-foreground">{title}</h1>
        {subtitle ? (
          <p className="font-mono text-xs text-muted-foreground">{subtitle}</p>
        ) : null}
      </div>
      <div className="text-right text-xs text-muted-foreground">
        {lastActive ? (
          <>
            <span className="block uppercase tracking-caps">Last active</span>
            <span className="font-mono text-foreground" title={when.iso ?? undefined}>
              {when.label}
            </span>
          </>
        ) : null}
      </div>
    </div>
  );
}

