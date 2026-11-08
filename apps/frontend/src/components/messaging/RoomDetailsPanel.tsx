"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Paperclip } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useAsyncAction } from "./useAsyncAction";
import type { RoomRunView, RoomSummary } from "@lightbulb/shared";
import type { RoomMessage, RoomParticipant } from "./types";

export type DetailsTab = "members" | "files" | "about";

const ALL_TABS: Array<{ id: DetailsTab; label: string }> = [
  { id: "members", label: "Members" }, { id: "files", label: "Files" }, { id: "about", label: "Details" },
];

interface RoomDetailsPanelProps {
  room?: RoomSummary;
  roomName: string;
  tab: DetailsTab;
  onSelectTab: (tab: DetailsTab) => void;
  participants: readonly RoomParticipant[];
  actionable: boolean;
  messages: readonly RoomMessage[];
  activeRuns: readonly RoomRunView[];
  /** Super-agent: no "Active work" line. It counts room-agent runs only, so it said "No active runs" beside a Working
   *  request (dev walk N-F5); the Requests column beside the conversation shows what is working. */
  hideActiveWork?: boolean;
  onOpenMessage: (messageId: string) => void;
  onLeaveRoom?: () => Promise<void>;
  // kind === "channel" AND role === "owner" (not shared): mirrors RoomHeader's
  // Leave dialog -- explain why Leave isn't offered rather than saying nothing.
  ownerCannotLeave?: boolean;
  members: ReactNode; // the shipped Members section, lifted verbatim from RoomPanel
  /** The Lightbulb page's own DM with the assistant: no Members tab (nobody to manage). */
  hideMembers?: boolean;
}

