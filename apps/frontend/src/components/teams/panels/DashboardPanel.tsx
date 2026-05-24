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
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { PanelMasthead } from "@/components/teams/shell";
import type {
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
  costs: {
    monthSpendCents: 0,
    monthBudgetCents: 0,
    monthUtilizationPercent: 0,
  },
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

/* ---------- panel ---------- */

export function DashboardPanel() {
  const { read } = useTeamsApi();
  const dashboardQuery = read<DashboardResponse>("/dashboard");
  const issuesQuery = read<unknown>("/issues");
  const { events, isLoading: activityLoading } = useActivityFeed(10);

  if (dashboardQuery.isLoading && !dashboardQuery.data) {
    return (
      <div
        className="editorial-page p-6 sm:p-8 font-editorial-serif text-xl italic"
        style={{ color: "var(--mute)" }}
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

  const agentsCount = Object.values(dashboard.agents).reduce(
    (s, v) => s + (v ?? 0),
    0,
  );
  const openTasks = dashboard.tasks.open ?? 0;
  const blockedTasks = dashboard.tasks.blocked ?? 0;
  const totalApprovals =
    dashboard.pendingApprovals + dashboard.budgets.pendingApprovals;

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Operations"
        title="Dashboard"
        meta="Last 14 days"
      />

      {/* Metric tiles — single continuous strip with internal hairlines */}
      <section
        aria-label="Key metrics"
        className="editorial-card grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 overflow-hidden"
      >
        <MetricCard
          icon={Bot}
          value={agentsCount}
          label="Agents"
          to="/teams/agents"
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
          to="/teams/issues"
          description={
            <span>
              {openTasks} open, {blockedTasks} blocked
            </span>
          }
        />
        <MetricCard
          icon={DollarSign}
          value={formatCents(dashboard.costs.monthSpendCents)}
          label="Month Spend"
          to="/teams/costs"
          description={
            dashboard.costs.monthBudgetCents > 0 ? (
              <span>
                {dashboard.costs.monthUtilizationPercent}% of{" "}
                {formatCents(dashboard.costs.monthBudgetCents)} budget
              </span>
            ) : (
              <span>Unlimited budget</span>
            )
          }
        />
        <MetricCard
          icon={ShieldCheck}
          value={totalApprovals}
          label="Pending Approvals"
          to="/teams/approvals"
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
        className="grid gap-3 sm:gap-4 sm:grid-cols-2 xl:grid-cols-4"
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

      {/* Recent activity */}
      <section aria-label="Recent activity" className="min-w-0">
        <div className="flex items-baseline justify-between mb-3">
          <h2
            className="font-editorial-serif text-2xl"
            style={{ color: "var(--ink)" }}
          >
            Recent activity
          </h2>
          {events.length > 0 && (
            <Link
              href="/teams/activity"
              className="text-[11px] transition-colors hover:underline"
              style={{ color: "var(--forest)" }}
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
              style={{ color: "var(--mute)" }}
            >
              Loading activity…
            </div>
          ) : events.length === 0 ? (
            <div
              className="px-4 py-3 font-editorial-serif text-base italic"
              style={{ color: "var(--mute)" }}
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
                  <ActivityRow event={evt} />
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
