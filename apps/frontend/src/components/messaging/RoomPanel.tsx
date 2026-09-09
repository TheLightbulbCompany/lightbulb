"use client";

import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import type { HumanPresenceRow } from "@lightbulb/shared";
import { ArrowLeft, Check, Circle, CircleAlert, Hash, LockKeyhole, MessageCircle, Users } from "lucide-react";
import type { Components } from "react-markdown";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { RoomComposer, type RoomComposerProps } from "./RoomComposer";
import { roomKindLabel, type RoomContext, type RoomMessage, type RoomParticipant, type RoomState, type RoomSummary, type WorkState } from "./types";
import "./messaging.css";

export interface RoomPanelProps {
  ownerScopeId: string;
  /** Opaque canonical id. No URL-derived audience or identity assumptions. */
  roomId: string;
  room?: RoomSummary;
  state: RoomState;
  participants: readonly RoomParticipant[];
  /** Canonical order; rows remain distinct even when their authors match. */
  messages: readonly RoomMessage[];
  presence?: readonly HumanPresenceRow[];
  /** Already authorized and expiry-filtered refs for this room only. */
  typingRefs?: readonly string[];
  threads?: readonly { parentMessageId: string; replies: readonly RoomMessage[] }[];
  contexts?: readonly RoomContext[];
  composer: RoomComposerProps;
  threadComposer?: { parentMessageId: string; composer: RoomComposerProps };
  onSelectThread?: (parentMessageId: string | null) => void;
  onRetry?: () => void;
  /** Host may use panel width; otherwise the viewport supplies narrow layout. */
  narrow?: boolean;
}

const workLabel: Record<WorkState, string> = {
  ready: "Ready", queued: "Queued", working: "Working", paused: "Paused",
  "needs-approval": "Needs approval", completed: "Completed", failed: "Failed", offline: "Offline", unknown: "Unknown",
};
function WorkStatus({ state }: { state: WorkState }) {
  const tone = state === "completed" ? "text-ok" : state === "failed" ? "text-err"
    : state === "working" || state === "queued" ? "text-info" : state === "needs-approval" || state === "paused" ? "text-warn" : "text-muted-foreground";
  const Icon = state === "completed" ? Check : state === "failed" || state === "needs-approval" ? CircleAlert : Circle;
  return <span role="status" aria-label="Work status" className={`inline-flex items-center gap-1 text-sm ${tone}`}><Icon size={14} aria-hidden="true" />{workLabel[state]}</span>;
}

// Keep react-markdown's default URL sanitizer. Overrides remove company attachment
// download/Paperclip navigation side effects; images never initiate a remote fetch.
const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    const safe = safeHref(href);
    return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
};

const narrowQuery = "(max-width: 859px)";
function subscribeNarrow(callback: () => void) {
  const media = window.matchMedia(narrowQuery);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}
const getNarrow = () => window.matchMedia(narrowQuery).matches;
const getServerNarrow = () => false;

/**
 * Parent must replace ALL records and controlled drafts atomically with owner/room
 * turnover and remove revoked records immediately. Local context/menus remount at
 * this boundary; no component cache, transport, or browser storage holds history.
 */
export function RoomPanel(props: RoomPanelProps) {
  if (props.state === "access-removed" || props.state === "unavailable") return <section className="room-panel room-blocked" aria-label="Conversation unavailable">
    <p role="status" aria-label="Room status">{props.state === "access-removed" ? "Access removed. You can no longer view this conversation." : "This conversation is unavailable."}</p>
    {props.state === "unavailable" && props.onRetry && <Button variant="outline" onClick={props.onRetry}>Try again</Button>}
  </section>;
  return <RoomContent key={JSON.stringify([props.ownerScopeId, props.roomId])} {...props} />;
}