function messageTime(at: string) {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function RoomDetailsPanel({
  room,
  roomName,
  tab,
  onSelectTab,
  participants,
  actionable,
  messages,
  activeRuns,
  hideActiveWork = false,
  onOpenMessage,
  onLeaveRoom,
  ownerCannotLeave,
  members,
  hideMembers = false,
}: RoomDetailsPanelProps) {
  const id = useId();
  const TABS = hideMembers ? ALL_TABS.filter((entry) => entry.id !== "members") : ALL_TABS;
  const [confirmLeave, setConfirmLeave] = useState(false);
  const noun = room?.kind === "group-dm" ? "group chat" : "channel";
  const leaveCancel = useRef<HTMLButtonElement>(null);
  const { pending, error, run, reset } = useAsyncAction();
  // Focus moves to Cancel as the confirm row appears -- the trigger it replaced
  // is gone, so focus would otherwise fall to <body> and the warning go unread.
  // An effect, not an inline callback ref: that re-attaches every render and
  // would steal focus back from whatever the viewer moved to.
  useEffect(() => { if (confirmLeave) leaveCancel.current?.focus(); }, [confirmLeave]);
  const authorName = (message: RoomMessage) =>
    participants.find((participant) => participant.kind === message.author.kind && participant.id === message.author.id)?.displayName
      ?? message.author.displayName;
  // A list of identically named "Jump to message" buttons tells a screen-reader
  // user nothing about which row they are on, so a resolved row names its own.
  const jump = (messageId: string, from?: RoomMessage) => <Button type="button" variant="ghost" size="sm"
    aria-label={from ? `Jump to message from ${authorName(from)} at ${messageTime(from.createdAt)}` : "Jump to message"}
    onClick={() => onOpenMessage(messageId)}>Jump</Button>;

  function selectTab(next: DetailsTab) {
    // The pending/error pair is shared across tabs (see useAsyncAction's doc
    // comment), so clear it on the way out rather than leaking a failed action
    // into the tab the viewer opens next.
    reset();
    setConfirmLeave(false);
    onSelectTab(next);
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = TABS.findIndex((entry) => entry.id === tab);
    const next = event.key === "ArrowRight" ? (current + 1) % TABS.length
      : event.key === "ArrowLeft" ? (current - 1 + TABS.length) % TABS.length
        : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    selectTab(TABS[next].id);
    (event.currentTarget.children[next] as HTMLElement | undefined)?.focus();
  }

  // RoomPanel re-renders on every room.run.frame -- many times a second while an agent streams -- so this is memoized.
  const files = useMemo(() => messages.filter((message) => !message.deletedAt && message.attachments?.length), [messages]);
  const activeWork = hideActiveWork ? null : <section aria-labelledby={`${id}-runs`}>
    <h4 id={`${id}-runs`} className="font-semibold">Active work</h4>
    {activeRuns.length === 0 ? <p className="text-ui text-muted-foreground">No active runs.</p>
      : activeRuns.map((activeRun) => <p key={activeRun.runId} className="room-wrap">
        {participants.find((participant) => participant.kind === "agent" && participant.id === activeRun.agentId)?.displayName ?? "Agent"} is working</p>)}
  </section>;

  return <>
    <div className="room-details-tabs" role="tablist" aria-label="Room details" onKeyDown={onTabKeyDown}>
      {TABS.map((entry) => <button key={entry.id} type="button" role="tab" id={`${id}-tab-${entry.id}`}
        aria-controls={`${id}-panel`} aria-selected={entry.id === tab} tabIndex={entry.id === tab ? 0 : -1}
        onClick={() => selectTab(entry.id)}>{entry.label}</button>)}
    </div>
    <div className="room-context-body" id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-tab-${tab}`} tabIndex={0}>
      {tab === "members" && <>{members}{activeWork}</>}

      {tab === "files" && (files.length === 0
        ? <p className="text-ui text-muted-foreground">No files in the messages loaded so far. Scroll back in the chat to load more.</p>
        : <ul aria-label="Shared files">{files.map((message) => <li key={message.messageId} className="room-details-row">
          <Paperclip size={14} aria-hidden="true" className="mt-1" />
          <div className="flex-1 min-w-0">
            <p className="room-wrap">{(message.attachments ?? []).map((attachment) => attachment.fileName).join(", ")}</p>
            <p className="text-ui text-muted-foreground">{authorName(message)} · {messageTime(message.createdAt)}</p>
          </div>
          {jump(message.messageId, message)}
        </li>)}</ul>)}

      {tab === "about" && <>
        <section aria-labelledby={`${id}-about`}>
          <h4 id={`${id}-about`} className="font-semibold">About {roomName}</h4>
          <p className="room-wrap">{room?.topic ?? "No topic yet."}</p>
          <p className="text-ui text-muted-foreground">{participants.length} members</p>
        </section>
        {activeWork}
        {/* Leaving is irreversible for a private channel, so it confirms here the way
            the header's Leave dialog does -- inline rather than in a second dialog. */}
        {onLeaveRoom && (confirmLeave
          ? <div className="room-details-row" role="alert">
            <p className="flex-1">Leave {roomName}? You will lose access to this conversation and its messages.</p>
            <Button ref={leaveCancel} type="button" variant="outline" disabled={pending}
              onClick={() => setConfirmLeave(false)}>Cancel</Button>
            <Button type="button" variant="destructive" disabled={!actionable || pending}
              onClick={() => void run(onLeaveRoom, `The ${noun} could not be left.`)}>{pending ? "Leaving…" : `Leave ${noun}`}</Button>
          </div>
          : <Button type="button" variant="outline" className="mt-4" disabled={!actionable}
            onClick={() => { reset(); setConfirmLeave(true); }}>Leave {noun}</Button>)}
        {/* An owner alone in a channel can't leave it -- explain rather than offering an
            action that would 400. Deleting it is reachable from the header's
            "..." menu, which already carries the typed-confirmation flow. */}
        {!onLeaveRoom && ownerCannotLeave
          && <p className="text-ui text-muted-foreground mt-4">You are the only person in this {noun}. Delete it from the conversation options menu.</p>}
      </>}

      {error && <p role="alert" className="text-ui text-err">{error}</p>}
    </div>
  </>;
}
