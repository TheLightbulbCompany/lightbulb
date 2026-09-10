"use client";

import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { ArrowLeft, Check, Circle, CircleAlert, FileText, Hash, Link2, MessageCircle, Users } from "lucide-react";
import type { Components } from "react-markdown";
import type { ListenMode, MemberRef } from "@lightbulb/shared";
import { ToolActivityTrace } from "@/components/chat/ToolActivityTrace";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { cn } from "@/lib/utils";
import { RoomComposer, type RoomComposerProps } from "./RoomComposer";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { roomDisplayName } from "./types";
import type {
  PendingRoomMessage,
  DirectoryParticipant,
  RoomContext,
  RoomMessage,
  RoomParticipant,
  RoomRunView,
  RoomState,
  RoomSummary,
  WorkState,
} from "./types";
import "./messaging.css";

export interface RoomPanelProps {
  ownerScopeId: string;
  currentUserId: string;
  roomId: string;
  room?: RoomSummary;
  state: RoomState;
  httpReady: boolean;
  liveConnected: boolean;
  participants: readonly RoomParticipant[];
  messages: readonly RoomMessage[];
  pendingMessages?: readonly PendingRoomMessage[];
  typingMembers?: readonly { userId: string; displayName: string }[];
  runs?: readonly RoomRunView[];
  contexts?: readonly RoomContext[];
  composer: RoomComposerProps;
  threadComposer?: { parentMessageId: string; composer: RoomComposerProps };
  onSelectThread?: (parentMessageId: string | null) => void;
  onSelectParticipant?: (participant: RoomParticipant) => void | Promise<void>;
  onRetry?: () => void;
  onListenModeChange?: (agentId: string, mode: ListenMode) => void;
  addMemberCandidates?: readonly DirectoryParticipant[];
  onAddMember?: (member: MemberRef) => Promise<void>;
  onAbortRun?: (runId: string) => void;
  onSteerRun?: (runId: string, text: string) => void;
  onLinkToWork?: () => void;
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

const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    const safe = safeHref(href);
    return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
};

const narrowQuery = "(max-width: 859px)";
const overlayQuery = "(min-width: 860px) and (max-width: 1179px)";
function subscribeNarrow(callback: () => void) {
  const media = window.matchMedia(narrowQuery);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}
const getNarrow = () => window.matchMedia(narrowQuery).matches;
const getServerNarrow = () => false;

export function RoomPanel(props: RoomPanelProps) {
  return <RoomBoundary key={JSON.stringify([props.ownerScopeId, props.roomId])} {...props} />;
}

type Attempt = { observed: RoomComposerProps["sendState"]; state: RoomComposerProps["sendState"]; revision: number };

