// Mission Control: sentinel CEO chat on the left, Dashboard and Inbox
// stacked on the right. Built by code (never persisted as a preset blob) so
// the default evolves with releases; Reset re-applies it.
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
  api.addPanel({
    id: "teams-dashboard",
    component: "teams",
    title: "Dashboard",
    params: { teamsKey: "dashboard" },
    position: { referencePanel: chatId, direction: "right" },
    // Chat keeps ~55% — dockview splits 50/50 by default; initialWidth on
    // the right group nudges it. 45% of a 1440 dock ≈ 648.
    initialWidth: 648,
  });
  api.addPanel({
    id: "teams-inbox",
    component: "teams",
    title: "Inbox",
    params: { teamsKey: "inbox" },
    position: { referencePanel: "teams-dashboard", direction: "below" },
  });
  api.getPanel(chatId)?.api.setActive();
}
