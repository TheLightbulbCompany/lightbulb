"use client";

import { useTeamsWorkspaceStatus } from "@/hooks/useTeamsApi";
import {
  ProvisioningOverlay,
  SubscribeOverlay,
  ErrorOverlay,
} from "@/components/teams/TeamsLayout";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";

// Control-panel keys for the inline Teams panels are namespaced `teams-*`
// so they never collide with the existing Control panel keys.
export const TEAMS_PANEL_PREFIX = "teams-";

export function isTeamsPanelKey(panel: string): boolean {
  return panel.startsWith(TEAMS_PANEL_PREFIX);
}

// Renders a Teams panel inside the /chat shell by reusing the exact same
// TeamsPanelRouter + providers that /teams uses (TeamsLayout). GatewayProvider
// is already present in /chat (page.tsx), which both providers depend on.
export function InlineTeamsPanel({
  panel,
  detailId,
  openDetail,
}: {
  panel: string;
  detailId?: string | null;
  openDetail?: (panelKey: string, id: string) => void;
}): React.ReactElement {
  const teamsPanel = panel.slice(TEAMS_PANEL_PREFIX.length);
  // Mirror the workspace-status gate /teams applies in TeamsLayout: surface the
  // provisioning / subscribe / error states with the same overlays instead of
  // letting the panels fall through to generic loading/error. (loading and
  // ready both render the panel — identical to /teams.)
  const status = useTeamsWorkspaceStatus();
  if (status.kind === "provisioning") return <ProvisioningOverlay />;
  if (status.kind === "subscribe_required") return <SubscribeOverlay />;
  if (status.kind === "error") return <ErrorOverlay error={status.error} />;
  // The nav provider lets a list panel's "Open →" row open its detail view
  // in-shell (via openDetail) instead of leaving for /teams. openDetail is a
  // no-op fallback when the shell didn't supply one.
  return (
    <InlineTeamsNavProvider value={{ openDetail: openDetail ?? (() => {}) }}>
      <TeamsEventsProvider>
        <CommandPaletteProvider>
          <div className="editorial-page h-full overflow-auto">
            <TeamsPanelRouter panel={teamsPanel} detailId={detailId} />
          </div>
        </CommandPaletteProvider>
      </TeamsEventsProvider>
    </InlineTeamsNavProvider>
  );
}