function RoomBoundary(props: RoomPanelProps) {
  const [attempts, setAttempts] = useState(() => new Map<string, Attempt>());

  function retainAttempt(composer: RoomComposerProps): RoomComposerProps {
    const key = composer.clientMessageId;
    const previous = attempts.get(key);
    const changed = composer.sendState !== "idle" && composer.sendState !== previous?.observed;
    const record = changed ? { observed: composer.sendState, state: composer.sendState, revision: previous?.revision ?? 0 } : previous;
    async function invoke(callback: RoomComposerProps["onSubmit"], request: Parameters<RoomComposerProps["onSubmit"]>[0]) {
      const revision = (record?.revision ?? 0) + 1;
      setAttempts((current) => new Map(current).set(key, { observed: composer.sendState, state: "sending", revision }));
      try {
        await callback(request);
      } catch {
        setAttempts((current) => {
          const latest = current.get(key);
          if (!latest || latest.revision !== revision || latest.state === "persisted" || latest.state === "rejected") return current;
          return new Map(current).set(key, { ...latest, state: "uncertain" });
        });
      }
    }
    return {
      ...composer,
      sendState: record?.state ?? composer.sendState,
      members: composer.members.filter((target) => props.participants.some((member) => member.kind === target.kind && member.id === target.id)),
      onSubmit: (request) => invoke(composer.onSubmit, request),
      onRetry: composer.onRetry ? (request) => invoke(composer.onRetry!, request) : undefined,
    };
  }
  const composer = retainAttempt(props.composer);
  const threadComposer = props.threadComposer && { ...props.threadComposer, composer: retainAttempt(props.threadComposer.composer) };
  if (props.state === "loading") return <section className="room-panel room-blocked" aria-label="Loading conversation">
    <p role="status" aria-label="Room status">Loading conversation…</p><div className="room-skeleton" aria-hidden="true"><span /><span /><span /></div>
  </section>;
  const matchingIdentity = !!props.ownerScopeId && !!props.roomId && props.room?.roomId === props.roomId &&
    props.messages.every((message) => message.roomId === props.roomId) &&
    (props.pendingMessages ?? []).every((message) => message.roomId === props.roomId) &&
    (props.contexts ?? []).every((context) => context.roomId === props.roomId);
  if (!matchingIdentity || props.state === "access-removed" || props.state === "unavailable") return <section className="room-panel room-blocked" aria-label="Conversation unavailable">
    <p role="status" aria-label="Room status">{props.state === "access-removed" ? "Access removed. You can no longer view this conversation." : "This conversation is unavailable."}</p>
    {matchingIdentity && props.state === "unavailable" && props.onRetry && <Button variant="outline" onClick={props.onRetry}>Try again</Button>}
  </section>;
  return <RoomContent {...props} composer={composer} threadComposer={threadComposer} />;
}

