"use client";

// apps/frontend/src/components/teams/panels/DashboardPanel.tsx
//
// The agent telemetry below is ported from upstream Paperclip's Dashboard
// (paperclip/ui/src/pages/Dashboard.tsx) (MIT, (c) 2025 Paperclip AI).
// Drops upstream's plugin slots, onboarding empty-state (handled elsewhere in
// our TeamsLayout), and configurable date ranges (14d hardcoded).
//
// Composition: the company's own name is the H1, the business strip and the
// quick actions come first, and the ported agent telemetry sits second inside
// a collapsible "Your team". The /dashboard loading + error gate lives INSIDE
// that section on purpose — it does an ECS probe, and a slow or provisioning
// agent runtime must never hold up the revenue hero.

import Link from "next/link";
import { useState } from "react";
import { Bot, CircleDot, ShieldCheck } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { MetricCard } from "@/components/teams/dashboard/MetricCard";
import {
  ChartCard,
  RunActivityChart,
  SuccessRateChart,
} from "@/components/teams/dashboard/ActivityCharts";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { ActiveAgentsPanel } from "@/components/teams/dashboard/ActiveAgentsPanel";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { PanelMasthead, PanelSection } from "@/components/teams/shell";
import { HomeRollupStrip } from "@/components/teams/dashboard/HomeRollupStrip";
import { HomeQuickActions } from "@/components/teams/dashboard/HomeQuickActions";
import { useCompanyRollup } from "@/hooks/useCompanyRollup";
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
  const agentsQuery = read<{ agents: CompanyAgent[] }>("/agents");
  // Paperclip company record; `name` is the user-visible, editable company
  // name (routers/teams/settings.py). Falls back to "Home" while it loads —
  // PanelMasthead hides the overline when it equals the title, so the fallback
  // reads as a plain "Home" masthead rather than "Home Home".
  const settingsQuery = read<{ name?: string }>("/settings");
  const companyName = settingsQuery.data?.name?.trim() || "Home";
  const { events, isLoading: activityLoading } = useActivityFeed(10);
  // Live-agent-runs feed for the "Agents" panel. SWR self-refreshes it on an
  // interval.
  const { runs: liveRuns } = useLiveRuns();
  // Same SWR key the strip reads at its default span, so this is one request
  // and not two. Only the binding flag is consulted here — it gates the
  // Add-a-product chip.
  const { rollup } = useCompanyRollup();
  const [teamOpen, setTeamOpen] = useState(true);

  // Not early returns: /dashboard does an ECS probe, so this gate belongs
  // inside "Your team" where it costs the owner nothing above it.
  const dashboardLoading = dashboardQuery.isLoading && !dashboardQuery.data;
  const dashboardFailed = !!dashboardQuery.error && !dashboardQuery.data;

  const dashboard = dashboardQuery.data?.dashboard ?? EMPTY_DASHBOARD;
  const issues = normalizeIssues(issuesQuery.data);
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
      <PanelMasthead overline="Home" title={companyName} />
      <p className="-mt-2 text-sm text-[var(--midnight-muted)]">
        How the business is doing. Open any tile for the full picture.
      </p>

      {/* Business first: "is my business working" is the question an owner
          opens Home with — the agent telemetry below answers "are my agents
          busy", a different and less urgent one. Rendered independently of
          Paperclip's /dashboard (which does an ECS probe), so a slow or
          provisioning agent runtime never holds the revenue hero. */}
      <HomeRollupStrip spendCents={dashboard.costs?.monthSpendCents ?? null} />
      <HomeQuickActions stripeBound={!!rollup?.stripe_bound} />

      {/* "Are my agents busy" — real, and second. */}
      <PanelSection
        label="Your team"
        count={agentsCount || undefined}
        open={teamOpen}
        onOpenChange={setTeamOpen}
      >
        <div className="flex flex-col gap-4 pt-4">
          {dashboardLoading ? (
            <div
              className="font-editorial-serif text-xl italic"
              style={{ color: "var(--midnight-muted)" }}
            >
              Loading…
            </div>
          ) : dashboardFailed ? (
            <div
              role="alert"
              className="font-editorial-serif text-xl italic"
              style={{ color: "var(--warn)" }}
            >
              Failed to load dashboard.
            </div>
          ) : (
            <>
              {/* Live agent runs — the first thing this section surfaces. */}
              <ActiveAgentsPanel runs={liveRuns} issuesById={issuesById} />

              {/* Metric tiles — single continuous strip with internal hairlines */}
              <section
                aria-label="Key metrics"
                className="editorial-card grid grid-cols-1 @md:grid-cols-3 overflow-hidden"
              >
                <MetricCard
                  icon={Bot}
                  value={agentsCount}
                  label="Agents"
                  to={teamsPanelUrl("agents")}
                  onClick={(e) =>
                    handleShellLinkClick(e, teamsPanelUrl("agents"))
                  }
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
                  onClick={(e) =>
                    handleShellLinkClick(e, teamsPanelUrl("issues"))
                  }
                  description={
                    <span>
                      {openTasks} open, {blockedTasks} blocked
                    </span>
                  }
                />
                <MetricCard
                  icon={ShieldCheck}
                  value={totalApprovals}
                  label="Pending Approvals"
                  to={teamsPanelUrl("inbox")}
                  onClick={(e) =>
                    handleShellLinkClick(e, teamsPanelUrl("inbox"))
                  }
                  description={
                    dashboard.budgets.pendingApprovals > 0 ? (
                      <span>
                        {dashboard.budgets.pendingApprovals} budget overrides
                        awaiting board review
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
                className="grid gap-3 @md:gap-4 @xl:grid-cols-2"
              >
                <ChartCard title="Run Activity" subtitle="Last 14 days">
                  <RunActivityChart activity={dashboard.runActivity} />
                </ChartCard>
                <ChartCard title="Success Rate" subtitle="Last 14 days">
                  <SuccessRateChart activity={dashboard.runActivity} />
                </ChartCard>
              </section>
            </>
          )}

          {/* Outside the gate above: these two read /issues and /activity, so
              a /dashboard failure is no reason to withhold them. */}

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
                  onClick={(e) =>
                    handleShellLinkClick(e, teamsPanelUrl("issues"))
                  }
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
                          ? (agentNames.get(issue.assigneeAgentId) ?? null)
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
                  onClick={(e) =>
                    handleShellLinkClick(e, teamsPanelUrl("activity"))
                  }
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
                      style={
                        i === 0
                          ? undefined
                          : { borderTop: "1px solid var(--edge)" }
                      }
                    >
                      <ActivityRow event={evt} agentNames={agentNames} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </div>
      </PanelSection>
    </div>
  );
}
