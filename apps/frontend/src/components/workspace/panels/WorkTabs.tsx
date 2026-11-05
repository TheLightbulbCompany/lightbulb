"use client";

// The Work place's tab strip. Every task-board page opens in the one Work main
// view (panelRegistry WORK_PANEL_ID); a tab here re-targets that same view.
import { PanelTabs } from "@/components/teams/shell";
import { WORK_TEAMS_KEYS, WORK_THREADS_KEY } from "../panelRegistry";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useTeamsApi } from "@/hooks/useTeamsApi";
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

/** With the super-agent on, the Paperclip-era pages that threads replace stay out of the strip (Claude Projects:
 *  the work, its schedules, its people, its money). Their URLs still open them. */
const SUPER_AGENT_HIDDEN: ReadonlySet<string> = new Set(["projects", "goals", "cases"]);

export function WorkTabs({ teamsKey }: { teamsKey: string }): React.ReactElement {
  const superAgent = useSuperAgent();
  // The Paperclip task board stays reachable as "Older tasks", last, only while it holds any (or is the page shown).
  const { data: older } = useTeamsApi().read<{ issues?: unknown[] }>(superAgent ? "/issues?excludeRoutineExecutions=true&limit=1" : null);
  const showOlder = !!older?.issues?.length || teamsKey === "issues";
  type Key = keyof typeof LABELS;
  const keys: readonly Key[] = superAgent
    ? [WORK_THREADS_KEY, ...WORK_TEAMS_KEYS.filter((k) => k !== "issues" && (!SUPER_AGENT_HIDDEN.has(k) || k === teamsKey)), ...(showOlder ? ["issues" as const] : [])]
    : WORK_TEAMS_KEYS;
  return (
    <PanelTabs
      ariaLabel="Work"
      className="px-6 pt-3"
      tabs={keys.map((key) => ({ value: key, label: superAgent && key === "issues" ? "Older tasks" : LABELS[key] }))}
      value={teamsKey}
      onValueChange={(key) => openPanel(`teams-${key}`, {}, { pushUrl: true })}
    />
  );
}
