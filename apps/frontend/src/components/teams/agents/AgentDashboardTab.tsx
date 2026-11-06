// apps/frontend/src/components/teams/agents/AgentDashboardTab.tsx

// Ported from upstream Paperclip's AgentDetail page — the Overview (dashboard)
// view (`AgentOverview` + `LatestRunCard` + `SummaryRow` +
// `getRunSnapshotIssueId` / `resolveLatestRunNavigation`)
// (paperclip/ui/src/pages/AgentDetail.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). The Live/Latest Run card (live-run pick,
// snapshot→task resolution with the fallback issue fetch, 3-line summary
// excerpt), the Identity card, Capabilities and Recent Tasks are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - react-query -> useTeamsApi reads: runs from /teams/agents/{id}/runs (the
//                                       BFF fences the agent to the caller's
//                                       company), tasks from
//                                       /teams/issues?participantAgentId=, the
//                                       fallback task from /teams/issues/{id}
//   - the "Harness / Runtime" card is not ported: adapter and model live in the
//     adapter config, which the BFF redacts (its apiKey is the owner's hook
//     token); our Overview tab shows the Hermes profile's model instead
//   - the "Skills" card is not ported: skills are the Hermes profile's (Overview/
//     Tools tabs), not Paperclip's agent skill snapshot
//   - the "Audit" links (activity / runs / costs / budgets) are not ported:
//     runs is the Runs tab beside this one, and costs/budgets are Metronome's
//   - react-router Link -> in-shell anchors: runs -> the run-detail panel,
//     tasks -> the issue-detail panel, reports-to -> the agent inspector,
//     "See All" -> the Tasks panel
//   - StatusBadge / StatusGlyph / Badge / MarkdownBody -> a status pill /
//     StatusIcon / SourceBadge / MarkdownView; the blue live palette -> brand tokens

"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import type { CompanyAgent, Issue, RunDetail as HeartbeatRun } from "@/components/teams/shared/types";
import { RunStatusIcon, SourceBadge, runSummaryText, sortRunsNewestFirst } from "./AgentRunsTab";

// Upstream AGENT_ROLE_LABELS (packages/shared/src/constants.ts).
const roleLabels: Record<string, string> = {
  ceo: "Coordinator",
  cto: "CTO",
  cmo: "CMO",
  cfo: "CFO",
  security: "Security",
  engineer: "Engineer",
  designer: "Designer",
  pm: "PM",
  qa: "QA",
  devops: "DevOps",
  researcher: "Researcher",
  general: "General",
};

/** Upstream's react-router Link, as an in-shell anchor (new-tab still works). */
function Link({ to, className, children }: { to: string; className?: string; children: React.ReactNode }) {
  return (
    <a href={to} className={className} onClick={(e) => handleShellLinkClick(e, to)}>
      {children}
    </a>
  );
}

function StatusPill({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span className="rounded-full border border-[var(--edge)] px-2 py-0.5 text-2xs capitalize text-[var(--midnight-soft)]">
      {status.replace(/_/g, " ")}
    </span>
  );
}

function SummaryRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-caption text-[var(--midnight-muted)]">{label}</span>
      <div className="flex items-center gap-1">{children}</div>
    </div>
  );
}

export type LatestRunIssue = { id: string; title: string; status: string; identifier?: string | null };

/**
 * The id of the issue a run works on, read from its context snapshot. Newer
 * snapshots use `issueId`; older ones use `taskId`. Returns undefined for pure
 * timer heartbeats that carry no task reference.
 */
export function getRunSnapshotIssueId(run: Pick<HeartbeatRun, "contextSnapshot">): string | undefined {
  const ctx = run.contextSnapshot as Record<string, unknown> | null | undefined;
  const issueId = ctx?.issueId ?? ctx?.taskId;
  return issueId ? String(issueId) : undefined;
}

/**
 * Resolve the Live Run section's two navigation destinations and the task (if
 * any) the run works on. The heading always links to the run detail; the
 * running row links to the task when the snapshot resolves to a known issue,
 * otherwise falls back to the run detail.
 */
