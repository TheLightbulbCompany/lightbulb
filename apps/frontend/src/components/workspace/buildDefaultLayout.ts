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
// BrowserDockPanel still resizes the real display, so the slot IS the agent's
// Chrome viewport — as a full-width tab that viewport gets wider, never
// smaller. Built by code (never persisted as a preset blob) so the default
// evolves with releases; Reset re-applies it.
import type { DockviewApi } from "dockview-react";
import { chatPanelId, DEFAULT_AGENT_SENTINEL } from "./panelRegistry";

export function buildMissionControlLayout(api: DockviewApi): void {
  const chatId = chatPanelId(DEFAULT_AGENT_SENTINEL);
  api.addPanel({
    id: chatId,
    component: "chat",
    title: "Chat",
    params: { agentId: DEFAULT_AGENT_SENTINEL },
    renderer: "always",
  });
  // No `position`: dockview falls back to the active group, so each of these
  // stacks as a tab beside Chat instead of splitting the dock.
  api.addPanel({
    id: "browser",
    component: "browser",
    title: "Browser",
    params: {},
  });
  api.addPanel({
    id: "teams-inbox",
    component: "teams",
    title: "Inbox",
    params: { teamsKey: "inbox" },
  });
  api.getPanel(chatId)?.api.setActive();
}
