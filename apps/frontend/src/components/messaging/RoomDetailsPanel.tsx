"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Paperclip, PinOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { useAsyncAction } from "./useAsyncAction";
import type { RoomRunView, RoomSummary } from "@lightbulb/shared";
import type { RoomMessage, RoomParticipant } from "./types";
import type { RoomOutput } from "@/components/tasks/useRoomOutputs";

export type DetailsTab = "threads" | "members" | "pins" | "files" | "links" | "about";

const ROOM_TABS: Array<{ id: DetailsTab; label: string }> = [
  { id: "members", label: "Members" }, { id: "pins", label: "Pins" },
  { id: "files", label: "Files" }, { id: "links", label: "Links" }, { id: "about", label: "Details" },
];
const THREAD_TABS: Array<{ id: DetailsTab; label: string }> = [{ id: "threads", label: "Tasks" }, ...ROOM_TABS];
const URL_PATTERN = /https?:\/\/[^\s<>()]+/g;

interface RoomDetailsPanelProps {
  room?: RoomSummary;
  roomName: string;
  tab: DetailsTab;
  onSelectTab: (tab: DetailsTab) => void;
  participants: readonly RoomParticipant[];
  actionable: boolean;
  messages: readonly RoomMessage[];
  pinnedMessageIds: readonly string[];
  activeRuns: readonly RoomRunView[];
  /** Super-agent: no "Active work" line. It counts room-agent runs only, so it said "No active runs" beside a Working
   *  request (dev walk N-F5); the Requests column beside the conversation shows what is working. */
  hideActiveWork?: boolean;
  onOpenMessage: (messageId: string) => void;
  onUnpin?: (messageId: string) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
  // kind === "channel" AND role === "owner" (not shared): mirrors RoomHeader's
  // Leave dialog -- explain why Leave isn't offered rather than saying nothing.
  ownerCannotLeave?: boolean;
  members: ReactNode; // the shipped Members section, lifted verbatim from RoomPanel
  /** This room's threads (the room-scoped Work overview), first tab when given (super-agent on). */
  threads?: ReactNode;
  /** Super-agent: what this room's requests made, listed first under Files (the runtime's disk never is). */
  outputs?: readonly RoomOutput[];
  onOpenOutput?: (taskId: string) => void;
  /** The room has requests past the first page: Files offers "Show older" (useRoomOutputs). */
  olderOutputs?: { loading: boolean; load: () => void };
  /** One view, no tab strip: the project pane's Files view (super-agent), titled "Files" by its header. */
  hideTabs?: boolean;
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
  tab: askedTab,
  onSelectTab,
  participants,
  actionable,
  messages,
  pinnedMessageIds,
  activeRuns,
  hideActiveWork = false,
  onOpenMessage,
  onUnpin,
  onLeaveRoom,
  ownerCannotLeave,
  members,
  threads,
  outputs,
  onOpenOutput,
  olderOutputs,
  hideTabs = false,
}: RoomDetailsPanelProps) {
  const id = useId();
  const TABS = threads ? THREAD_TABS : ROOM_TABS;
  // A "threads" tab asked for with the switch off (a stale state) shows Members.
  const tab = askedTab === "threads" && !threads ? "members" : askedTab;
  const [confirmLeave, setConfirmLeave] = useState(false);
  const noun = room?.kind === "group-dm" ? "group chat" : "channel";
  const [unpinning, setUnpinning] = useState<string | null>(null);
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

  async function unpin(messageId: string) {
    if (!onUnpin) return;
    setUnpinning(messageId);
    let unpinned = false;
    await run(() => onUnpin(messageId), "The pin could not be removed.", () => { unpinned = true; });
    setUnpinning(null);
    // The pin row unmounts once the caller's data reflects the unpin, taking
    // its Unpin button with it -- move focus to the (always-present) Pins tab
    // before that happens, rather than leaving it to fall to <body>.
    if (unpinned) document.getElementById(`${id}-tab-pins`)?.focus();
  }

  function selectTab(next: DetailsTab) {
    // The pending/error pair is shared across tabs (see useAsyncAction's doc
    // comment), so clear it on the way out rather than leaking a failed unpin
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

  // RoomPanel re-renders on every room.run.frame -- many times a second while
  // an agent streams -- and `links` runs a regex over every loaded message
  // body, so both must be memoized on the one thing that actually changes
  // them rather than re-derived on every one of those renders.
  const files = useMemo(() => messages.filter((message) => !message.deletedAt && message.attachments?.length), [messages]);
  const links = useMemo(() => extractLinks(messages), [messages]);
  const activeWork = hideActiveWork ? null : <section aria-labelledby={`${id}-runs`}>
    <h4 id={`${id}-runs`} className="font-semibold">Active work</h4>
    {activeRuns.length === 0 ? <p className="text-ui text-muted-foreground">No active runs.</p>
      : activeRuns.map((activeRun) => <p key={activeRun.runId} className="room-wrap">
        {participants.find((participant) => participant.kind === "agent" && participant.id === activeRun.agentId)?.displayName ?? "Agent"} is working</p>)}
  </section>;

  return <>
    {!hideTabs && <div className="room-details-tabs" role="tablist" aria-label="Room details" onKeyDown={onTabKeyDown}>
      {TABS.map((entry) => <button key={entry.id} type="button" role="tab" id={`${id}-tab-${entry.id}`}
        aria-controls={`${id}-panel`} aria-selected={entry.id === tab} tabIndex={entry.id === tab ? 0 : -1}
        onClick={() => selectTab(entry.id)}>{entry.label}</button>)}
    </div>}
    <div className="room-context-body" id={`${id}-panel`} {...(hideTabs ? {} : { role: "tabpanel", "aria-labelledby": `${id}-tab-${tab}`, tabIndex: 0 })}>
      {tab === "threads" && threads}
      {tab === "members" && <>{members}{activeWork}</>}

      {tab === "pins" && (pinnedMessageIds.length === 0
        ? <p className="text-ui text-muted-foreground">No pinned messages yet.</p>
        : <ul aria-label="Pinned messages">{[...pinnedMessageIds].reverse().map((messageId) => {
          const found = messages.find((candidate) => candidate.messageId === messageId);
          // A deleted message keeps its pin row -- the row is the only way to
          // unpin it -- but never its text, the same way the conversation
          // renders "Message deleted" in place of the body.
          const message = found?.deletedAt ? undefined : found;
          const busy = unpinning === messageId;
          return <li key={messageId} className="room-details-row">
            {message
              ? <div className="flex-1 min-w-0">
                <p className="text-ui text-muted-foreground">{authorName(message)} · {messageTime(message.createdAt)}</p>
                <p className="room-wrap">{message.text.slice(0, 120)}</p>
              </div>
              // A pin older than the loaded window still has to be reachable --
              // Jump hands it to the shipped scroll/highlight path.
              : <p className="flex-1 text-ui text-muted-foreground">{found ? "Message deleted" : "Message not loaded yet"}</p>}
            {jump(messageId, message)}
            {onUnpin && <Button type="button" variant="ghost" size="icon-sm" aria-label={busy ? "Unpinning…" : "Unpin this message"}
              disabled={!actionable || pending} aria-busy={busy}
              onClick={() => void unpin(messageId)}><PinOff aria-hidden="true" /></Button>}
          </li>;
        })}</ul>)}

      {tab === "files" && !!outputs?.length && <ul aria-label="Made by tasks">{outputs.map((output) => <li key={output.taskId} className="room-details-row">
        <Paperclip size={14} aria-hidden="true" className="mt-1" />
        <div className="flex-1 min-w-0">
          <p className="room-wrap">{output.title}</p>
          <p className="text-ui text-muted-foreground room-wrap">{output.request}</p>
        </div>
        {onOpenOutput && <Button type="button" variant="ghost" size="sm" aria-label={`Open ${output.request}`} onClick={() => onOpenOutput(output.taskId)}>Open</Button>}
      </li>)}</ul>}
      {tab === "files" && olderOutputs && <Button type="button" variant="ghost" size="sm" className="justify-self-start" disabled={olderOutputs.loading}
        onClick={olderOutputs.load}>{olderOutputs.loading ? "Loading…" : "Show older"}</Button>}
      {tab === "files" && (files.length === 0
        ? outputs?.length ? null : <p className="text-ui text-muted-foreground">{outputs ? "No files yet. Files shared here and what tasks make show up here." : "No files in the messages loaded so far. Scroll back in the chat to load more."}</p>
        : <ul aria-label="Shared files">{files.map((message) => <li key={message.messageId} className="room-details-row">
          <Paperclip size={14} aria-hidden="true" className="mt-1" />
          <div className="flex-1 min-w-0">
            <p className="room-wrap">{(message.attachments ?? []).map((attachment) => attachment.fileName).join(", ")}</p>
            <p className="text-ui text-muted-foreground">{authorName(message)} · {messageTime(message.createdAt)}</p>
          </div>
          {jump(message.messageId, message)}
        </li>)}</ul>)}

      {tab === "links" && (links.length === 0
        ? <p className="text-ui text-muted-foreground">No links in the messages loaded so far. Scroll back in the chat to load more.</p>
        : <ul aria-label="Shared links">{links.map((link) => <li key={link.href} className="room-details-row">
          <p className="room-wrap flex-1"><a href={link.href} target="_blank" rel="noopener noreferrer"
            className="text-info underline underline-offset-4">{link.label}</a></p>
          {jump(link.messageId, messages.find((candidate) => candidate.messageId === link.messageId))}
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
