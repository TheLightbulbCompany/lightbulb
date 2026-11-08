"use client";

import "./workspace.css";
import { UserButton } from "@clerk/nextjs";
import { Bell, HelpCircle, Home, MessageSquare, PhoneCall, Search, Settings, Users } from "lucide-react";
import Link from "next/link";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { recordMessagesNavigation } from "./chatHistory";
import { HistoryButtons } from "./HistoryButtons";
import { TalkOrb } from "@/components/voice/TalkOrb";
import { LIGHTBULB } from "@/components/voice/data";
import { activateMessagesPanel, closePane, openPanel, useWorkspaceState, type Place } from "./workspaceController";
import { useNeedsYouCount } from "@/components/tasks/useRoomThreads";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

// Founder, 6 Nov 2026: Home is the chats and the project spaces; what Home showed (Needs you, mentions, Ideas, the
// feed, Working/Done/Closed) is Activity, which keeps the panel key "activity" and every link to it. Chat is on the
// rail (Lightbulb stays a communications platform): it opens the channels and DMs. No Work (its tasks are in Activity,
// its schedules in each channel's Scheduled view and Activity's Scheduled group, Money in Settings › Business).
// Settings is a place at the foot, inside the shell. Files and Mail open in the right pane.
type Destination = { place: Place; panel: string; label: string; Icon: typeof Home };
const DESTINATIONS: readonly Destination[] = [
  { place: "home", panel: "home", label: "Home", Icon: Home },
  { place: "activity", panel: "activity", label: "Activity", Icon: Bell },
  { place: "chat", panel: "chat", label: "Chat", Icon: MessageSquare },
  // The owner's AI staff: see, hire and manage specialists, and message one (its DM lives here, not in the sidebar).
  { place: "team", panel: "teams-agents", label: "Team", Icon: Users },
];
const SETTINGS: Destination = { place: "settings", panel: "settings", label: "Settings", Icon: Settings };

/** The global rail: the places, search, history and the account controls. */
export function WorkspaceHeader({ chatReady = true, onNavigate }: {
  chatReady?: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
}): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen } = useCommandPalette();
  const { place: current } = useWorkspaceState();
  // Activity's badge is needsYouCount, the one count Activity, Home and the tab title read.
  const needsYou = useNeedsYouCount();
  return (
    <nav className="workspace-rail" aria-label="Workspace">
      <HistoryButtons />
      <button type="button" className="workspace-rail__search" aria-label="Search channels, threads, files and messages" title="Search (⌘/Ctrl K)" onClick={() => { setOpen(true); onNavigate?.(); }}>
        <Search size={20} aria-hidden="true" />
      </button>
      {/* Hold to talk to the assistant, from every place (voice spec §5: owner mode, the Router in the DM). The rail,
          not a floating button: it never covers a composer, and its caption opens beside the rail while a talk is on. */}
      <TalkOrb name={LIGHTBULB} compact />
      {DESTINATIONS.map(({ place, panel, label, Icon }) => {
        // Activity badges what needs you.
        const badge = place === "activity" ? needsYou : 0;
        const accessibleLabel = badge ? `${label}, ${badge} need${badge === 1 ? "s" : ""} you` : label;
        // Messaging never needs the container; the other places do.
        return <button key={place} type="button" aria-label={accessibleLabel} title={accessibleLabel}
          disabled={!chatReady && place !== "chat"}
          aria-current={current === place ? "page" : undefined}
          onClick={() => {
            // A new place starts clean: the pane belonged to the place being left.
            if (place !== current) closePane();
            if (place === "chat") { activateMessagesPanel(); recordMessagesNavigation(); }
            else openPanel(panel, {}, { pushUrl: true });
            onNavigate?.();
          }}>
          <span className="workspace-rail__icon"><Icon size={20} aria-hidden="true" />
            {badge > 0 && <span className="workspace-rail__activity-badge" aria-hidden="true">{badge > 9 ? "9+" : badge}</span>}
          </span>
          <span>{label}</span>
        </button>;
      })}
      {/* The voice product (spec 2026-11-07 §5): its own four places at /voice. */}
      <Link href="/voice" aria-label="Voice" title="Voice"><PhoneCall size={20} aria-hidden="true" /><span>Voice</span></Link>
      <div className="workspace-rail__spacer" />
      {/* Appearance sits above the help/settings/account group: it changes how
          the app looks, not what it does, so it reads as the least consequential
          of the bottom controls and takes the position furthest from the
          account button. */}
      <ThemeToggles />
      <button type="button" onClick={() => { openSupport(); onNavigate?.(); }} aria-label="Get help" title="Get help"><HelpCircle size={20} aria-hidden="true" /></button>
      <button type="button" aria-label={SETTINGS.label} title={SETTINGS.label} aria-current={current === "settings" ? "page" : undefined}
        onClick={() => { if (current !== "settings") closePane(); openPanel(SETTINGS.panel, {}, { pushUrl: true }); onNavigate?.(); }}>
        <Settings size={20} aria-hidden="true" />
      </button>
      <UserButton appearance={{ elements: { avatarBox: "h-8 w-8" } }} />
    </nav>
  );
}
