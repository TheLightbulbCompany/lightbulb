"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Bell, Chrome, FolderOpen, HelpCircle, Home, ListTodo, MessageSquare, Search, Settings } from "lucide-react";
import Link from "next/link";
import { capture } from "@/lib/analytics";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { activateMessagesPanel, openPanel, useWorkspaceState } from "./workspaceController";
import { MESSAGES_PANEL_ID } from "./panelRegistry";

const DESTINATIONS = [
  { panel: MESSAGES_PANEL_ID, label: "Messages", Icon: MessageSquare },
  { panel: "teams-dashboard", label: "Home", Icon: Home },
  { panel: "teams-inbox", label: "Activity", Icon: Bell },
  { panel: "teams-issues", label: "Tasks", Icon: ListTodo },
  { panel: "browser", label: "Browser", Icon: Chrome },
  { panel: "file-viewer", label: "Browse workspace files", Icon: FolderOpen },
] as const;

/** The global rail stays available when Messages hides the dock tabs. */
export function WorkspaceHeader({ chatReady = true }: { chatReady?: boolean }): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { activePanelId } = useWorkspaceState();
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <button type="button" className="workspace-rail__search" aria-label="Search agents, issues, and projects" title="Search (⌘/Ctrl K)" onClick={() => setOpen(true)}>
        <Search size={20} aria-hidden="true" />
      </button>
      {DESTINATIONS.map(({ panel, label, Icon }) => (
        <button key={panel} type="button" aria-label={label} title={label} disabled={!chatReady}
          aria-current={activePanelId === panel ? "page" : undefined}
          onClick={() => {
            if (panel === "file-viewer") capture("file_browser_opened");
            if (panel === "browser") capture("browser_view_opened");
            if (panel === MESSAGES_PANEL_ID) activateMessagesPanel();
            else openPanel(panel, {}, { pushUrl: true });
          }}>
          <Icon size={20} aria-hidden="true" />
          {label !== "Browse workspace files" && <span>{label}</span>}
        </button>
      ))}
      <div className="workspace-rail__spacer" />
      <button type="button" onClick={() => openSupport()} aria-label="Get help" title="Get help"><HelpCircle size={20} aria-hidden="true" /></button>
      <Link href="/settings" aria-label="Settings" title="Settings"><Settings size={20} aria-hidden="true" /></Link>
      <UserButton appearance={{ elements: { avatarBox: "h-8 w-8" } }} />
    </nav>
  );
}