function RoomContent({
  roomId,
  currentUserId,
  room,
  state,
  httpReady,
  liveConnected,
  participants,
  messages,
  pendingMessages = [],
  typingMembers = [],
  runs = [],
  contexts = [],
  composer,
  threadComposer,
  onSelectThread,
  onSelectParticipant,
  onRetry,
  onListenModeChange,
  addMemberCandidates = [],
  onAddMember,
  onAbortRun,
  onSteerRun,
  onLinkToWork,
  narrow,
}: RoomPanelProps) {
  const id = useId();
  const [membersOpen, setMembersOpen] = useState(false);
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [addingMembers, setAddingMembers] = useState(false);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [memberError, setMemberError] = useState<string>();
  const [contextCollapsed, setContextCollapsed] = useState(true);
  const [selection, setSelection] = useState<{ kind: "thread" | "context"; id: string } | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const contextHeading = useRef<HTMLHeadingElement>(null);
  const contextTrigger = useRef<HTMLButtonElement>(null);
  const contextWasOpen = useRef(false);
  const memberTrigger = useRef<HTMLButtonElement>(null);
  const viewportNarrow = useSyncExternalStore(subscribeNarrow, getNarrow, getServerNarrow);
  const isNarrow = narrow ?? viewportNarrow;
  const visibleRoom = room?.roomId === roomId ? room : undefined;
  const actionable = httpReady && !!visibleRoom;
  const mainMessages = messages.filter((message) => message.threadRootMessageId === null);
  const parentMessage = selection?.kind === "thread" ? messages.find((message) => message.messageId === selection.id) : undefined;
  const selectedContext = selection?.kind === "context" ? contexts.find((context) => context.id === selection.id) : undefined;
  const showDetail = !!(parentMessage || selectedContext);
  const showContext = showDetail || !contextCollapsed;
  const activeRuns = runs.filter((run) => run.state === "running");
  const viewer = participants.find((participant) => participant.kind === "human" && participant.id === currentUserId);
  const canAddMembers = visibleRoom?.kind === "channel" && !!onAddMember;

  async function addSelectedMembers() {
    if (!onAddMember || selectedMembers.length === 0) return;
    setAddingMembers(true);
    setMemberError(undefined);
    try {
      for (const key of selectedMembers) {
        const member = addMemberCandidates.find((candidate) => `${candidate.kind}:${candidate.id}` === key);
        if (member) await onAddMember({ kind: member.kind, id: member.id });
      }
      setSelectedMembers([]);
    } catch (error) {
      setMemberError(error instanceof Error ? error.message : "Members could not be added.");
    } finally {
      setAddingMembers(false);
    }
  }

  useEffect(() => {
    if (selection) detailHeading.current?.focus({ preventScroll: true });
    else trigger.current?.focus({ preventScroll: true });
  }, [selection]);

  useEffect(() => {
    if (!contextCollapsed && isNarrow && !selection) contextHeading.current?.focus({ preventScroll: true });
    else if (contextCollapsed && contextWasOpen.current && isNarrow && !selection) contextTrigger.current?.focus({ preventScroll: true });
    contextWasOpen.current = !contextCollapsed;
  }, [contextCollapsed, isNarrow, selection]);

  const closeDetail = useCallback(() => {
    if (selection?.kind === "thread") onSelectThread?.(null);
    setSelection(null);
  }, [onSelectThread, selection?.kind]);

  const closeOverlay = useCallback(() => {
    if (showDetail && !isNarrow && window.matchMedia(overlayQuery).matches) closeDetail();
  }, [closeDetail, isNarrow, showDetail]);

  useEffect(() => {
    if (!showDetail) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") closeOverlay(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeOverlay, showDetail]);

  function openDetail(kind: "thread" | "context", targetId: string, button: HTMLButtonElement, fromThread = false) {
    if (!fromThread) trigger.current = button;
    if (kind === "thread") onSelectThread?.(targetId);
    else if (selection?.kind === "thread") onSelectThread?.(null);
    setSelection({ kind, id: targetId });
  }

  function renderMessage(message: RoomMessage, inThread = false) {
    const participant = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id);
    const name = participant?.displayName || message.author.displayName || "Unknown author";
    const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
    const replyCount = inThread ? 0 : messages.filter((candidate) => candidate.threadRootMessageId === message.messageId).length;
    return <article key={message.messageId} className="room-message" aria-label={`Message from ${name}`}>
      {participant ? <ParticipantAvatar participant={participant} /> : <Identity name={name} avatarOnly size="default" />}
      <div className="room-message-content">
        <div className="room-message-byline">{participant && onSelectParticipant && !(participant.kind === "human" && participant.id === currentUserId)
          ? <button type="button" className="room-author font-semibold truncate" title={name} disabled={!actionable} onClick={() => onSelectParticipant(participant)}>{name}</button>
          : <span className="font-semibold truncate" title={name}>{name}</span>}
          {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
          {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}
        </div>
        <MarkdownView skipHtml components={roomMarkdown}>{message.text}</MarkdownView>
        {message.run && <div className="room-run"><WorkStatus state={message.run.state} />
          {message.run.details && <details><summary>Run details</summary><pre>{message.run.details.slice(0, 8192)}</pre>{message.run.details.length > 8192 && <p className="text-sm text-muted-foreground">Further details omitted.</p>}</details>}
        </div>}
        {message.contextIds?.map((contextId) => {
          const context = contexts.find((record) => record.id === contextId);
          return context && <Button key={contextId} type="button" variant="outline" className="room-work-link" disabled={!actionable}
            onClick={(event) => openDetail("context", context.id, event.currentTarget, inThread)}><FileText size={16} aria-hidden="true" />{context.title}</Button>;
        })}
        {!inThread && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
          aria-label={`Reply in thread${replyCount > 0 ? `, ${replyCount} ${replyCount === 1 ? "reply" : "replies"}` : ""}`}
          onClick={(event) => openDetail("thread", message.messageId, event.currentTarget)}><MessageCircle aria-hidden="true" />{replyCount > 0 ? `${replyCount} ${replyCount === 1 ? "reply" : "replies"} · Open thread` : "Reply in thread"}</Button>}
      </div>
    </article>;
  }

  const RoomIcon = visibleRoom?.kind === "channel" ? Hash : visibleRoom?.kind === "group-dm" ? Users : MessageCircle;
  const counterparts = participants.filter((participant) => participant.kind !== "human" || participant.id !== currentUserId);
  const roomName = roomDisplayName(visibleRoom, counterparts);
  return <section className={cn("room-panel", showContext && "room-has-context", showDetail ? "room-has-detail" : "room-has-default-context")} aria-label={roomName}>
    <div className="room-conversation" hidden={showContext && isNarrow} onClickCapture={closeOverlay} onFocusCapture={closeOverlay}>
      <header className="room-header">
        <div className="room-title">{visibleRoom?.kind === "dm" && counterparts.length === 1
          ? <ParticipantAvatar participant={counterparts[0]} size="sm" />
          : <RoomIcon size={20} aria-hidden="true" />}<h2 className="font-semibold text-lg truncate" title={roomName}>{roomName}</h2></div>
        <div className="room-audience">
          <Button ref={memberTrigger} type="button" variant="ghost" className="room-members-trigger" aria-label="View members" aria-expanded={membersOpen} aria-controls={`${id}-members`}
            onClick={() => setMembersOpen(!membersOpen)}><Users aria-hidden="true" />{participants.length}</Button>
        </div>
      </header>
      <nav className="room-tabs" aria-label="Conversation views">
        <button type="button" className="room-tab" aria-current={contextCollapsed && !showDetail ? "page" : undefined} onClick={() => { closeDetail(); setContextCollapsed(true); }}>{visibleRoom?.kind === "channel" ? "Channel" : "Conversation"}</button>
        <button ref={contextTrigger} type="button" className="room-tab" aria-label={contextCollapsed ? "Show room details" : "Hide room details"} aria-expanded={!contextCollapsed} aria-current={!contextCollapsed && !showDetail ? "page" : undefined}
          onClick={() => { closeDetail(); setContextCollapsed(!contextCollapsed); }}>Context</button>
        {onLinkToWork && <button type="button" className="room-tab" aria-label="Link to work" onClick={onLinkToWork}><Link2 size={14} aria-hidden="true" />Tasks</button>}
      </nav>
      {membersOpen && <section id={`${id}-members`} aria-label="Room members" className="room-members"
        onKeyDown={(event) => { if (event.key === "Escape") { setMembersOpen(false); memberTrigger.current?.focus(); } }}>
        <h3 className="font-semibold">Room members</h3><ul>{participants.map((participant) => <li key={`${participant.kind}:${participant.id}`} className="room-member">
          <ParticipantAvatar participant={participant} size="sm" /><span className="truncate flex-1" title={participant.displayName}>{participant.displayName}</span>
          {participant.kind === "agent" ? <><span className="text-sm text-muted-foreground">{participant.mentionHandle ? `@${participant.mentionHandle}` : "Agent"}</span>
            {onListenModeChange && <label className="sr-only" htmlFor={`${id}-${participant.id}-mode`}>Listen mode for {participant.displayName}</label>}
            {onListenModeChange && <select id={`${id}-${participant.id}-mode`} className="room-listen-mode" value={participant.listenMode ?? "mentions"}
              onChange={(event) => onListenModeChange(participant.id, event.target.value as ListenMode)}><option value="active">Active</option><option value="mentions">Mentions only</option><option value="observe">Observe</option></select>}
            </> : <span className="text-sm text-muted-foreground">Person</span>}
        </li>)}</ul>
      </section>}
      {(state === "reconnecting" || !liveConnected) && <div className="room-state"><p role="status" aria-label="Room status">Reconnecting live updates. You can keep sending messages.</p>
        {onRetry && <Button variant="outline" onClick={onRetry}>Check for updates</Button>}</div>}
      <div role="region" aria-label="Messages" className="room-history" tabIndex={0}>
        {mainMessages.length === 0 && pendingMessages.length === 0
          ? <div className="room-empty"><h3 className="font-semibold text-lg">Start the conversation</h3><p className="text-sm text-muted-foreground">Share an update or mention an agent when you want help.</p></div>
          : <>{mainMessages.map((message) => renderMessage(message))}
            {pendingMessages.filter((message) => message.threadRootMessageId === null).map((message) => <article key={message.clientMessageId} className="room-message" aria-label={`Pending message from ${viewer?.displayName ?? "You"}`}>
              <Identity name={viewer?.displayName ?? "You"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{viewer?.displayName ?? "You"}</span><span className="text-sm text-muted-foreground">Sending…</span></div><MarkdownView skipHtml components={roomMarkdown}>{message.text}</MarkdownView></div>
            </article>)}</>}
        {runs.map((run) => {
          const agent = participants.find((participant) => participant.kind === "agent" && participant.id === run.agentId);
          return <article key={`${run.roomId}:${run.agentId}:${run.runId}`} className="room-message room-live-run" aria-label={`Live run from ${agent?.displayName ?? "Agent"}`}>
            <Identity name={agent?.displayName ?? "Agent"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{agent?.displayName ?? "Agent"}</span><WorkStatus state={run.state === "running" ? "working" : run.state === "completed" ? "completed" : run.state === "aborted" ? "paused" : "failed"} /></div>
              {run.thinking && <details><summary>Thinking</summary><p className="room-wrap text-muted-foreground">{run.thinking}</p></details>}
              {run.text && <MarkdownView skipHtml components={roomMarkdown}>{run.text}</MarkdownView>}
              {run.tools.length > 0 && <ToolActivityTrace calls={run.tools} />}
              {run.state === "running" && (onAbortRun || onSteerRun) && <form className="room-run-controls" onSubmit={(event) => {
                event.preventDefault();
                if (!onSteerRun) return;
                const input = event.currentTarget.elements.namedItem("steer") as HTMLInputElement | null;
                const text = input?.value.trim();
                if (!text) return;
                onSteerRun(run.runId, text);
                event.currentTarget.reset();
              }}>
                {onSteerRun && <><label className="sr-only" htmlFor={`${id}-${run.runId}-steer`}>Steer {agent?.displayName ?? "agent"}</label>
                  <input id={`${id}-${run.runId}-steer`} name="steer" className="room-input" placeholder="Steer this run…" /></>}
                {onSteerRun && <Button type="submit" variant="outline" disabled={!actionable}>Steer</Button>}
                {onAbortRun && <Button type="button" variant="outline" disabled={!actionable} onClick={() => onAbortRun(run.runId)}>Stop</Button>}
              </form>}
            </div>
          </article>;
        })}
      </div>
      <p className="room-typing text-sm text-muted-foreground" role="status">{typingMembers.length > 0 ? `${typingMembers.slice(0, 3).map((member) => member.displayName).join(", ")}${typingMembers.length > 3 ? " and others" : ""} ${typingMembers.length === 1 ? "is" : "are"} typing…` : ""}</p>
      <RoomComposer {...composer} disabled={!actionable || composer.disabled} />
    </div>
    {showContext && !showDetail && <aside className="room-context" aria-label="Room details">
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={() => setContextCollapsed(true)}><ArrowLeft aria-hidden="true" /></Button><h3 ref={contextHeading} tabIndex={-1} className="font-semibold">Room details</h3></header>
      <div className="room-context-body">
        <section aria-labelledby={`${id}-context-members`}>
          <div className="room-members-heading"><h4 id={`${id}-context-members`} className="font-semibold">Members</h4>
            {canAddMembers && <Button type="button" variant="outline" aria-expanded={memberPickerOpen} onClick={() => setMemberPickerOpen(!memberPickerOpen)}>Add members</Button>}
          </div>
          <ul>{participants.map((participant) => <li key={`${participant.kind}:${participant.id}`} className="room-member">
            <ParticipantAvatar participant={participant} size="sm" />
            <span className="truncate flex-1" title={participant.displayName}>{participant.displayName}</span>
            <span className="text-sm text-muted-foreground">{participant.kind === "agent" ? "Agent" : "Person"}</span>
            {participant.kind === "agent" && onListenModeChange && <>
              <label className="sr-only" htmlFor={`${id}-context-${participant.id}-mode`}>Listen mode for {participant.displayName}</label>
              <select id={`${id}-context-${participant.id}-mode`} className="room-listen-mode" value={participant.listenMode ?? "mentions"}
                onChange={(event) => onListenModeChange(participant.id, event.target.value as ListenMode)}><option value="active">Active</option><option value="mentions">Mentions only</option><option value="observe">Observe</option></select>
            </>}
          </li>)}</ul>
          {canAddMembers && memberPickerOpen && <div className="room-member-picker">
            {addMemberCandidates.length === 0 ? <p className="text-sm text-muted-foreground">Everyone in your workspace is already here.</p>
              : <><fieldset disabled={addingMembers}><legend className="sr-only">Choose members to add</legend>
                {addMemberCandidates.map((candidate) => {
                  const key = `${candidate.kind}:${candidate.id}`;
                  return <label key={key} className="room-member-choice"><input type="checkbox" aria-label={candidate.displayName} checked={selectedMembers.includes(key)}
                    onChange={(event) => setSelectedMembers((current) => event.target.checked ? [...current, key] : current.filter((item) => item !== key))} />
                    <ParticipantAvatar participant={candidate} size="sm" /><span>{candidate.displayName}</span><span className="text-sm text-muted-foreground">{candidate.kind === "agent" ? "Agent" : "Person"}</span></label>;
                })}
              </fieldset>
              <Button type="button" disabled={selectedMembers.length === 0 || addingMembers} onClick={() => void addSelectedMembers()}>{addingMembers ? "Adding…" : "Add selected"}</Button></>}
            {memberError && <p role="alert" className="text-sm text-err">{memberError}</p>}
          </div>}
        </section>
        <section aria-labelledby={`${id}-context-runs`}>
          <h4 id={`${id}-context-runs`} className="font-semibold">Active work</h4>
          {activeRuns.length === 0 ? <p className="text-sm text-muted-foreground">No active runs.</p>
            : activeRuns.map((run) => <p key={run.runId} className="room-wrap">{participants.find((participant) => participant.kind === "agent" && participant.id === run.agentId)?.displayName ?? "Agent"} is working</p>)}
        </section>
      </div>
    </aside>}
    {showDetail && <aside className="room-context" aria-label={parentMessage ? "Thread" : selectedContext!.title} onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold room-wrap">{parentMessage ? "Thread" : selectedContext!.title}</h3></header>
      <div className="room-context-body">{parentMessage ? <>{renderMessage(parentMessage, true)}
        <div className="room-thread-replies" aria-label="Thread replies">{messages.filter((message) => message.threadRootMessageId === parentMessage.messageId).map((message) => renderMessage(message, true))}</div>
      </> : <><p className="text-sm text-muted-foreground">{selectedContext!.kind.charAt(0).toUpperCase() + selectedContext!.kind.slice(1)}</p>
        {selectedContext!.status && <WorkStatus state={selectedContext!.status} />}<div className="room-context-record">{selectedContext!.content}</div>
        {selectedContext!.onAction && selectedContext!.actionLabel && <Button disabled={!actionable || selectedContext!.busy} aria-busy={selectedContext!.busy} onClick={selectedContext!.onAction}>{selectedContext!.actionLabel}</Button>}
      </>}</div>
      {parentMessage && threadComposer?.parentMessageId === parentMessage.messageId && <RoomComposer {...threadComposer.composer} label="Reply to thread" disabled={!actionable || threadComposer.composer.disabled} />}
    </aside>}
  </section>;
}
