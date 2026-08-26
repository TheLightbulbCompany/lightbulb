// Mission Control: sentinel CEO chat on the left, Browser and Inbox stacked
// on the right. Browser, not Dashboard: the live view of the agent working
// is the thing a non-technical owner understands at a glance; the metrics
// dashboard overwhelmed them. Built by code (never persisted as a preset
// blob) so the default evolves with releases; Reset re-applies it.
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
    id: "browser",
    component: "browser",
    title: "Browser",
    params: {},
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
    position: { referencePanel: "browser", direction: "below" },
  });
  api.getPanel(chatId)?.api.setActive();
}
