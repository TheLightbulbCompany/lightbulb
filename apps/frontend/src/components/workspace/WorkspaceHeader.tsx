"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Bell, HelpCircle, Home, Images, LayoutGrid, ListTodo, MessageSquare, Search, Settings, Users } from "lucide-react";
import Link from "next/link";
import { useActiveTeamRunCount } from "@/components/notifications/teamRunActivity";
import { useNeedsYouBadge } from "@/components/activity/useNeedsYouBadge";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { recordMessagesNavigation } from "./chatHistory";
import { HistoryButtons } from "./HistoryButtons";
import { activateMessagesPanel, closePane, openPanel, useWorkspaceState, type Place } from "./workspaceController";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useHomeNeedsYou } from "@/components/tasks/useRoomThreads";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

// The six places, and nothing else (spec 3.1; Team added 2026-11-05). Browser, Files and Mail open in
// the right pane (the conversation header, links, ⌘K); Settings is at the bottom.
type Destination = { place: Place; panel: string; label: string; Icon: typeof Home };
const DESTINATIONS: readonly Destination[] = [
  // Home is the old Activity place grown: the viewer's threads lead, For you and the feed follow. Its panel key
  // stays "activity", so every ?panel=activity link (notifications, filters, focus) opens Home.
  { place: "activity", panel: "activity", label: "Home", Icon: Home },
  { place: "chat", panel: "chat", label: "Chat", Icon: MessageSquare },
  // Every task-board page lives on Work; the rail opens it on Tasks (on Automations with the super-agent on).
  { place: "work", panel: "teams-issues", label: "Work", Icon: ListTodo },
  // The owner's AI staff: see, hire and manage specialists, and message one (its DM lives here, not in the sidebar).
  { place: "team", panel: "teams-agents", label: "Team", Icon: Users },
  // Every document and deliverable (stock Paperclip's Artifacts).
  { place: "library", panel: "teams-artifacts", label: "Library", Icon: Images },
  // Apps the agents built, opened inside the pane.
  { place: "canvas", panel: "canvas", label: "Canvas", Icon: LayoutGrid },
];

// Super-agent (founder, 6 Nov 2026): Home is the chats and the project spaces; what Home showed (Needs you, mentions,
// Ideas, the feed, Working/Done/Closed) is Activity, which keeps the panel key "activity" and every link to it.
const SUPER_AGENT_DESTINATIONS: readonly Destination[] = [
  { place: "home", panel: "home", label: "Home", Icon: Home },
  { place: "activity", panel: "activity", label: "Activity", Icon: Bell },
  ...DESTINATIONS.slice(1),
];

/** The global rail: the places, search, history and the account controls. */
export function WorkspaceHeader({ chatReady = true, onNavigate }: {
  chatReady?: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
}): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { place: current } = useWorkspaceState();
  const activeTeamRunCount = useActiveTeamRunCount();
  const superAgent = useSuperAgent();
  // Home's badge is Home's Needs you count, from the one read Home renders (super-agent). Without it, what
  // For you → Needs you shows (the desk, minus dismissed and snoozed items, plus failed-run groups) and unseen
  // mentions, DMs and Paperclip items.
  const needsYou = useHomeNeedsYou();
  const legacyBadge = useNeedsYouBadge(superAgent);
  const homeCount = superAgent ? needsYou : legacyBadge;
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <HistoryButtons />
      <button type="button" className="workspace-rail__search" aria-label="Search messages, agents, issues, and projects" title="Search (⌘/Ctrl K)" onClick={() => { setOpen(true); onNavigate?.(); }}>
        <Search size={20} aria-hidden="true" />
      </button>
      {(superAgent ? SUPER_AGENT_DESTINATIONS : DESTINATIONS).map(({ place, panel, label, Icon }) => {
        // Team runs are team work, so their count rides on Work; Activity (Home without the super-agent) badges what
        // needs you.
        const runs = place === "work" && !superAgent ? activeTeamRunCount : 0;
        const unseen = place === "activity" ? homeCount : 0;
        const badge = runs || unseen;
        const accessibleLabel = runs
          ? `${label}, ${runs} active team ${runs === 1 ? "run" : "runs"}`
          : unseen ? `${label}, ${unseen} need${unseen === 1 ? "s" : ""} you` : label;
        // Messaging never needs the container; the other places do.
        return <button key={place} type="button" aria-label={accessibleLabel} title={accessibleLabel}
          disabled={!chatReady && place !== "chat"}
          aria-current={current === place ? "page" : undefined}
          onClick={() => {
            // A new place starts clean: the pane belonged to the place being left.
            if (place !== current) closePane();
            if (place === "chat") { activateMessagesPanel(); recordMessagesNavigation(); }
            else openPanel(place === "work" && superAgent ? "teams-routines" : panel, {}, { pushUrl: true });
            onNavigate?.();
          }}>
          <span className="workspace-rail__icon"><Icon size={20} aria-hidden="true" />
            {badge > 0 && <span className="workspace-rail__activity-badge" aria-hidden="true">{badge > 9 ? "9+" : badge}</span>}
          </span>
          <span>{label}</span>
        </button>;
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
