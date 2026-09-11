"use client";

import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { Paperclip, PinOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { useAsyncAction } from "./useAsyncAction";
import type { RoomMessage, RoomParticipant, RoomRunView, RoomSummary } from "./types";

export type DetailsTab = "members" | "pins" | "files" | "links" | "about";

const TABS: Array<{ id: DetailsTab; label: string }> = [
  { id: "members", label: "Members" }, { id: "pins", label: "Pins" },
  { id: "files", label: "Files" }, { id: "links", label: "Links" }, { id: "about", label: "About" },
];
const URL_PATTERN = /https?:\/\/[^\s<>()]+/g;

export interface RoomDetailsPanelProps {
  room?: RoomSummary;
  roomName: string;
  tab: DetailsTab;
  onSelectTab: (tab: DetailsTab) => void;
  participants: readonly RoomParticipant[];
  currentUserId: string; // part of the shipped details contract; the panel itself renders no viewer-specific row
  actionable: boolean;
  messages: readonly RoomMessage[];
  pinnedMessageIds: readonly string[];
  activeRuns: readonly RoomRunView[];
  onOpenMessage: (messageId: string) => void;
  onUnpin?: (messageId: string) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
  members: ReactNode; // the shipped Members section, lifted verbatim from RoomPanel
}

/**
 * Links a room has shared, newest first: every `https?://` run in non-deleted
 * message text, put through the same `safeHref` guard the markdown renderer
 * uses and deduped by href. Derived from loaded history only -- it fills in as
 * the viewer scrolls back, and never paginates on its own.
 */
export function extractLinks(messages: readonly RoomMessage[]) {
  const seen = new Map<string, { href: string; label: string; messageId: string; at: string }>();
  for (const message of messages) {
    if (message.deletedAt) continue;
    for (const match of message.text.match(URL_PATTERN) ?? []) {
      const href = safeHref(match.replace(/[.,;:]+$/, ""));
      if (!href || seen.has(href)) continue;
      seen.set(href, { href, label: href.replace(/^https?:\/\//, ""), messageId: message.messageId, at: message.createdAt });
    }
  }
  return [...seen.values()].sort((left, right) => Date.parse(right.at) - Date.parse(left.at));
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
  pinnedMessageIds,
  activeRuns,
  onOpenMessage,
  onUnpin,
  onLeaveRoom,
  members,
}: RoomDetailsPanelProps) {
  const id = useId();
  const [confirmLeave, setConfirmLeave] = useState(false);
  const { pending, error, run, reset } = useAsyncAction();
  const authorName = (message: RoomMessage) =>
    participants.find((participant) => participant.kind === message.author.kind && participant.id === message.author.id)?.displayName
      ?? message.author.displayName;
  const jump = (messageId: string) => <Button type="button" variant="ghost" size="sm" aria-label="Jump to message"
    onClick={() => onOpenMessage(messageId)}>Jump</Button>;

  function selectTab(next: DetailsTab) {
    // The pending/error pair is shared across tabs (see useAsyncAction's doc
    // comment), so clear it on the way out rather than leaking a failed unpin
    // into the tab the viewer opens next.
    reset();
    setConfirmLeave(false);
    onSelectTab(next);
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = (TABS.findIndex((entry) => entry.id === tab) + step + TABS.length) % TABS.length;
    selectTab(TABS[next].id);
    (event.currentTarget.children[next] as HTMLElement | undefined)?.focus();
  }

  const files = messages.filter((message) => !message.deletedAt && message.attachments?.length);
  const links = extractLinks(messages);
  const activeWork = <section aria-labelledby={`${id}-runs`}>
    <h4 id={`${id}-runs`} className="font-semibold">Active work</h4>
    {activeRuns.length === 0 ? <p className="text-sm text-muted-foreground">No active runs.</p>
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

      {tab === "pins" && (pinnedMessageIds.length === 0
        ? <p className="text-sm text-muted-foreground">No pinned messages yet.</p>
        : <ul aria-label="Pinned messages">{[...pinnedMessageIds].reverse().map((messageId) => {
          const message = messages.find((candidate) => candidate.messageId === messageId);
          return <li key={messageId} className="room-details-row">
            {message
              ? <div className="flex-1 min-w-0">
                <p className="text-sm text-muted-foreground">{authorName(message)} · {messageTime(message.createdAt)}</p>
                <p className="room-wrap">{message.text.slice(0, 120)}</p>
              </div>
              // A pin older than the loaded window still has to be reachable --
              // Jump hands it to the shipped scroll/highlight path.
              : <p className="flex-1 text-sm text-muted-foreground">Message not loaded yet</p>}
            {jump(messageId)}
            {onUnpin && <Button type="button" variant="ghost" size="icon-sm" aria-label="Unpin this message"
              disabled={!actionable || pending} aria-busy={pending}
              onClick={() => void run(() => onUnpin(messageId), "The pin could not be removed.")}><PinOff aria-hidden="true" /></Button>}
          </li>;
        })}</ul>)}

      {tab === "files" && (files.length === 0
        ? <p className="text-sm text-muted-foreground">No files shared yet.</p>
        : <ul aria-label="Shared files">{files.map((message) => <li key={message.messageId} className="room-details-row">
          <Paperclip size={14} aria-hidden="true" className="mt-1" />
          <div className="flex-1 min-w-0">
            <p className="room-wrap">{(message.attachments ?? []).map((attachment) => attachment.fileName).join(", ")}</p>
            <p className="text-sm text-muted-foreground">{authorName(message)} · {messageTime(message.createdAt)}</p>
          </div>
          {jump(message.messageId)}
        </li>)}</ul>)}

      {tab === "links" && (links.length === 0
        ? <p className="text-sm text-muted-foreground">No links shared yet.</p>
        : <ul aria-label="Shared links">{links.map((link) => <li key={link.href} className="room-details-row">
          <p className="room-wrap flex-1"><a href={link.href} target="_blank" rel="noopener noreferrer"
            className="text-info underline underline-offset-4">{link.label}</a></p>
          {jump(link.messageId)}
        </li>)}</ul>)}

      {tab === "about" && <>
        <section aria-labelledby={`${id}-about`}>
          <h4 id={`${id}-about`} className="font-semibold">About {roomName}</h4>
          <p className="room-wrap">{room?.topic ?? "No topic yet."}</p>
          <p className="text-sm text-muted-foreground">{participants.length} members</p>
        </section>
        {activeWork}
        {/* Leaving is irreversible for a private channel, so it confirms here the way
            the header's Leave dialog does -- inline rather than in a second dialog. */}
        {onLeaveRoom && (confirmLeave
          ? <div className="room-details-row">
            <p className="flex-1">Leave {roomName}? You will lose access to this conversation and its messages.</p>
            <Button type="button" variant="outline" disabled={pending} onClick={() => setConfirmLeave(false)}>Cancel</Button>
            <Button type="button" variant="destructive" disabled={!actionable || pending}
              onClick={() => void run(onLeaveRoom, "The channel could not be left.")}>{pending ? "Leaving…" : "Leave channel"}</Button>
          </div>
          : <Button type="button" variant="outline" className="mt-4" disabled={!actionable}
            onClick={() => { reset(); setConfirmLeave(true); }}>Leave channel</Button>)}
      </>}

      {error && <p role="alert" className="text-sm text-err">{error}</p>}
    </div>
  </>;
}
