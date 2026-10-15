"use client";

// The Work place's tab strip. Every task-board page opens in the one Work main
// view (panelRegistry WORK_PANEL_ID); a tab here re-targets that same view.
import { PanelTabs } from "@/components/teams/shell";
import { WORK_TEAMS_KEYS } from "../panelRegistry";
import { openPanel } from "../workspaceController";

export { WORK_TEAMS_KEYS };

const LABELS: Record<(typeof WORK_TEAMS_KEYS)[number], string> = {
  issues: "Tasks",
  projects: "Projects",
  goals: "Goals",
  routines: "Automations",
  cases: "Cases",
  agents: "Agents",
  money: "Money",
};

export function WorkTabs({ teamsKey }: { teamsKey: string }): React.ReactElement {
  return (
    <PanelTabs
      ariaLabel="Work"
      className="px-6 pt-3"
      tabs={WORK_TEAMS_KEYS.map((key) => ({ value: key, label: LABELS[key] }))}
      value={teamsKey}
      onValueChange={(key) => openPanel(`teams-${key}`, {}, { pushUrl: true })}
    />
  );
}
