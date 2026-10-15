// Mission Control: one tab group — CEO chat in front, Activity behind it
// (Activity's For you holds what the Inbox tab used to show). The shell is
// tabs-only (see WorkspaceShell's `disableDnd`), so these are tabs in a single
// group rather than tiled panes.
//
// Dockview's default renderer is `onlyWhenVisible`, so an inactive tab never
// mounts and its reads do not fire for every owner on first load. Chat keeps
// `renderer: "always"` because its state must survive being tabbed away from.
//
// Built by code (never persisted as a preset blob) so the default evolves with
// releases; Reset re-applies it.
import type { DockviewApi } from "dockview-react";
import { MESSAGES_PANEL_ID, DEFAULT_AGENT_SENTINEL } from "./panelRegistry";

export function buildMissionControlLayout(api: DockviewApi): void {
  api.addPanel({
    id: MESSAGES_PANEL_ID,
    component: "chat",
    title: "Chat",
    params: { agentId: DEFAULT_AGENT_SENTINEL },
    renderer: "always",
  });
  // No `position`: dockview falls back to the active group, so each of these
  // stacks as a tab beside Chat instead of splitting the dock.
  api.addPanel({
    id: "activity",
    component: "activity",
    title: "Activity",
    params: {},
  });
  api.getPanel(MESSAGES_PANEL_ID)?.api.setActive();
}
