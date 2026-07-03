"use client";

import { useEffect } from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";

// Shown while a panel's code-split chunk loads. Without a loading fallback,
// next/dynamic renders nothing and a first visit flashes a blank pane before
// the panel's own loading state can appear.
function PanelSkeleton() {
  return (
    <div className="p-8" aria-busy="true">
      <PageSkeleton />
    </div>
  );
}

// Loaders are declared apart from their dynamic() wrappers so
// preloadTeamsPanels() can warm the exact same chunks without duplicating
// import paths.
const PANEL_LOADERS: Record<string, () => Promise<React.ComponentType>> = {
  dashboard: () => import("./panels/DashboardPanel").then((m) => m.DashboardPanel),
  agents: () => import("./panels/AgentsListPanel").then((m) => m.AgentsListPanel),
  "org-chart": () => import("./panels/OrgChartPanel").then((m) => m.OrgChartPanel),
  inbox: () => import("./panels/InboxPanel").then((m) => m.InboxPanel),
  issues: () => import("./panels/IssuesPanel").then((m) => m.IssuesPanel),
  routines: () => import("./panels/RoutinesPanel").then((m) => m.RoutinesPanel),
  goals: () => import("./panels/GoalsPanel").then((m) => m.GoalsPanel),
  projects: () => import("./panels/ProjectsListPanel").then((m) => m.ProjectsListPanel),
  activity: () => import("./panels/ActivityPanel").then((m) => m.ActivityPanel),
  costs: () => import("./panels/CostsPanel").then((m) => m.CostsPanel),
  skills: () => import("./panels/SkillsPanel").then((m) => m.SkillsPanel),
  settings: () => import("./panels/SettingsPanel").then((m) => m.SettingsPanel),
};

const PANELS: Record<string, React.ComponentType> = Object.fromEntries(
  Object.entries(PANEL_LOADERS).map(([key, loader]) => [
    key,
    dynamic(loader, { loading: PanelSkeleton }),
  ]),
);

// Detail panels take their id as a prop (not propless like the list panels in
// PANELS), so they get their own dynamic imports + a dedicated branch that
// passes the id through from `detailId`.
const DETAIL_LOADERS = {
  issue: () => import("./panels/IssueDetailPanel").then((m) => m.IssueDetailPanel),
  routine: () => import("./panels/RoutineDetailPanel").then((m) => m.RoutineDetailPanel),
  project: () => import("./panels/ProjectDetailPanel").then((m) => m.ProjectDetailPanel),
  goal: () => import("./panels/GoalDetailPanel").then((m) => m.GoalDetailPanel),
};
const IssueDetailPanel = dynamic(DETAIL_LOADERS.issue, { loading: PanelSkeleton });
const RoutineDetailPanel = dynamic(DETAIL_LOADERS.routine, { loading: PanelSkeleton });
const ProjectDetailPanel = dynamic(DETAIL_LOADERS.project, { loading: PanelSkeleton });
const GoalDetailPanel = dynamic(DETAIL_LOADERS.goal, { loading: PanelSkeleton });

// Warm every panel chunk (called from the shell during browser idle time) so
// the first open of a panel mounts from the module cache instead of fetching
// its chunk on click. Failures are ignored — the dynamic() wrappers refetch
// on demand.
export function preloadTeamsPanels(): void {
  for (const load of [
    ...Object.values(PANEL_LOADERS),
    ...Object.values(DETAIL_LOADERS),
  ]) {
    void load().catch(() => {});
  }
}

// Panels removed from the nav but still reachable by old URLs/bookmarks.
const RETIRED_REDIRECTS: Record<string, string> = {
  approvals: "/teams/inbox", // folded into the inbox "Needs attention" band
  members: "/chat?panel=teams-settings", // members management lives in Settings now
};

export function TeamsPanelRouter({
  panel,
  detailId,
}: {
  panel: string;
  detailId?: string | null;
}) {
  const router = useRouter();
  const redirectTo = RETIRED_REDIRECTS[panel];

  useEffect(() => {
    if (redirectTo) router.replace(redirectTo);
  }, [redirectTo, router]);

  if (redirectTo) return null;

  // Detail branch — must precede the list map. A detail key without a
  // detailId falls through to "Unknown panel" below (no id to render).
  if (detailId) {
    if (panel === "issue-detail")
      return <IssueDetailPanel issueId={detailId} />;
    if (panel === "routine-detail")
      return <RoutineDetailPanel routineId={detailId} />;
    if (panel === "project-detail")
      return <ProjectDetailPanel projectId={detailId} />;
    if (panel === "goal-detail")
      return <GoalDetailPanel goalId={detailId} />;
  }

  const Cmp = PANELS[panel];
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
