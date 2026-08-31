"use client";

// apps/frontend/src/components/teams/panels/DashboardPanel.tsx
//
// Ported from upstream Paperclip's Dashboard
// (paperclip/ui/src/pages/Dashboard.tsx) (MIT, (c) 2025 Paperclip AI).
// 4 metric tiles + 4 charts + recent activity feed. Drops upstream's
// ActiveAgentsPanel, plugin slots, onboarding empty-state (handled
// elsewhere in our TeamsLayout), and configurable date ranges (14d hardcoded).
// Editorial reskin: tokens (cream/ink/mute/forest/edge) + Lora display
// for masthead H1 + section H2s; class-only swap, hooks/data preserved.

import Link from "next/link";
import { Bot, CircleDot, DollarSign, ShieldCheck } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { MetricCard } from "@/components/teams/dashboard/MetricCard";
import {
  ChartCard,
  RunActivityChart,
  PriorityChart,
  IssueStatusChart,
  SuccessRateChart,
} from "@/components/teams/dashboard/ActivityCharts";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { ActiveAgentsPanel } from "@/components/teams/dashboard/ActiveAgentsPanel";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { PanelMasthead } from "@/components/teams/shell";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import {
  handleShellLinkClick,
  teamsDetailUrl,
  teamsPanelUrl,
} from "@/lib/shellNav";
import type {
  CompanyAgent,
  DashboardResponse,
  DashboardSummary,
  Issue,
} from "@/components/teams/shared/types";

/* ---------- helpers ---------- */

