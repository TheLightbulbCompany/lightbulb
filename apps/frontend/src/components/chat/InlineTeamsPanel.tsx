"use client";

import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

// Control-panel keys for the inline Teams panels are namespaced `teams-*`
// so they never collide with the existing Control panel keys.
export const TEAMS_PANEL_PREFIX = "teams-";

export function isTeamsPanelKey(panel: string): boolean {
  return panel.startsWith(TEAMS_PANEL_PREFIX);
}

// Renders a Teams panel inside the /chat shell by reusing the exact same
// TeamsPanelRouter + providers that /teams uses (TeamsLayout). GatewayProvider
// is already present in /chat (page.tsx), which both providers depend on.
export function InlineTeamsPanel({ panel }: { panel: string }): React.ReactElement {
  const teamsPanel = panel.slice(TEAMS_PANEL_PREFIX.length);
  return (
    <TeamsEventsProvider>
      <CommandPaletteProvider>
        <div className="editorial-page h-full overflow-auto">
          <TeamsPanelRouter panel={teamsPanel} />
        </div>
      </CommandPaletteProvider>
    </TeamsEventsProvider>
  );
}
