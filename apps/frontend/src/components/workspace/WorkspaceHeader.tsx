"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Bell, FolderOpen, HelpCircle, Home, ListTodo, MessageSquare, Plug, Search, Settings } from "lucide-react";
import Link from "next/link";
import { capture } from "@/lib/analytics";
import { useActiveTeamRunCount } from "@/components/notifications/teamRunActivity";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { activateMessagesPanel, openPanel, useWorkspaceState } from "./workspaceController";
import { describePanel, MESSAGES_PANEL_ID } from "./panelRegistry";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

const DESTINATIONS = [
  { panel: MESSAGES_PANEL_ID, label: "Messages", Icon: MessageSquare },
  { panel: "teams-dashboard", label: "Home", Icon: Home },
  { panel: "teams-inbox", label: "Activity", Icon: Bell },
  { panel: "teams-issues", label: "Tasks", Icon: ListTodo },
  { panel: "file-viewer", label: "Browse workspace files", Icon: FolderOpen },
  { panel: "connections", label: "Connections", Icon: Plug },
] as const;

/** The global rail stays available when Messages hides the dock tabs. */
export function WorkspaceHeader({ chatReady = true }: { chatReady?: boolean }): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { activePanelId } = useWorkspaceState();
  const activeTeamRunCount = useActiveTeamRunCount();
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <button type="button" className="workspace-rail__search" aria-label="Search agents, issues, and projects" title="Search (⌘/Ctrl K)" onClick={() => setOpen(true)}>
        <Search size={20} aria-hidden="true" />
      </button>
      {DESTINATIONS.map(({ panel, label, Icon }) => {
        // The panel KEY is not always the panel ID — a control-key destination
        // (Connections) opens as `control:connections`. Resolve through the
        // registry so the active marker follows the id the dock actually uses.
        const panelId = describePanel(panel, {})?.panelId ?? panel;
        const showsActivity = panel === "teams-inbox" && activeTeamRunCount > 0;
        const accessibleLabel = showsActivity
          ? `${label}, ${activeTeamRunCount} active team ${activeTeamRunCount === 1 ? "run" : "runs"}`
          : label;
        // Messaging never needs the container; the other destinations do.
        return <button key={panel} type="button" aria-label={accessibleLabel} title={accessibleLabel}
          disabled={!chatReady && panel !== MESSAGES_PANEL_ID}
          aria-current={activePanelId === panelId ? "page" : undefined}
          onClick={() => {
            if (panel === "file-viewer") capture("file_browser_opened");
            if (panel === MESSAGES_PANEL_ID) activateMessagesPanel();
            else openPanel(panel, {}, { pushUrl: true });
          }}>
          <span className="workspace-rail__icon"><Icon size={20} aria-hidden="true" />
            {showsActivity && <span className="workspace-rail__activity-badge" aria-hidden="true">{activeTeamRunCount > 9 ? "9+" : activeTeamRunCount}</span>}
          </span>
          {label !== "Browse workspace files" && <span>{label}</span>}
        </button>;
      })}
      <div className="workspace-rail__spacer" />
      {/* Appearance sits above the help/settings/account group: it changes how
          the app looks, not what it does, so it reads as the least consequential
          of the bottom controls and takes the position furthest from the
          account button. */}
      <ThemeToggles />
      <button type="button" onClick={() => openSupport()} aria-label="Get help" title="Get help"><HelpCircle size={20} aria-hidden="true" /></button>
      <Link href="/settings" aria-label="Settings" title="Settings"><Settings size={20} aria-hidden="true" /></Link>
      <UserButton appearance={{ elements: { avatarBox: "h-8 w-8" } }} />
    </nav>
  );
}