export function resolveLatestRunNavigation(
  run: Pick<HeartbeatRun, "id" | "contextSnapshot">,
  issuesById: Map<string, LatestRunIssue>,
): { task: LatestRunIssue | undefined; runHref: string; rowHref: string } {
  const issueId = getRunSnapshotIssueId(run);
  const task = issueId ? issuesById.get(issueId) : undefined;
  const runHref = teamsDetailUrl("run", run.id);
  const rowHref = task ? teamsDetailUrl("issue", task.identifier ?? task.id) : runHref;
  return { task, runHref, rowHref };
}

// Extract a clean 2-3 line excerpt: first non-empty, non-header, non-list-mark lines
function summaryExcerpt(summaryRaw: string): string {
  if (!summaryRaw) return "";
  const lines = summaryRaw
    .replace(/^#{1,6}\s+/gm, "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0 && !l.startsWith("---") && !l.startsWith("|") && !l.startsWith("```") && !/^[-*>]/.test(l) && !/^\d+\./.test(l));
  const excerpt: string[] = [];
  let chars = 0;
  for (const line of lines) {
    if (excerpt.length >= 3 || chars + line.length > 280) break;
    excerpt.push(line);
    chars += line.length;
  }
  return excerpt.join(" ");
}

function LatestRunCard({
  runs,
  issuesById,
}: {
  runs: HeartbeatRun[];
  issuesById: Map<string, LatestRunIssue>;
}) {
  const { read } = useTeamsApi();
  const sorted = sortRunsNewestFirst(runs);

  const liveRun = sorted.find((r) => r.status === "running" || r.status === "queued");
  const run = liveRun ?? sorted[0];

  // The tasks list this card resolves against is bounded (server page limit),
  // so a live run can reference a valid issue that isn't on the loaded page.
  // When the snapshot points at an issue we don't already have, fetch it
  // directly so the running row always links to the task.
  const snapshotIssueId = run ? getRunSnapshotIssueId(run) : undefined;
  const needsFallbackFetch = !!snapshotIssueId && !issuesById.has(snapshotIssueId);
  const { data: fallbackIssue } = read<Issue>(
    needsFallbackFetch ? `/issues/${encodeURIComponent(snapshotIssueId)}` : null,
  );

  const summary = summaryExcerpt(run ? runSummaryText(run) : "");

  if (!run) return null;

  const isLive = run.status === "running" || run.status === "queued";
  const effectiveIssuesById =
    fallbackIssue && snapshotIssueId
      ? new Map(issuesById).set(snapshotIssueId, {
          id: fallbackIssue.id,
          title: fallbackIssue.title,
          status: fallbackIssue.status,
          identifier: fallbackIssue.identifier,
        })
      : issuesById;
  const { task, runHref, rowHref } = resolveLatestRunNavigation(run, effectiveIssuesById);

  return (
    <div className="space-y-3">
      <div className="flex w-full items-center justify-between">
        <Link to={runHref} className="no-underline">
          <h3 className="flex items-center gap-2 text-ui font-medium text-[var(--midnight)]">
            {isLive && (
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-pulse rounded-full bg-[var(--ok)] opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--ok)]" />
              </span>
            )}
            <span>{isLive ? "Live Run" : "Latest Run"}</span>
            <span className="font-mono text-caption font-normal text-[var(--midnight-muted)]">
              &middot; {run.id.slice(0, 8)}
            </span>
          </h3>
        </Link>
      </div>

      <Link
        to={rowHref}
        className={cn(
          "block w-full cursor-pointer space-y-2 rounded-lg border p-4 no-underline transition-colors hover:bg-[var(--cream-deep)]",
          isLive ? "border-[var(--ok)]" : "border-[var(--edge)]",
        )}
      >
        <div className="flex items-center gap-2">
          <RunStatusIcon status={run.status} />
          <StatusPill status={run.status} />
          {task ? (
            <>
              <StatusIcon status={task.status} />
              <span className="font-mono text-caption text-[var(--midnight-muted)]">
                {task.identifier ?? task.id.slice(0, 8)}
              </span>
              <span className="truncate text-caption">{task.title}</span>
            </>
          ) : (
            <>
              <span className="font-mono text-caption text-[var(--midnight-muted)]">{run.id.slice(0, 8)}</span>
              <SourceBadge source={run.invocationSource} />
            </>
          )}
          <span className="ml-auto text-caption text-[var(--midnight-muted)]">
            {run.createdAt ? timeAgo(run.createdAt) : ""}
          </span>
        </div>

        {summary && (
          <div className="max-h-16 overflow-hidden text-ui">
            <MarkdownView>{summary}</MarkdownView>
          </div>
        )}
      </Link>
    </div>
  );
}

