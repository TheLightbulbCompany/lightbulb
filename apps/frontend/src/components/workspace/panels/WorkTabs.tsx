"use client";

// The Work place's tab strip. Every task-board page opens in the one Work main
// view (panelRegistry WORK_PANEL_ID); a tab here re-targets that same view.
import { PanelTabs } from "@/components/teams/shell";
import { WORK_TEAMS_KEYS, WORK_THREADS_KEY } from "../panelRegistry";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { openPanel } from "../workspaceController";

export { WORK_TEAMS_KEYS };

const LABELS: Record<(typeof WORK_TEAMS_KEYS)[number] | typeof WORK_THREADS_KEY, string> = {
  threads: "Threads",
  issues: "Tasks",
  projects: "Projects",
  goals: "Goals",
  routines: "Automations",
  cases: "Cases",
  agents: "Agents",
  money: "Money",
};

export function WorkTabs({ teamsKey }: { teamsKey: string }): React.ReactElement {
  const keys = useSuperAgent() ? [WORK_THREADS_KEY, ...WORK_TEAMS_KEYS] as const : WORK_TEAMS_KEYS;
  return (
    <PanelTabs
      ariaLabel="Work"
      className="px-6 pt-3"
      tabs={keys.map((key) => ({ value: key, label: LABELS[key] }))}
      value={teamsKey}
      onValueChange={(key) => openPanel(`teams-${key}`, {}, { pushUrl: true })}
    />
  );
}
