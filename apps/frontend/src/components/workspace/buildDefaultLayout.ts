// Mission Control: one tab group — CEO chat in front, Browser and Inbox behind
// it. The shell is tabs-only (see WorkspaceShell's `disableDnd`), so these are
// three tabs in a single group rather than three tiled panes.
//
// Browser, not Dashboard: the live view of the agent working is the thing a
// non-technical owner understands at a glance; the metrics dashboard
// overwhelmed them.
//
// Tabbing it also RETIRES a cost the tiled layout paid. Dockview's default
// renderer is `onlyWhenVisible`, so an inactive tab never mounts: Browser's
// mount effect — which starts the container's headful Chrome, one idempotent
// launch per container lifetime — no longer fires for every owner on first
// load, only for owners who actually open the tab. Chat keeps
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
    title: "Messages",
    params: { agentId: DEFAULT_AGENT_SENTINEL },
    renderer: "always",
  });
  // No `position`: dockview falls back to the active group, so each of these
  // stacks as a tab beside Chat instead of splitting the dock.
  api.addPanel({
    id: "teams-inbox",
    component: "teams",
    title: "Inbox",
    params: { teamsKey: "inbox" },
  });
  api.getPanel(MESSAGES_PANEL_ID)?.api.setActive();
}