export function AgentDashboardTab({
  agent,
  agents,
}: {
  /** The inspected agent's Teams roster row (Paperclip agent). */
  agent: CompanyAgent;
  /** The whole roster, for "Reports to" and the direct-report count. */
  agents: CompanyAgent[];
}) {
  const { read } = useTeamsApi();
  const { data: runsData } = read<{ runs: HeartbeatRun[] }>(`/agents/${encodeURIComponent(agent.id)}/runs`);
  const { data: issuesData } = read<{ issues: Issue[] }>(
    `/issues?participantAgentId=${encodeURIComponent(agent.id)}`,
  );
  const runs = runsData?.runs ?? [];
  const assignedIssues = useMemo(() => issuesData?.issues ?? [], [issuesData]);
  const issuesById = useMemo(() => {
    const map = new Map<string, LatestRunIssue>();
    for (const issue of assignedIssues) map.set(issue.id, issue);
    return map;
  }, [assignedIssues]);
  const reportsToAgent = agent.reportsTo ? agents.find((a) => a.id === agent.reportsTo) : undefined;
  const directReportCount = agents.filter((a) => a.reportsTo === agent.id).length;

  return (
    <div className="space-y-6 py-4">
      <LatestRunCard runs={runs} issuesById={issuesById} />

      <div className="grid gap-4 md:grid-cols-2">
        <section className="rounded-lg border border-[var(--edge)] p-4" aria-labelledby="agent-identity-heading">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h3 id="agent-identity-heading" className="text-ui font-medium">Identity</h3>
            <StatusPill status={agent.status} />
          </div>
          <div className="space-y-3">
            <SummaryRow label="Role">
              <span className="text-ui">{agent.role ? roleLabels[agent.role] ?? agent.role : "Not set"}</span>
            </SummaryRow>
            <SummaryRow label="Title"><span className="text-ui">{agent.title ?? "Not set"}</span></SummaryRow>
            <SummaryRow label="Reports to">
              {reportsToAgent ? (
                <Link className="text-ui hover:underline" to={agentInspectorUrl(reportsToAgent.id)}>
                  {reportsToAgent.name}
                </Link>
              ) : <span className="text-ui">Board</span>}
            </SummaryRow>
            <SummaryRow label="Direct reports"><span className="text-ui tabular-nums">{directReportCount}</span></SummaryRow>
          </div>
        </section>

        <section className="rounded-lg border border-[var(--edge)] p-4" aria-labelledby="agent-capabilities-heading">
          <h3 id="agent-capabilities-heading" className="mb-3 text-ui font-medium">Capabilities</h3>
          {agent.capabilities?.trim() ? (
            <div className="text-ui">
              <MarkdownView>{agent.capabilities}</MarkdownView>
            </div>
          ) : (
            <p className="text-ui text-[var(--midnight-muted)]">No capability summary has been added.</p>
          )}
        </section>
      </div>

      <section className="space-y-3" aria-labelledby="agent-recent-tasks-heading">
        <div className="flex items-center justify-between">
          <h3 id="agent-recent-tasks-heading" className="text-ui font-medium">Recent Tasks</h3>
          <Link
            to={teamsPanelUrl("issues")}
            className="text-caption text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight)]"
          >
            See All &rarr;
          </Link>
        </div>
        {assignedIssues.length === 0 ? (
          <p className="text-ui text-[var(--midnight-muted)]">No recent tasks.</p>
        ) : (
          <div className="overflow-hidden rounded-lg border border-[var(--edge)]">
            {assignedIssues.slice(0, 6).map((issue) => (
              <IssueRow
                key={issue.id}
                issue={issue}
                trailingMeta={issue.updatedAt ? timeAgo(issue.updatedAt) : null}
              />
            ))}
            {assignedIssues.length > 6 && (
              <div className="border-t border-[var(--edge)] px-3 py-2 text-center text-caption text-[var(--midnight-muted)]">
                +{assignedIssues.length - 6} more tasks
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
