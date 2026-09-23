"use client";

import type { IDockviewHeaderActionsProps } from "dockview-react";
import { Columns2, Maximize2 } from "lucide-react";
import { MESSAGES_PANEL_ID } from "./panelRegistry";
import { TASKS_PANEL_ID, canSplitBesideMessages, setTasksPlacement } from "./workspaceController";

/**
 * Group-header action for the Tasks panel only: ⤢ Expand when it sits beside
 * the conversation, "Show beside chat" when it is a tab in the Messages group.
 * Renders nothing for every other panel and when there is no Messages panel
 * to sit beside.
 */
export function TasksHeaderActions({ activePanel, group, containerApi }: IDockviewHeaderActionsProps) {
  if (activePanel?.id !== TASKS_PANEL_ID) return null;
  const messages = containerApi.getPanel(MESSAGES_PANEL_ID);
  if (!messages) return null;
  const isTab = messages.group.id === group.id;
  if (isTab && !canSplitBesideMessages()) return null;
  const label = isTab ? "Show beside chat" : "Expand to a full tab";
  const Icon = isTab ? Columns2 : Maximize2;
  return (
    <div className="workspace-dock-actions">
      <button
        type="button"
        className="workspace-dock-action"
        aria-label={label}
        title={label}
        onClick={() => setTasksPlacement(isTab ? "split" : "tab")}
      >
        <Icon size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
