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

// Panels removed from the nav but still reachable by old URLs/bookmarks.
const RETIRED_REDIRECTS: Record<string, string> = {
  approvals: "/teams/inbox", // folded into the inbox "Needs attention" band
};

export function TeamsPanelRouter({ panel }: { panel: string }) {
  const router = useRouter();
  const redirectTo = RETIRED_REDIRECTS[panel];

  useEffect(() => {
    if (redirectTo) router.replace(redirectTo);
  }, [redirectTo, router]);

  if (redirectTo) return null;

  const Cmp = PANELS[panel];
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
