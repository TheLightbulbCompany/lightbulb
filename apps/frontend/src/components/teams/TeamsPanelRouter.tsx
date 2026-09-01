"use client";

import dynamic from "next/dynamic";
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
  artifacts: () => import("./panels/ArtifactsPanel").then((m) => m.ArtifactsPanel),
  brain: () => import("./panels/BrainPanel").then((m) => m.BrainPanel),
  accounts: () => import("./panels/AccountsPanel").then((m) => m.AccountsPanel),
  search: () => import("./panels/SearchPanel").then((m) => m.SearchPanel),
  settings: () => import("./panels/SettingsPanel").then((m) => m.SettingsPanel),
  spend: () => import("./panels/SpendPanel").then((m) => m.SpendPanel),
  payments: () => import("./panels/PaymentsPanel").then((m) => m.PaymentsPanel),
  products: () => import("./panels/ProductsPanel").then((m) => m.ProductsPanel),
  customers: () => import("./panels/CustomersPanel").then((m) => m.CustomersPanel),
  publish: () => import("./panels/PublishPanel").then((m) => m.PublishPanel),
  seo: () => import("./panels/SeoPanel").then((m) => m.SeoPanel),
  ads: () => import("./panels/AdsPanel").then((m) => m.AdsPanel),
  support: () => import("./panels/SupportPanel").then((m) => m.SupportPanel),
  identity: () => import("./panels/IdentityPanel").then((m) => m.IdentityPanel),
  "domain-site": () => import("./panels/DomainSitePanel").then((m) => m.DomainSitePanel),
  compliance: () => import("./panels/CompliancePanel").then((m) => m.CompliancePanel),
  contracts: () => import("./panels/ContractsPanel").then((m) => m.ContractsPanel),
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
  run: () => import("./panels/RunDetailPanel").then((m) => m.RunDetailPanel),
};
const IssueDetailPanel = dynamic(DETAIL_LOADERS.issue, { loading: PanelSkeleton });
const RoutineDetailPanel = dynamic(DETAIL_LOADERS.routine, { loading: PanelSkeleton });
const ProjectDetailPanel = dynamic(DETAIL_LOADERS.project, { loading: PanelSkeleton });
const GoalDetailPanel = dynamic(DETAIL_LOADERS.goal, { loading: PanelSkeleton });
const RunDetailPanel = dynamic(DETAIL_LOADERS.run, { loading: PanelSkeleton });

// Issues + Inbox take params/callbacks (unlike the propless list panels in
// PANELS), so they get their own prop-typed dynamic wrappers. They point at the
// same chunks preloadTeamsPanels() already warms via PANEL_LOADERS.
const IssuesPanel = dynamic(
  () => import("./panels/IssuesPanel").then((m) => m.IssuesPanel),
  { loading: PanelSkeleton },
);
const InboxPanel = dynamic(
  () => import("./panels/InboxPanel").then((m) => m.InboxPanel),
  { loading: PanelSkeleton },
);
const BrainPanel = dynamic(
  () => import("./panels/BrainPanel").then((m) => m.BrainPanel),
  { loading: PanelSkeleton },
);

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

function normalizeNewIssue(
  value: string | undefined,
): "1" | "onboarding" | null {
  return value === "1" || value === "onboarding" ? value : null;
}

export interface TeamsPanelRouterProps {
  panel: string;
  detailId?: string | null;
  // Params the shell feeds each panel, read from its own channel (URL on
  // mobile, dockview params on desktop): { agent?, newIssue?, new? }. Retired
  // panel keys are resolved to their live surface by the shell/registry before
  // reaching here — this router no longer rewrites the URL for aliases.
  panelParams?: Record<string, string>;
  onConsumeOneShot?: (key: "new" | "newIssue") => void;
  onUpdateDurableParam?: (key: "agent", value: string | null) => void;
}

export function TeamsPanelRouter({
  panel,
  detailId,
  panelParams,
  onConsumeOneShot,
  onUpdateDurableParam,
}: TeamsPanelRouterProps) {
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
    if (panel === "run-detail")
      return <RunDetailPanel runId={detailId} />;
  }

  // Param-taking list panels — threaded from panelParams; the consume callbacks
  // let each shell strip its own channel.
  if (panel === "issues") {
    return (
      <IssuesPanel
        agentFilter={panelParams?.agent ?? null}
        newTask={panelParams?.new === "task"}
        onNewTaskConsumed={() => onConsumeOneShot?.("new")}
        onAgentFilterCleared={() => onUpdateDurableParam?.("agent", null)}
      />
    );
  }
  if (panel === "inbox") {
    return (
      <InboxPanel
        newIssue={normalizeNewIssue(panelParams?.newIssue)}
        onNewIssueConsumed={() => onConsumeOneShot?.("newIssue")}
        initialTab={panelParams?.tab ?? null}
      />
    );
  }

  if (panel === "brain") {
    return (
      <BrainPanel
        initialPath={panelParams?.path ?? null}
        initialTab={panelParams?.tab === "dreams" ? "dreams" : null}
      />
    );
  }

  const Cmp = PANELS[panel];
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
