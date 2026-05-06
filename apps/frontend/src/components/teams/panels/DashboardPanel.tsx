"use client";

// apps/frontend/src/components/teams/panels/DashboardPanel.tsx
//
// Ported from upstream Paperclip's Dashboard
// (paperclip/ui/src/pages/Dashboard.tsx) (MIT, (c) 2025 Paperclip AI).
// 4 metric tiles + 4 charts + recent activity feed. Drops upstream's
// ActiveAgentsPanel, plugin slots, onboarding empty-state (handled
// elsewhere in our TeamsLayout), and configurable date ranges (14d hardcoded).
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

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
      <div className="p-6 sm:p-8 text-sm text-muted-foreground">Loading…</div>
    );
  }
  if (dashboardQuery.error && !dashboardQuery.data) {
    return (
      <div role="alert" className="p-6 sm:p-8 text-sm text-destructive">
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

  const today = new Date().toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  return (
    <div className="p-4 sm:p-6 lg:p-8 flex flex-col gap-7 max-w-[1600px] mx-auto w-full">
      {/* Header — editorial-grid masthead */}
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2 pb-1 border-b border-border/60">
        <div className="space-y-1">
          <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-[0.12em]">
            Overview
          </p>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight leading-none">
            Dashboard
          </h1>
        </div>
        <p className="text-xs text-muted-foreground tabular-nums font-mono pb-1">
          {today}
        </p>
      </header>

      {/* Metric tiles — single continuous strip with internal hairlines */}
      <section
        aria-label="Key metrics"
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 border border-border/70 rounded-lg bg-card overflow-hidden divide-y divide-border/70 sm:divide-y-0 lg:divide-x lg:divide-y-0"
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
          <h2 className="text-[11px] font-semibold text-foreground uppercase tracking-[0.06em]">
            Recent activity
          </h2>
          {events.length > 0 && (
            <Link
              href="/teams/activity"
              className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
            >
              View all →
            </Link>
          )}
        </div>
        <div className="border border-border/70 rounded-lg divide-y divide-border/70 bg-card overflow-hidden">
          {activityLoading && events.length === 0 ? (
            <div className="px-4 py-3 text-sm text-muted-foreground/70 italic">
              Loading activity…
            </div>
          ) : events.length === 0 ? (
            <div className="px-4 py-3 text-sm text-muted-foreground/70 italic">
              No recent activity.
            </div>
          ) : (
            events.map((evt) => <ActivityRow key={evt.id} event={evt} />)
          )}
        </div>
      </section>
    </div>
  );
}
