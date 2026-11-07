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
  money: "Money",
};

/** Threads live on Home; the Paperclip-era pages they replace (its Tasks board, Projects,
 *  Goals, Cases) stay out of the strip. Their URLs still open them, with their tab shown. */
const SUPER_AGENT_HIDDEN: ReadonlySet<string> = new Set(["issues", "projects", "goals", "cases"]);

export function WorkTabs({ teamsKey }: { teamsKey: string }): React.ReactElement {
  const keys = WORK_TEAMS_KEYS.filter((k) => !SUPER_AGENT_HIDDEN.has(k) || k === teamsKey);
  return (
    <PanelTabs
      ariaLabel="Work"
      className="px-6 pt-3"
      tabs={keys.map((key) => ({ value: key, label: key === "issues" ? "Older tasks" : LABELS[key] }))}
      value={teamsKey}
      onValueChange={(key) => openPanel(`teams-${key}`, {}, { pushUrl: true })}
    />
  );
}
