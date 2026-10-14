"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Fragment } from "react";
import { Bell, Chrome, FolderOpen, HelpCircle, Home, Images, ListTodo, Mail, MessageSquare, Plug, Search, Send, Settings, UserPlus } from "lucide-react";
import Link from "next/link";
import { useStatusLoaded } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useActiveTeamRunCount } from "@/components/notifications/teamRunActivity";
import { useActivity } from "@/hooks/useActivity";
import { useNewMailCount } from "@/components/mail/useTeamMail";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { recordMessagesNavigation } from "./chatHistory";
import { HistoryButtons } from "./HistoryButtons";
import { activateMessagesPanel, openInviteDialog, openPanel, useWorkspaceState } from "./workspaceController";
import { describePanel, MESSAGES_PANEL_ID } from "./panelRegistry";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

const DESTINATIONS = [
  { panel: MESSAGES_PANEL_ID, label: "Messages", Icon: MessageSquare },
  { panel: "teams-dashboard", label: "Home", Icon: Home },
  { panel: "activity", label: "Activity", Icon: Bell },
  // The workspace's shared agent mailbox. Not "Inbox": that is the company inbox tab.
  { panel: "mail", label: "Mail", Icon: Mail },
  // Every task-board page lives on Work; the rail opens it on Tasks.
  { panel: "teams-issues", label: "Work", Icon: ListTodo },
  // Every document and deliverable, one click away (stock Paperclip puts
  // Artifacts beside Tasks too). Chat's document cards are the usual way in.
  { panel: "teams-artifacts", label: "Library", Icon: Images },
  { panel: "browser", label: "Browser", Icon: Chrome },
  { panel: "file-viewer", label: "Browse workspace files", Icon: FolderOpen },
  { panel: "connections", label: "Connections", Icon: Plug },
] as const;

/** The global rail stays available when Messages hides the dock tabs. */
export function WorkspaceHeader({ chatReady = true, onNavigate }: {
  chatReady?: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
}): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { open: openMarketplace } = useMarketplaceDrawer();
  const { activePanelId } = useWorkspaceState();
  const activeTeamRunCount = useActiveTeamRunCount();
  // Stock Paperclip's inbox badge: open approvals and failed runs always, the rest while unseen.
  const { attentionCount } = useActivity();
  const newMail = useNewMailCount();
  // The Browser panel watches an AgentCore Browser session, so the rail offers
  // it only once a status body has landed for this workspace — `false` hides
  // it rather than offering a panel that can only render its empty state.
  const browserShown = useStatusLoaded();
  const destinations = browserShown ? DESTINATIONS : DESTINATIONS.filter((d) => d.panel !== "browser");
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <HistoryButtons />
      <button type="button" className="workspace-rail__search" aria-label="Search messages, agents, issues, and projects" title="Search (⌘/Ctrl K)" onClick={() => { setOpen(true); onNavigate?.(); }}>
        <Search size={20} aria-hidden="true" />
      </button>
      {destinations.map(({ panel, label, Icon }) => {
        // The panel KEY is not always the panel ID — a control-key destination
        // (Connections) opens as `control:connections`. Resolve through the
        // registry so the active marker follows the id the dock actually uses.
        const panelId = describePanel(panel, {})?.panelId ?? panel;
        // Team runs are team work, so their count rides on Work; Activity badges what needs you.
        const runs = panel === "teams-issues" ? activeTeamRunCount : 0;
        const unseen = panel === "activity" ? attentionCount : 0;
        const mail = panel === "mail" ? newMail : 0;
        const badge = runs || unseen || mail;
        const accessibleLabel = runs
          ? `${label}, ${runs} active team ${runs === 1 ? "run" : "runs"}`
          : unseen ? `${label}, ${unseen} need${unseen === 1 ? "s" : ""} you`
            : mail ? `${label}, ${mail} new` : label;
        // Messaging never needs the container; the other destinations do.
        return <Fragment key={panel}><button type="button" aria-label={accessibleLabel} title={accessibleLabel}
          disabled={!chatReady && panel !== MESSAGES_PANEL_ID && panel !== "mail"}
          aria-current={activePanelId === panelId ? "page" : undefined}
          onClick={() => {
            if (panel === "file-viewer") capture("file_browser_opened");
            if (panel === "browser") capture("browser_view_opened");
            if (panel === MESSAGES_PANEL_ID) { activateMessagesPanel(); recordMessagesNavigation(); }
            else openPanel(panel, {}, { pushUrl: true });
            onNavigate?.();
          }}>
          <span className="workspace-rail__icon"><Icon size={20} aria-hidden="true" />
            {badge > 0 && <span className="workspace-rail__activity-badge" aria-hidden="true">{badge > 9 ? "9+" : badge}</span>}
          </span>
          {label !== "Browse workspace files" && <span>{label}</span>}
        </button>
        {/* Hiring is a primary verb for AI employees: the marketplace drawer, not a
            dock panel, and it needs no container, so it is never disabled. */}
        {panel === "teams-issues" && (
          <button type="button" aria-label="Hire an agent" title="Hire an agent"
            onClick={() => { openMarketplace({ view: "browse" }); onNavigate?.(); }}>
            <span className="workspace-rail__icon"><UserPlus size={20} aria-hidden="true" /></span>
            <span>Hire</span>
          </button>
        )}
        {panel === "teams-issues" && (
          <button type="button" aria-label="Invite people" title="Invite people" onClick={() => { openInviteDialog(); onNavigate?.(); }}>
            <span className="workspace-rail__icon"><Send size={20} aria-hidden="true" /></span>
            <span>Invite</span>
          </button>
        )}</Fragment>;
      })}
      <div className="workspace-rail__spacer" />
      {/* Appearance sits above the help/settings/account group: it changes how
          the app looks, not what it does, so it reads as the least consequential
          of the bottom controls and takes the position furthest from the
          account button. */}
      <ThemeToggles />
      <button type="button" onClick={() => { openSupport(); onNavigate?.(); }} aria-label="Get help" title="Get help"><HelpCircle size={20} aria-hidden="true" /></button>
      <Link href="/settings" aria-label="Settings" title="Settings"><Settings size={20} aria-hidden="true" /></Link>
      <UserButton appearance={{ elements: { avatarBox: "h-8 w-8" } }} />
    </nav>
  );
}