function RoomContent({ roomId, room, state, participants, messages, presence = [], typingRefs = [],
  threads = [], contexts = [], composer, threadComposer, onSelectThread, onRetry, narrow }: RoomPanelProps) {
  const id = useId();
  const [membersOpen, setMembersOpen] = useState(false);
  const [selection, setSelection] = useState<{ kind: "thread" | "context"; id: string } | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const memberTrigger = useRef<HTMLButtonElement>(null);
  const viewportNarrow = useSyncExternalStore(subscribeNarrow, getNarrow, getServerNarrow);
  const isNarrow = narrow ?? viewportNarrow;
  const visibleRoom = room?.id === roomId ? room : undefined;
  const loading = state === "loading";
  const actionable = state === "ready" && !!visibleRoom;
  const authors = new Map(participants.map(participant => [participant.ref, participant]));
  const parentMessage = selection?.kind === "thread" ? messages.find(message => message.id === selection.id) : undefined;
  const selectedContext = selection?.kind === "context" ? contexts.find(context => context.id === selection.id) : undefined;
  const showDetail = !loading && !!(parentMessage || selectedContext);
  const viewers = participants.filter(person => person.kind === "human" && presence.some(row => row.personId === person.ref && row.inRoom && row.observedAt !== null && (row.state === "available" || row.state === "away")));
  const typing = [...new Set(typingRefs)].map(ref => authors.get(ref)).filter(person => person?.kind === "human");

  useEffect(() => {
    if (selection) detailHeading.current?.focus({ preventScroll: true });
    else trigger.current?.focus({ preventScroll: true });
  }, [selection]);

  function closeDetail() {
    if (selection?.kind === "thread") onSelectThread?.(null);
    setSelection(null);
  }
  function openDetail(kind: "thread" | "context", targetId: string, button: HTMLButtonElement, fromThread = false) {
    // A work link inside a thread unmounts when selected. Keep the room's opener.
    if (!fromThread) trigger.current = button;
    if (kind === "thread") onSelectThread?.(targetId);
    else if (selection?.kind === "thread") onSelectThread?.(null);
    setSelection({ kind, id: targetId });
  }

  function renderMessage(message: RoomMessage, replies = false) {
    const author = message.senderRef ? authors.get(message.senderRef) : undefined;
    const name = author?.name || "Unknown author";
    const timestamp = message.timestamp && !Number.isNaN(Date.parse(message.timestamp)) ? message.timestamp : undefined;
    return <article key={message.id} className="room-message" aria-label={`Message from ${name}`}>
      <Identity name={name} avatarOnly size="default" />
      <div className="room-message-content">
        <div className="room-message-byline"><span className="font-semibold truncate" title={name}>{name}</span>
          {author?.kind === "agent" && <span className="text-sm text-muted-foreground">Agent</span>}
          {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={timestamp}>{new Date(timestamp).toISOString().slice(11, 16)} UTC</time>}
        </div>
        <MarkdownView skipHtml components={roomMarkdown}>{message.body}</MarkdownView>
        {message.run && <div className="room-run"><WorkStatus state={message.run.state} />
          {message.run.details && <details><summary>Run details</summary><pre>{message.run.details.slice(0, 8192)}</pre>{message.run.details.length > 8192 && <p className="text-sm text-muted-foreground">Further details omitted.</p>}</details>}
        </div>}
        {message.contextIds?.map(contextId => {
          const context = contexts.find(record => record.id === contextId);
          return context && <Button key={contextId} type="button" variant="outline" className="room-work-link" disabled={!actionable}
            onClick={event => openDetail("context", context.id, event.currentTarget, replies)}>{context.title}</Button>;
        })}
        {!replies && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
          onClick={event => openDetail("thread", message.id, event.currentTarget)}><MessageCircle aria-hidden="true" />Reply</Button>}
      </div>
    </article>;
  }

  const RoomIcon = visibleRoom?.kind === "private" ? LockKeyhole : visibleRoom?.kind === "public" ? Hash : visibleRoom?.kind === "group" ? Users : MessageCircle;
  return <section className={`room-panel ${showDetail && !isNarrow ? "room-with-context" : ""}`} aria-label={visibleRoom?.name ?? "Conversation"}>
    <div className="room-conversation" hidden={showDetail && isNarrow}>
      <header className="room-header">
        <div className="room-title"><RoomIcon size={20} aria-hidden="true" /><h2 className="font-serif text-lg truncate" title={visibleRoom?.name}>{visibleRoom?.name ?? "Conversation"}</h2></div>
        {visibleRoom && <p className="text-sm text-muted-foreground">{roomKindLabel[visibleRoom.kind]}</p>}
        {!loading && <div className="room-audience">
          {viewers.length > 0 && <div role="group" aria-label="People viewing this room" className="room-facepile">
            {viewers.slice(0, 4).map(person => <span key={person.ref} role="group" aria-label={`${person.name} · In this room`} title={`${person.name} · In this room`}><Identity name={person.name} size="sm" avatarOnly /></span>)}
            {viewers.length > 4 && <span className="text-sm text-muted-foreground">and {viewers.length - 4} more</span>}
          </div>}
          <Button ref={memberTrigger} type="button" variant="ghost" aria-label="View members" aria-expanded={membersOpen} aria-controls={`${id}-members`}
            onClick={() => setMembersOpen(!membersOpen)}><Users aria-hidden="true" />Members</Button>
        </div>}
      </header>
      {membersOpen && !loading && <section id={`${id}-members`} aria-label="Room members" className="room-members"
        onKeyDown={event => { if (event.key === "Escape") { setMembersOpen(false); memberTrigger.current?.focus(); } }}>
        <h3 className="font-semibold">Room members</h3><ul>{participants.map(person => {
          const humanState = presence.find(row => row.personId === person.ref)?.state ?? "unknown";
          return <li key={person.ref} className="room-member"><Identity name={person.name} avatarOnly size="sm" /><span className="truncate flex-1" title={person.name}>{person.name}</span>
            {person.kind === "agent" ? <><span className="text-sm text-muted-foreground">Agent</span><WorkStatus state={person.agentState ?? "unknown"} /></> : <span className="text-sm text-muted-foreground">{humanState.charAt(0).toUpperCase() + humanState.slice(1)}</span>}
          </li>;
        })}</ul>
      </section>}
      {(loading || state === "reconnecting") && <div className="room-state"><p role="status" aria-label="Room status">{loading ? "Loading conversation…" : "Reconnecting. Sending is paused until the connection is restored."}</p>
        {state === "reconnecting" && onRetry && <Button variant="outline" onClick={onRetry}>Reconnect</Button>}
      </div>}
      <div role="region" aria-label="Messages" className="room-history" tabIndex={0}>
        {loading ? <div className="room-skeleton" aria-hidden="true"><span /><span /><span /></div> : messages.length === 0
          ? <div className="room-empty"><h3 className="font-serif text-lg">Start the conversation</h3><p className="text-sm text-muted-foreground">Share an update or choose an agent when you want help with work.</p></div>
          : messages.map(message => renderMessage(message))}
      </div>
      <p className="room-typing text-sm text-muted-foreground" role="status">{!loading && typing.length > 0 ? `${typing.slice(0, 3).map(person => person!.name).join(", ")}${typing.length > 3 ? " and others" : ""} ${typing.length === 1 ? "is" : "are"} typing…` : ""}</p>
      <RoomComposer {...composer} disabled={!actionable || composer.disabled} />
    </div>
    {showDetail && <aside className="room-context" aria-label={parentMessage ? "Thread" : selectedContext!.title} onKeyDown={event => { if (event.key === "Escape") closeDetail(); }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-serif text-lg room-wrap">{parentMessage ? "Thread" : selectedContext!.title}</h3>
      </header>
      <div className="room-context-body">{parentMessage ? <>
        {renderMessage(parentMessage, true)}
        <div className="room-thread-replies" aria-label="Thread replies">{threads.find(thread => thread.parentMessageId === parentMessage.id)?.replies.map(message => renderMessage(message, true))}</div>
      </> : <>
        <p className="text-sm text-muted-foreground">{selectedContext!.kind.charAt(0).toUpperCase() + selectedContext!.kind.slice(1)}</p>
        {selectedContext!.status && <WorkStatus state={selectedContext!.status} />}
        <div className="room-context-record">{selectedContext!.content}</div>
        {selectedContext!.onAction && selectedContext!.actionLabel && <Button disabled={!actionable || selectedContext!.busy} aria-busy={selectedContext!.busy} onClick={selectedContext!.onAction}>{selectedContext!.actionLabel}</Button>}
      </>}</div>
      {parentMessage && threadComposer?.parentMessageId === parentMessage.id && <RoomComposer {...threadComposer.composer} label="Reply to thread" disabled={!actionable || threadComposer.composer.disabled} />}
    </aside>}
  </section>;
}
