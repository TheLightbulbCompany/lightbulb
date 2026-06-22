"use client";

import { useEffect } from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";

const PANELS: Record<string, React.ComponentType> = {
  dashboard: dynamic(() =>
    import("./panels/DashboardPanel").then((m) => m.DashboardPanel),
  ),
  agents: dynamic(() =>
    import("./panels/AgentsListPanel").then((m) => m.AgentsListPanel),
  ),
  "org-chart": dynamic(() =>
    import("./panels/OrgChartPanel").then((m) => m.OrgChartPanel),
  ),
  inbox: dynamic(() =>
    import("./panels/InboxPanel").then((m) => m.InboxPanel),
  ),
  issues: dynamic(() =>
    import("./panels/IssuesPanel").then((m) => m.IssuesPanel),
  ),
  routines: dynamic(() =>
    import("./panels/RoutinesPanel").then((m) => m.RoutinesPanel),
  ),
  goals: dynamic(() =>
    import("./panels/GoalsPanel").then((m) => m.GoalsPanel),
  ),
  projects: dynamic(() =>
    import("./panels/ProjectsListPanel").then((m) => m.ProjectsListPanel),
  ),
  activity: dynamic(() =>
    import("./panels/ActivityPanel").then((m) => m.ActivityPanel),
  ),
  costs: dynamic(() =>
    import("./panels/CostsPanel").then((m) => m.CostsPanel),
  ),
  skills: dynamic(() =>
    import("./panels/SkillsPanel").then((m) => m.SkillsPanel),
  ),
  members: dynamic(() =>
    import("./panels/MembersPanel").then((m) => m.MembersPanel),
  ),
  settings: dynamic(() =>
    import("./panels/SettingsPanel").then((m) => m.SettingsPanel),
  ),
};

// Detail panels take their id as a prop (not propless like the list panels in
// PANELS), so they get their own dynamic imports + a dedicated branch that
// passes the id through from `detailId`.
const IssueDetailPanel = dynamic(() =>
  import("./panels/IssueDetailPanel").then((m) => m.IssueDetailPanel),
);
const RoutineDetailPanel = dynamic(() =>
  import("./panels/RoutineDetailPanel").then((m) => m.RoutineDetailPanel),
);
const ProjectDetailPanel = dynamic(() =>
  import("./panels/ProjectDetailPanel").then((m) => m.ProjectDetailPanel),
);

// Panels removed from the nav but still reachable by old URLs/bookmarks.
const RETIRED_REDIRECTS: Record<string, string> = {
  approvals: "/teams/inbox", // folded into the inbox "Needs attention" band
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
  }

  const Cmp = PANELS[panel];
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
