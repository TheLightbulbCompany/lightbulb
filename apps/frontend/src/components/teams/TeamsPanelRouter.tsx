"use client";

import dynamic from "next/dynamic";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import {
  AGENTS_VIEWS,
  INBOX_SECTIONS,
  MONEY_SECTIONS,
  TASKS_GROUP_BY,
  type InboxSectionId,
} from "@/components/workspace/panelRegistry";
import type { AgentsViewMode } from "./agents/agentsViewMode";
import type { MoneySectionId } from "./panels/MoneyPanel";
import type { TasksGroupBy } from "./issues/TasksList";

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
  agents: () => import("./panels/AgentsListPanel").then((m) => m.AgentsListPanel),
  inbox: () => import("./panels/InboxPanel").then((m) => m.InboxPanel),
  issues: () => import("./panels/IssuesPanel").then((m) => m.IssuesPanel),
  // With the super-agent on, Automations (lane A's rows) with a link to today's routines; off, RoutinesPanel as before.
  routines: () => import("@/components/tasks/AutomationsPanel").then((m) => m.AutomationsPanel),
  goals: () => import("./panels/GoalsPanel").then((m) => m.GoalsPanel),
  projects: () => import("./panels/ProjectsListPanel").then((m) => m.ProjectsListPanel),
  cases: () => import("./panels/CasesPanel").then((m) => m.CasesPanel),
  artifacts: () => import("./panels/ArtifactsPanel").then((m) => m.ArtifactsPanel),
  search: () => import("./panels/SearchPanel").then((m) => m.SearchPanel),
  money: () => import("./panels/MoneyPanel").then((m) => m.MoneyPanel),
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
  case: () => import("./panels/CaseDetailPanel").then((m) => m.CaseDetailPanel),
  run: () => import("./panels/RunDetailPanel").then((m) => m.RunDetailPanel),
  approval: () => import("./approvals/ApprovalDetail").then((m) => m.ApprovalDetail),
};
const IssueDetailPanel = dynamic(DETAIL_LOADERS.issue, { loading: PanelSkeleton });
const RoutineDetailPanel = dynamic(DETAIL_LOADERS.routine, { loading: PanelSkeleton });
const ProjectDetailPanel = dynamic(DETAIL_LOADERS.project, { loading: PanelSkeleton });
const GoalDetailPanel = dynamic(DETAIL_LOADERS.goal, { loading: PanelSkeleton });
const CaseDetailPanel = dynamic(DETAIL_LOADERS.case, { loading: PanelSkeleton });
const RunDetailPanel = dynamic(DETAIL_LOADERS.run, { loading: PanelSkeleton });
const ApprovalDetail = dynamic(DETAIL_LOADERS.approval, { loading: PanelSkeleton });

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
// Agents absorbed the org chart as its second view — `teams-org-chart` aliases
// to `teams-agents` in the registry (one panel id, one dock tab) and arrives
// here as `view`.
const AgentsListPanel = dynamic(
  () => import("./panels/AgentsListPanel").then((m) => m.AgentsListPanel),
  { loading: PanelSkeleton },
);

// Money absorbed the four old Payments/Products/Customers/Spend tabs as
// sections of one ledger page. Those keys alias to `money` in the registry
// (one panel id, one dock tab) and arrive here as `section` — an owner's saved
// link still lands on the part of the page they bookmarked.
const MoneyPanel = dynamic(() => import("./panels/MoneyPanel").then((m) => m.MoneyPanel), { loading: PanelSkeleton });

// The fence for a hand-typed ?section=: MoneyPanel feeds this into a
// `#money-${section}` selector, and an arbitrary string there throws.
function asMoneySection(value: string | undefined): MoneySectionId | null {
  return value && MONEY_SECTIONS.has(value) ? (value as MoneySectionId) : null;
}

// Same fence for Inbox: InboxPage feeds this into a `#inbox-${section}`
// selector, so an arbitrary string there must never reach it.
function asInboxSection(value: string | undefined): InboxSectionId | undefined {
  return value && INBOX_SECTIONS.has(value) ? (value as InboxSectionId) : undefined;
}

// And for Agents: an unknown ?view falls back to the panel's own stored
// preference rather than reaching it as a bogus view.
function asAgentsView(value: string | undefined): AgentsViewMode | null {
  return value && AGENTS_VIEWS.has(value) ? (value as AgentsViewMode) : null;
}

// And for Tasks: an unknown ?groupBy falls back to the page's own default
// (no grouping) rather than reaching TasksList as a bogus dimension.
function asTasksGroupBy(value: string | undefined): TasksGroupBy | undefined {
  return value && TASKS_GROUP_BY.has(value) ? (value as TasksGroupBy) : undefined;
}

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

export interface TeamsPanelRouterProps {
  panel: string;
  detailId?: string | null;
  // Params the shell feeds each panel, read from its own channel (URL on
  // mobile, the workspace slot params on desktop): { agent?, newIssue?, new? }. Retired
  // panel keys are resolved to their live surface by the shell/registry before
  // reaching here — this router no longer rewrites the URL for aliases.
  panelParams?: Record<string, string>;
  onConsumeOneShot?: (key: "new" | "newIssue") => void;
  onUpdateDurableParam?: (key: "agent" | "path" | "day", value: string | null) => void;
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
    if (panel === "case-detail")
      return <CaseDetailPanel caseId={detailId} />;
    if (panel === "run-detail")
      return <RunDetailPanel runId={detailId} />;
    if (panel === "approval-detail")
      return <ApprovalDetail approvalId={detailId} />;
  }

  // Param-taking list panels — threaded from panelParams; the consume callbacks
  // let each shell strip its own channel.
  if (panel === "issues") {
    return (
      <IssuesPanel
        agentFilter={panelParams?.agent ?? null}
        initialGroupBy={asTasksGroupBy(panelParams?.groupBy)}
        // Two channels, one dialog: `?new=task` from the sidebar pill, and
        // `?newIssue=` from Get Started, which Inbox used to own.
        newTask={panelParams?.new === "task" || Boolean(panelParams?.newIssue)}
        newTaskPrefill={panelParams?.newIssue === "onboarding" ? "onboarding" : null}
        onNewTaskConsumed={() => {
          onConsumeOneShot?.("new");
          onConsumeOneShot?.("newIssue");
        }}
        onAgentFilterCleared={() => onUpdateDurableParam?.("agent", null)}
      />
    );
  }
  if (panel === "agents") {
    return <AgentsListPanel initialView={asAgentsView(panelParams?.view)} />;
  }
  if (panel === "inbox") {
    return <InboxPanel section={asInboxSection(panelParams?.section)} />;
  }

  if (panel === "money") {
    return (
      <MoneyPanel
        section={asMoneySection(panelParams?.section)}
        day={panelParams?.day ?? null}
        onDayCleared={() => onUpdateDurableParam?.("day", null)}
      />
    );
  }

  // Own-property only (Object.hasOwn) — `in` and bracket access both walk the
  // prototype chain, so a `panel` of "constructor"/"valueOf" would resolve to
  // an inherited Object.prototype value instead of rejecting to "Unknown
  // panel". Same hazard panelRegistry.ts guards against for its own key-maps.
  const Cmp = Object.hasOwn(PANELS, panel) ? PANELS[panel] : undefined;
  if (!Cmp) return <div className="p-8">Unknown panel: {panel}</div>;
  return <Cmp />;
}