function formatCents(cents: number): string {
  return `$${(cents / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const EMPTY_DASHBOARD: DashboardSummary = {
  agents: {},
  tasks: {},
  runActivity: [],
  pendingApprovals: 0,
  budgets: { pendingApprovals: 0 },
};

/** Normalize the BFF /issues envelope (array | {items} | {issues}) to Issue[]. */
function normalizeIssues(data: unknown): Issue[] {
  if (Array.isArray(data)) return data as Issue[];
  if (data && typeof data === "object") {
    const obj = data as { items?: unknown; issues?: unknown };
    if (Array.isArray(obj.items)) return obj.items as Issue[];
    if (Array.isArray(obj.issues)) return obj.issues as Issue[];
  }
  return [];
}

/**
 * Charts ported from upstream consume the lighter `{priority, createdAt: Date}`
 * / `{status, createdAt: Date}` shape. Map our richer Issue rows down to that
 * minimal projection, dropping rows without a priority/status or createdAt
 * since they cannot be plotted on the date axis anyway.
 */
function toPriorityRows(
  issues: Issue[],
): { priority: string; createdAt: Date }[] {
  const out: { priority: string; createdAt: Date }[] = [];
  for (const i of issues) {
    if (!i.priority || !i.createdAt) continue;
    out.push({ priority: i.priority, createdAt: new Date(i.createdAt) });
  }
  return out;
}

function toStatusRows(issues: Issue[]): { status: string; createdAt: Date }[] {
  const out: { status: string; createdAt: Date }[] = [];
  for (const i of issues) {
    if (!i.status || !i.createdAt) continue;
    out.push({ status: i.status, createdAt: new Date(i.createdAt) });
  }
  return out;
}

/** Most-recently-touched timestamp for ordering (updatedAt, falling back to
 * createdAt). Returns 0 for rows with neither so they sort last. */
function recencyOf(issue: Issue): number {
  const ts = issue.updatedAt ?? issue.createdAt;
  return ts ? new Date(ts).getTime() : 0;
}

const RECENT_TASKS_LIMIT = 6;

/* ---------- recent tasks ---------- */

/** Compact "Recent Tasks" list — the dashboard already fetches the issues
 * feed for the charts; reuse it to surface the most-recently-updated tasks as
 * a quick jump-back-in list. Rows mirror the TasksList row (status circle +
 * identifier + title + assignee + relative updated time) and open the task in
 * the /chat shell when the InlineTeamsNav provider is present; the href is the
 * canonical in-shell URL, so new-tab / no-provider clicks land there too. */
function RecentTaskRow({
  issue,
  assigneeName,
}: {
  issue: Issue;
  assigneeName: string | null;
}) {
  const nav = useInlineTeamsNav();
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const updatedTs = issue.updatedAt ?? issue.createdAt;

  const handleClick = (e: React.MouseEvent) => {
    if (!nav) return;
    if (
      e.defaultPrevented ||
      e.button !== 0 ||
      e.metaKey ||
      e.ctrlKey ||
      e.shiftKey ||
      e.altKey
    ) {
      return;
    }
    e.preventDefault();
    nav.openDetail("issue-detail", issue.id);
  };

  return (
    <Link
      href={teamsDetailUrl("issue", issue.id)}
      prefetch={false}
      onClick={handleClick}
      className="group flex items-center gap-2 border-b border-[var(--edge)] px-4 py-2.5 text-sm no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deep)]"
      style={{ color: "var(--midnight)" }}
    >
      <span className="inline-flex shrink-0 items-center" aria-hidden="true">
        <StatusIcon status={issue.status} />
      </span>
      <span className="shrink-0 font-mono text-xs text-[var(--midnight-muted)]">
        {identifier}
      </span>
      <span className="min-w-0 flex-1 truncate">{issue.title}</span>
      {issue.assigneeAgentId ? (
        <Identity
          name={assigneeName ?? issue.assigneeAgentId.slice(0, 8)}
          size="xs"
          className="shrink-0"
        />
      ) : null}
      {updatedTs ? (
        <time
          dateTime={updatedTs}
          className="shrink-0 text-caption tabular-nums"
          style={{ color: "var(--midnight-muted)" }}
        >
          {timeAgo(updatedTs)}
        </time>
      ) : null}
    </Link>
  );
}

/* ---------- panel ---------- */

export function DashboardPanel() {
  const { read } = useTeamsApi();
  const dashboardQuery = read<DashboardResponse>("/dashboard");
  const issuesQuery = read<unknown>("/issues");
  // Company roster so the activity feed can resolve agent actors + agent
  // entity references to names (same /agents read InboxPanel/IssuesPanel use).
  // Called unconditionally (rules of hooks) above the loading/error returns.
  const agentsQuery = read<{ agents: CompanyAgent[] }>("/agents");
  const { events, isLoading: activityLoading } = useActivityFeed(10);
  // Live-agent-runs feed for the "Agents" panel. Hook is called
  // unconditionally (rules of hooks) — the panel renders below, after the
  // loading/error early-returns. SWR self-refreshes the feed on an interval.
  const { runs: liveRuns } = useLiveRuns();

  if (dashboardQuery.isLoading && !dashboardQuery.data) {
    return (
      <div
        className="editorial-page p-6 sm:p-8 font-editorial-serif text-xl italic"
        style={{ color: "var(--midnight-muted)" }}
      >
        Loading…
      </div>
    );
  }
  if (dashboardQuery.error && !dashboardQuery.data) {
    return (
      <div
        role="alert"
        className="editorial-page p-6 sm:p-8 font-editorial-serif text-xl italic"
        style={{ color: "var(--warn)" }}
      >
        Failed to load dashboard.
      </div>
    );
  }

  const dashboard = dashboardQuery.data?.dashboard ?? EMPTY_DASHBOARD;
  const issues = normalizeIssues(issuesQuery.data);
  const priorityRows = toPriorityRows(issues);
  const statusRows = toStatusRows(issues);
  // Issue lookup for the Agents panel's linked-task chips. Reuses the
  // dashboard's existing `/issues` read so the run cards resolve task
  // titles without fanning out a per-issue fetch (missing entries degrade
  // to an id-slug chip inside ActiveAgentsPanel).
  const issuesById = new Map(issues.map((i) => [i.id, i]));
  // agentId → name map for the activity feed's actor/entity resolution +
  // the Recent Tasks assignee labels.
  const agentNames = new Map(
    (agentsQuery.data?.agents ?? []).map((a) => [a.id, a.name] as const),
  );

  // Recent Tasks — the most-recently-updated issues, newest first. Reuses the
  // already-fetched /issues feed (non-mutating copy before sort).
  const recentTasks = [...issues]
    .sort((a, b) => recencyOf(b) - recencyOf(a))
    .slice(0, RECENT_TASKS_LIMIT);

  const agentsCount = Object.values(dashboard.agents).reduce(
    (s, v) => s + (v ?? 0),
    0,
  );
  const openTasks = dashboard.tasks.open ?? 0;
  const blockedTasks = dashboard.tasks.blocked ?? 0;
  const totalApprovals =
    dashboard.pendingApprovals + dashboard.budgets.pendingApprovals;

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Operations"
        title="Home"
        meta="Last 14 days"
      />

      {/* Live agent runs — sits prominently below the masthead so active
          work is the first thing the dashboard surfaces. */}
      <ActiveAgentsPanel runs={liveRuns} issuesById={issuesById} />

      {/* Metric tiles — single continuous strip with internal hairlines */}
      <section
        aria-label="Key metrics"
        className="editorial-card grid grid-cols-1 @md:grid-cols-2 @4xl:grid-cols-4 overflow-hidden"
      >
        <MetricCard
          icon={Bot}
          value={agentsCount}
          label="Agents"
          to={teamsPanelUrl("agents")}
          onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("agents"))}
          description={
            <span>
              {dashboard.agents.running ?? 0} running,{" "}
              {dashboard.agents.paused ?? 0} paused
            </span>
          }
        />
        <MetricCard
          icon={CircleDot}
          value={openTasks}
          label="Open Tasks"
          to={teamsPanelUrl("issues")}
          onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("issues"))}
          description={
            <span>
              {openTasks} open, {blockedTasks} blocked
            </span>
          }
        />
        {dashboard.costs != null && (
          <MetricCard
            icon={DollarSign}
            value={formatCents(dashboard.costs.monthSpendCents)}
            label="Month Spend"
            to="/chat?panel=usage"
            onClick={(e) => handleShellLinkClick(e, "/chat?panel=usage")}
            description={<span>Billed credits this month</span>}
          />
        )}
        <MetricCard
          icon={ShieldCheck}
          value={totalApprovals}
          label="Pending Approvals"
          to={teamsPanelUrl("inbox")}
          onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("inbox"))}
          description={
            dashboard.budgets.pendingApprovals > 0 ? (
              <span>
                {dashboard.budgets.pendingApprovals} budget overrides awaiting
                board review
              </span>
            ) : (
              <span>Awaiting board review</span>
            )
          }
        />
      </section>

      {/* Charts grid */}
      <section
        aria-label="Activity charts"
        className="grid gap-3 @md:gap-4 @xl:grid-cols-2 @5xl:grid-cols-4"
      >
        <ChartCard title="Run Activity" subtitle="Last 14 days">
          <RunActivityChart activity={dashboard.runActivity} />
        </ChartCard>
        <ChartCard title="Issues by Priority" subtitle="Last 14 days">
          <PriorityChart issues={priorityRows} />
        </ChartCard>
        <ChartCard title="Issues by Status" subtitle="Last 14 days">
          <IssueStatusChart issues={statusRows} />
        </ChartCard>
        <ChartCard title="Success Rate" subtitle="Last 14 days">
          <SuccessRateChart activity={dashboard.runActivity} />
        </ChartCard>
      </section>

      {/* Recent tasks — the most-recently-updated issues, a quick way back
          into in-flight work. */}
      <section aria-label="Recent tasks" className="min-w-0">
        <div className="flex items-baseline justify-between mb-3">
          <h2
            className="font-editorial-serif text-2xl"
            style={{ color: "var(--midnight)" }}
          >
            Recent Tasks
          </h2>
          {recentTasks.length > 0 && (
            <Link
              href={teamsPanelUrl("issues")}
              prefetch={false}
              onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("issues"))}
              className="text-caption transition-colors hover:underline"
              style={{ color: "var(--midnight)" }}
            >
              View all →
            </Link>
          )}
        </div>
        <div
          className="editorial-card overflow-hidden"
          style={{ borderColor: "var(--edge)" }}
        >
          {recentTasks.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              No recent tasks.
            </div>
          ) : (
            <div>
              {recentTasks.map((issue) => (
                <RecentTaskRow
                  key={issue.id}
                  issue={issue}
                  assigneeName={
                    issue.assigneeAgentId
                      ? agentNames.get(issue.assigneeAgentId) ?? null
                      : null
                  }
                />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Recent activity */}
      <section aria-label="Recent activity" className="min-w-0">
        <div className="flex items-baseline justify-between mb-3">
          <h2
            className="font-editorial-serif text-2xl"
            style={{ color: "var(--midnight)" }}
          >
            Recent activity
          </h2>
          {events.length > 0 && (
            <Link
              href={teamsPanelUrl("activity")}
              prefetch={false}
              onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("activity"))}
              className="text-caption transition-colors hover:underline"
              style={{ color: "var(--midnight)" }}
            >
              View all →
            </Link>
          )}
        </div>
        <div
          className="editorial-card overflow-hidden"
          style={{ borderColor: "var(--edge)" }}
        >
          {activityLoading && events.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              Loading activity…
            </div>
          ) : events.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              No recent activity.
            </div>
          ) : (
            <div>
              {events.map((evt, i) => (
                <div
                  key={evt.id}
                  style={i === 0 ? undefined : { borderTop: "1px solid var(--edge)" }}
                >
                  <ActivityRow event={evt} agentNames={agentNames} />
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
