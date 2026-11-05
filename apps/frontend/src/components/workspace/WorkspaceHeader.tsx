"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Bell, HelpCircle, Images, LayoutGrid, ListTodo, MessageSquare, Search, Settings } from "lucide-react";
import Link from "next/link";
import { useActiveTeamRunCount } from "@/components/notifications/teamRunActivity";
import { useNeedsYouBadge } from "@/components/activity/useNeedsYouBadge";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { recordMessagesNavigation } from "./chatHistory";
import { HistoryButtons } from "./HistoryButtons";
import { activateMessagesPanel, openPanel, useWorkspaceState, type Place } from "./workspaceController";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useThreads } from "@/components/tasks/useRoomThreads";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

// The five places, and nothing else (spec 3.1). Browser, Files and Mail open in
// the right pane (the conversation header, links, ⌘K); Settings is at the bottom.
const DESTINATIONS: readonly { place: Place; panel: string; label: string; Icon: typeof Bell }[] = [
  { place: "chat", panel: "chat", label: "Chat", Icon: MessageSquare },
  { place: "activity", panel: "activity", label: "Activity", Icon: Bell },
  // Every task-board page lives on Work; the rail opens it on Tasks.
  { place: "work", panel: "teams-issues", label: "Work", Icon: ListTodo },
  // Every document and deliverable (stock Paperclip's Artifacts).
  { place: "library", panel: "teams-artifacts", label: "Library", Icon: Images },
  // Apps the agents built, opened inside the pane.
  { place: "canvas", panel: "canvas", label: "Canvas", Icon: LayoutGrid },
];

/** The global rail: the places, search, history and the account controls. */
const WORKSPACE = { kind: "workspace" } as const;

export function WorkspaceHeader({ chatReady = true, onNavigate }: {
  chatReady?: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
}): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { place: current } = useWorkspaceState();
  const activeTeamRunCount = useActiveTeamRunCount();
  // Activity's badge: what For you → Needs you shows (the desk, minus dismissed and
  // snoozed items, plus failed-run groups), plus unseen mentions, DMs and Paperclip items.
  const attentionCount = useNeedsYouBadge();
  // With the super-agent on, Work leads with Threads (the overview); today's Tasks board stays a tab.
  const superAgent = useSuperAgent();
  // With it on, Work badges the threads waiting on you (team runs are the old board's).
  const { waitingOnMe } = useThreads(superAgent ? WORKSPACE : null);
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <HistoryButtons />
      <button type="button" className="workspace-rail__search" aria-label="Search messages, agents, issues, and projects" title="Search (⌘/Ctrl K)" onClick={() => { setOpen(true); onNavigate?.(); }}>
        <Search size={20} aria-hidden="true" />
      </button>
      {DESTINATIONS.map(({ place, panel, label, Icon }) => {
        // Team runs are team work, so their count rides on Work; Activity badges what needs you.
        const runs = place === "work" && !superAgent ? activeTeamRunCount : 0;
        const waiting = place === "work" && superAgent ? waitingOnMe : 0;
        const unseen = place === "activity" ? attentionCount : 0;
        const badge = runs || waiting || unseen;
        const accessibleLabel = runs
          ? `${label}, ${runs} active team ${runs === 1 ? "run" : "runs"}`
          : waiting ? `${label}, ${waiting} ${waiting === 1 ? "thread" : "threads"} waiting on you`
            : unseen ? `${label}, ${unseen} need${unseen === 1 ? "s" : ""} you` : label;
        // Messaging never needs the container; the other places do.
        return <button key={place} type="button" aria-label={accessibleLabel} title={accessibleLabel}
          disabled={!chatReady && place !== "chat"}
          aria-current={current === place ? "page" : undefined}
          onClick={() => {
            if (place === "chat") { activateMessagesPanel(); recordMessagesNavigation(); }
            else openPanel(place === "work" && superAgent ? "teams-threads" : panel, {}, { pushUrl: true });
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
