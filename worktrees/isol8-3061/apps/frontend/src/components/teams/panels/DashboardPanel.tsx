"use client";

// apps/frontend/src/components/teams/panels/DashboardPanel.tsx
//
// Home is the dashboard: the company's name, the business hero and its
// anchored figures (HomeRollupStrip), the verbs (HomeQuickActions), then the
// top of each detail page — what needs the owner, what the team is doing now,
// what it made — each with one link into Inbox, Tasks or Library.
//
// The Paperclip dashboard port that used to sit below (run charts, metric
// cards, recent tasks, recent activity) is gone from Home: those answer "are
// my agents busy", and Inbox / Tasks own that question now. `/dashboard` is
// still read for one thing, the Metronome-billed spend the strip prints, and
// its ECS-probing slowness is why the strip takes `undefined` while it loads.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { PanelMasthead } from "@/components/teams/shell";
import { HomeRollupStrip } from "@/components/teams/dashboard/HomeRollupStrip";
import { HomeQuickActions } from "@/components/teams/dashboard/HomeQuickActions";
import { HomeNeedsYou } from "@/components/teams/dashboard/HomeNeedsYou";
import { HomeHappeningNow } from "@/components/teams/dashboard/HomeHappeningNow";
import { HomeRecentWork } from "@/components/teams/dashboard/HomeRecentWork";
import { useCompanyRollup } from "@/hooks/useCompanyRollup";
import type { DashboardResponse, Issue } from "@/components/teams/shared/types";

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

export function DashboardPanel() {
  const { read } = useTeamsApi();
  const dashboardQuery = read<DashboardResponse>("/dashboard");
  // Task titles for the Happening-now rows (a run carries only an issue id).
  const issuesQuery = read<unknown>("/issues");
  // Paperclip company record; `name` is the editable company name. Falls back
  // to "Home" while loading — PanelMasthead hides the overline when it equals
  // the title, so the fallback reads as a plain "Home" masthead.
  const settingsQuery = read<{ name?: string }>("/settings");
  const companyName = settingsQuery.data?.name?.trim() || "Home";
  // Same SWR key the strip reads at its default span — one request, not two.
  const { rollup } = useCompanyRollup();

  const dashboardLoading = dashboardQuery.isLoading && !dashboardQuery.data;
  const dashboard = dashboardQuery.data?.dashboard;
  const issuesById = new Map(normalizeIssues(issuesQuery.data).map((i) => [i.id, i]));

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-5 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Home" title={companyName} />

      {/* Three states, not two: `undefined` while /dashboard is in flight keeps
          the strip from rendering an all-zero verdict; `null` is the settled
          "the backend omitted `costs`". On a `/dashboard` 5xx `costs` is also
          absent, so this tile reads "Not shown" — attributing the gap to the
          owner's plan rather than the failed read. Accepted: no number is
          printed, and a distinct settled-failure tile state is out of scope. */}
      <HomeRollupStrip
        spendCents={dashboardLoading ? undefined : (dashboard?.costs?.monthSpendCents ?? null)}
      />
      <HomeQuickActions stripeBound={!!rollup?.stripe_bound} />

      {/* Container query, not viewport: this renders inside a dock pane. */}
      <div className="grid grid-cols-1 gap-6 @3xl:grid-cols-2">
        <HomeNeedsYou />
        <div className="flex flex-col gap-6">
          <HomeHappeningNow
            issuesById={issuesById}
            openTasks={dashboard ? (dashboard.tasks.open ?? 0) : undefined}
          />
          <HomeRecentWork />
        </div>
      </div>
    </div>
  );
}
