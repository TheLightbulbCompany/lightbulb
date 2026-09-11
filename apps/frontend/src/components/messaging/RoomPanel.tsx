"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ArrowLeft, Check, Circle, CircleAlert, Hash, Link, Link2, MessageCircle, Users } from "lucide-react";
import type { Components } from "react-markdown";
import type { CreateMessageRequest, ListenMode, MemberRef, UpdateMessageRequest } from "@lightbulb/shared";
import { ToolActivityTrace } from "@/components/chat/ToolActivityTrace";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { cn } from "@/lib/utils";
import { roomUrlTransform } from "./markdown";
import { copyText, messagePermalink } from "./messageLinks";
import { RoomComposer, type RoomComposerProps } from "./RoomComposer";
import { linkifyMentions, parseMentionHref } from "./mentions";
import { readRecentEmoji } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomMessageItem } from "./RoomMessageItem";
import { RoomMessageSearch } from "./RoomMessageSearch";
import { RoomTimeline } from "./RoomTimeline";
import { buildTimeline } from "./timeline";
import { roomDisplayName } from "./types";
import type {
  PendingRoomMessage,
  DirectoryParticipant,
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
  readSeqAtOpen?: number | null;
  targetMessageId?: string | null;
  state: RoomState;
  httpReady: boolean;
  liveConnected: boolean;
  participants: readonly RoomParticipant[];
  messages: readonly RoomMessage[];
  historyLoading?: boolean;
  historyComplete?: boolean;
  historyError?: boolean;
  onLoadHistory?: () => void;
  pendingMessages?: readonly PendingRoomMessage[];
  typingMembers?: readonly { userId: string; displayName: string }[];
  runs?: readonly RoomRunView[];
  composer: RoomComposerProps;
  threadComposer?: { parentMessageId: string; composer: RoomComposerProps };
  onSelectThread?: (parentMessageId: string | null) => void;
  selectedThreadId?: string | null;
  onSelectParticipant?: (participant: RoomParticipant) => void | Promise<void>;
  onRetry?: () => void;
  onListenModeChange?: (agentId: string, mode: ListenMode) => void;
  onSetReaction?: (messageId: string, emoji: string, active: boolean) => Promise<void>;
  onUpdateMessage?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  addMemberCandidates?: readonly DirectoryParticipant[];
  onAddMember?: (member: MemberRef) => Promise<void>;
  onRenameRoom?: (name: string) => Promise<void>;
  onRemoveMember?: (member: MemberRef) => Promise<void>;
  onAbortRun?: (runId: string) => void;
  onSteerRun?: (runId: string, text: string) => void;
  onLinkToWork?: () => void;
  onDownloadAttachment?: (roomId: string, attachmentId: string) => Promise<Blob>;
  narrow?: boolean;
}

const DEFAULT_QUICK_REACTIONS = ["👍", "✅", "🎉", "👀"];

const workLabel: Record<WorkState, string> = {
  ready: "Ready", queued: "Queued", working: "Working", paused: "Paused",
  "needs-approval": "Needs approval", completed: "Completed", failed: "Failed", offline: "Offline", unknown: "Unknown",
};
const responseLabel: Record<ListenMode, string> = {
  active: "Automatic", mentions: "Mentions only", observe: "Observe",
};

function AgentResponse({ participant, selectId, onChange }: {
  participant: RoomParticipant;
  selectId: string;
  onChange?: (agentId: string, mode: ListenMode) => void;
}) {
  const mode = participant.listenMode ?? "mentions";
  if (!onChange) return <span className="text-sm text-muted-foreground">Response: {responseLabel[mode]}</span>;
  return <><span className="text-sm text-muted-foreground">Response</span><select id={selectId} aria-label={`Response for ${participant.displayName}`} className="room-listen-mode" value={mode}
    onChange={(event) => onChange(participant.id, event.target.value as ListenMode)}><option value="active">Automatic</option><option value="mentions">Mentions only</option><option value="observe">Observe</option></select></>;
}

function WorkStatus({ state }: { state: WorkState }) {
  const tone = state === "completed" ? "text-ok" : state === "failed" ? "text-err"
    : state === "working" || state === "queued" ? "text-info" : state === "needs-approval" || state === "paused" ? "text-warn" : "text-muted-foreground";
  const Icon = state === "completed" ? Check : state === "failed" || state === "needs-approval" ? CircleAlert : Circle;
  return <span role="status" aria-label="Work status" className={`inline-flex items-center gap-1 text-sm ${tone}`}><Icon size={14} aria-hidden="true" />{workLabel[state]}</span>;
}

const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    const mention = href ? parseMentionHref(href) : null;
    if (mention) return <span className="room-mention" data-kind={mention.kind}>{children}</span>;
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
      } catch (error) {
        setAttempts((current) => {
          const latest = current.get(key);
          if (!latest || latest.revision !== revision || latest.state === "persisted" || latest.state === "rejected") return current;
          return new Map(current).set(key, { ...latest, state: "uncertain" });
        });
        throw error;
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
    (props.pendingMessages ?? []).every((message) => message.roomId === props.roomId);
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
  readSeqAtOpen,
  targetMessageId,
  state,
  httpReady,
  liveConnected,
  participants,
  messages,
  historyLoading,
  historyComplete,
  historyError,
  onLoadHistory,
  pendingMessages = [],
  typingMembers = [],
  runs = [],
  composer,
  threadComposer,
  onSelectThread,
  selectedThreadId,
  onSelectParticipant,
  onRetry,
  onListenModeChange,
  onSetReaction,
  onUpdateMessage,
  addMemberCandidates = [],
  onAddMember,
  onRenameRoom,
  onRemoveMember,
  onAbortRun,
  onSteerRun,
  onLinkToWork,
  onDownloadAttachment,
  narrow,
}: RoomPanelProps) {
  const id = useId();
  // token increments on every selection (even of the same result) so RoomTimeline
  // re-scrolls/re-highlights instead of treating a repeat click as already-handled.
  const searchTargetToken = useRef(0);
  const [searchTarget, setSearchTarget] = useState<(Pick<RoomMessage, "messageId" | "threadRootMessageId"> & { token: number }) | null>(null);
  // RoomContent remounts per room (RoomBoundary is keyed by roomId), so this resets naturally on room change.
  const [sentSinceOpen, setSentSinceOpen] = useState(false);
  // Only suppress the "New" line once a send actually succeeds -- a rejected send must leave sentSinceOpen false.
  const markSent = (callback: RoomComposerProps["onSubmit"]) => async (request: CreateMessageRequest) => {
    await callback(request);
    setSentSinceOpen(true);
  };
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [reactionPending, setReactionPending] = useState<string | null>(null);
  const [reactionError, setReactionError] = useState<string | null>(null);
  // Computed once per mount/reaction-change (not per message render) -- reading
  // localStorage in every RoomMessageItem render was wasted work on a long list.
  const [quickReactions, setQuickReactions] = useState<readonly string[]>(() => {
    const recent = readRecentEmoji();
    return recent.length ? recent.slice(0, 4) : DEFAULT_QUICK_REACTIONS;
  });
  const [membersOpen, setMembersOpen] = useState(false);
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [addingMembers, setAddingMembers] = useState(false);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [memberError, setMemberError] = useState<string>();
  const [management, setManagement] = useState<"rename" | MemberRef | null>(null);
  const [roomNameDraft, setRoomNameDraft] = useState("");
  const [managementPending, setManagementPending] = useState(false);
  const [managementError, setManagementError] = useState<string>();
  const managementTrigger = useRef<HTMLButtonElement | null>(null);
  const managementCancel = useRef<HTMLButtonElement>(null);
  const [contextCollapsed, setContextCollapsed] = useState(true);
  const [localSelection, setSelection] = useState<{ kind: "thread"; id: string } | null>(null);
  const selection = useMemo(() => selectedThreadId === undefined ? localSelection
    : selectedThreadId ? { kind: "thread" as const, id: selectedThreadId }
      : null, [selectedThreadId, localSelection]);
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
  const newLineSeq = readSeqAtOpen ?? null;
  const ownSeqs = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId).map((message) => message.messageSeq);
  const ownLastSentSeq = ownSeqs.length ? Math.max(...ownSeqs) : null;
  // Only a main-list (non-thread) search result with no thread open is a target
  // for RoomTimeline -- a thread-scoped result is handled by the effect below.
  const activeSearchTarget = searchTarget && !searchTarget.threadRootMessageId && !selection ? searchTarget : null;
  const parentMessage = selection?.kind === "thread" ? messages.find((message) => message.messageId === selection.id) : undefined;
  const showDetail = !!parentMessage;
  const showContext = showDetail || !contextCollapsed;
  const activeRuns = runs.filter((run) => run.state === "running");
  const viewer = participants.find((participant) => participant.kind === "human" && participant.id === currentUserId);
  const canAddMembers = visibleRoom?.kind === "channel" && !!onAddMember;
  const isRoomAdmin = viewer?.role === "owner" || viewer?.role === "admin";
  const canRename = visibleRoom?.kind !== "dm" && isRoomAdmin;
  const canManageMembers = visibleRoom?.kind === "channel" && isRoomAdmin;
  const removalTarget = management && management !== "rename"
    ? participants.find((member) => member.kind === management.kind && member.id === management.id && member.role !== "owner")
    : undefined;

  function openManagement(action: "rename" | MemberRef, button: HTMLButtonElement) {
    managementTrigger.current = button;
    setRoomNameDraft(visibleRoom?.name ?? "");
    setManagementError(undefined);
    setManagement(action);
  }

  async function saveManagement() {
    if (!actionable || managementPending) return;
    if (management === "rename" ? !canRename || !onRenameRoom || !roomNameDraft.trim()
      : !canManageMembers || !onRemoveMember || !removalTarget) return;
    setManagementPending(true);
    setManagementError(undefined);
    try {
      if (management === "rename") await onRenameRoom!(roomNameDraft.trim());
      else await onRemoveMember!({ kind: removalTarget!.kind, id: removalTarget!.id });
      setManagement(null);
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "The channel could not be updated. Try again.");
    } finally {
      setManagementPending(false);
    }
  }

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
    if (!searchTarget?.threadRootMessageId) return;
    if (selection?.kind !== "thread" || selection.id !== searchTarget.threadRootMessageId) return;
    const target = document.getElementById(`${id}-thread-${searchTarget.messageId}`);
    target?.scrollIntoView({ block: "center" });
    target?.focus({ preventScroll: true });
  }, [id, searchTarget, selection]);

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

  function openDetail(kind: "thread", targetId: string, button: HTMLButtonElement, fromThread = false) {
    if (!fromThread) trigger.current = button;
    onSelectThread?.(targetId);
    setSelection({ kind, id: targetId });
  }

  function renderMessage(message: RoomMessage, inThread = false, collapsed = false) {
    const participant = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id);
    const mentionsMe = message.mentions.some((mention) => mention.kind === "human" && mention.id === currentUserId);
    return <RoomMessageItem key={message.messageId} id={`${id}-${inThread ? "thread" : "main"}-${message.messageId}`}
      message={message} text={linkifyMentions(message.text, message.mentions, participants)} participant={participant} currentUserId={currentUserId} actionable={actionable} inThread={inThread} collapsed={collapsed}
      replyCount={inThread ? 0 : messages.filter((candidate) => candidate.threadRootMessageId === message.messageId).length}
      highlighted={inThread && searchTarget?.messageId === message.messageId} mentionsViewer={mentionsMe} markdown={roomMarkdown} urlTransform={roomUrlTransform} workStatus={(state) => <WorkStatus state={state} />}
      quickReactions={quickReactions} reactionPending={reactionPending !== null} reactionError={reactionError === message.messageId}
      onToggleReaction={onSetReaction && !message.deletedAt ? (emoji) => void toggleReaction(message, emoji) : undefined}
      onOpenThread={(button) => openDetail("thread", message.messageId, button)}
      onSelectParticipant={onSelectParticipant} onUpdateMessage={onUpdateMessage} onDownloadAttachment={onDownloadAttachment}
      toolbarExtras={!message.deletedAt ? <>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Copy link"
          title={copied === message.messageId ? "Copied" : "Copy link"}
          onClick={async () => {
            const ok = await copyText(messagePermalink(roomId, message.messageId));
            setCopied(ok ? message.messageId : null);
            setCopyError(ok ? null : message.messageId);
            if (ok) setTimeout(() => setCopied((current) => current === message.messageId ? null : current), 2000);
          }}><Link aria-hidden="true" /></Button>
        {copyError === message.messageId && <p role="alert" className="text-sm text-err">Could not copy the link.</p>}
      </> : undefined} />;
  }
  async function toggleReaction(message: RoomMessage, emoji: string) {
    if (!onSetReaction || !actionable || reactionPending !== null) return;
    setReactionPending(message.messageId);
    setReactionError(null);
    try { await onSetReaction(message.messageId, emoji, !message.reactions?.[emoji]?.includes(currentUserId)); }
    catch { setReactionError(message.messageId); }
    finally {
      setReactionPending(null);
      const recent = readRecentEmoji();
      setQuickReactions(recent.length ? recent.slice(0, 4) : DEFAULT_QUICK_REACTIONS);
    }
  }

  function renderRun(run: RoomRunView) {
    const agent = participants.find((participant) => participant.kind === "agent" && participant.id === run.agentId);
    const agentName = agent?.displayName ?? "Agent";
    if (run.state !== "running" && !run.text) {
      const label = run.state === "completed" ? `${agentName} finished · no reply` : run.state === "aborted" ? `${agentName} stopped` : `${agentName}'s run failed`;
      return <div key={`${run.roomId}:${run.agentId}:${run.runId}`} className={cn("room-run-line", run.state === "failed" ? "text-err" : "text-muted-foreground")} role="status">
        <Circle size={12} aria-hidden="true" />{label}
        {run.tools.length > 0 && <details><summary>What it did</summary><ToolActivityTrace calls={run.tools} /></details>}
      </div>;
    }
    const workState = run.state === "running" ? "working" : run.state === "aborted" ? "paused" : run.state;
    return <article key={`${run.roomId}:${run.agentId}:${run.runId}`} className="room-message room-live-run" aria-label={`Live run from ${agentName}`}>
      <Identity name={agentName} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{agentName}</span><WorkStatus state={workState} /></div>
        {run.thinking && <details><summary>Thinking</summary><p className="room-wrap text-muted-foreground">{run.thinking}</p></details>}
        {run.text && <MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{run.text}</MarkdownView>}
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
          <RoomMessageSearch roomId={roomId} messages={messages} participants={participants}
            loading={historyLoading} complete={historyComplete} error={historyError} onLoad={onLoadHistory}
            onSelect={(message) => {
              const root = message.threadRootMessageId;
              // Otherwise the [selection] effect's else-branch focuses this stale trigger
              // (e.g. a "Reply in thread" button) once selection clears below, racing
              // RoomTimeline's own focus of the newly targeted main message.
              trigger.current = null;
              onSelectThread?.(root);
              setSelection(root ? { kind: "thread", id: root } : null);
              setContextCollapsed(true);
              setSearchTarget({ messageId: message.messageId, threadRootMessageId: root, token: ++searchTargetToken.current });
            }} />
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
          <ParticipantAvatar participant={participant} size="sm" /><ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} className="flex-1" />
          {participant.kind === "agent" ? <><span className="text-sm text-muted-foreground">{participant.mentionHandle ? `@${participant.mentionHandle}` : "Agent"}</span>
            <AgentResponse participant={participant} selectId={`${id}-${participant.id}-mode`} onChange={onListenModeChange} />
            </> : <span className="text-sm text-muted-foreground">Person</span>}
        </li>)}</ul>
      </section>}
      {(state === "reconnecting" || !liveConnected) && <div className="room-state"><p role="status" aria-label="Room status">Reconnecting live updates. You can keep sending messages.</p>
        {onRetry && <Button variant="outline" onClick={onRetry}>Check for updates</Button>}</div>}
      <RoomTimeline roomId={roomId} messages={mainMessages} readSeq={sentSinceOpen ? null : newLineSeq} ownLastSentSeq={ownLastSentSeq}
        targetMessageId={targetMessageId ?? (activeSearchTarget ? activeSearchTarget.messageId : null)}
        targetToken={targetMessageId ? undefined : activeSearchTarget?.token}
        renderMessage={(message, collapsed) => renderMessage(message, false, collapsed)}
        empty={pendingMessages.length ? null : <div className="room-empty"><h3 className="font-semibold text-lg">Start the conversation</h3><p className="text-sm text-muted-foreground">Share an update or mention an agent when you want help.</p></div>}>
        {pendingMessages.filter((message) => message.threadRootMessageId === null).map((message) => <article key={message.clientMessageId} className="room-message" aria-label={`Pending message from ${viewer?.displayName ?? "You"}`}>
          <Identity name={viewer?.displayName ?? "You"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{viewer?.displayName ?? "You"}</span><span className="text-sm text-muted-foreground">Sending…</span></div><MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{linkifyMentions(message.text, message.mentions, participants)}</MarkdownView></div>
        </article>)}
        {runs.filter((run) => !run.threadRootMessageId).map(renderRun)}
      </RoomTimeline>
      <p className="room-typing text-sm text-muted-foreground" role="status">{typingMembers.length > 0 ? `${typingMembers.slice(0, 3).map((member) => member.displayName).join(", ")}${typingMembers.length > 3 ? " and others" : ""} ${typingMembers.length === 1 ? "is" : "are"} typing…` : ""}</p>
      {copied && <p role="status" aria-label="Link copied" className="sr-only">Link copied</p>}
      <RoomComposer {...composer}
        onSubmit={markSent(composer.onSubmit)}
        onRetry={composer.onRetry ? markSent(composer.onRetry) : undefined}
        disabled={!actionable || composer.disabled} />
    </div>
    {showContext && !showDetail && <aside className="room-context" aria-label="Room details">
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={() => setContextCollapsed(true)}><ArrowLeft aria-hidden="true" /></Button><h3 ref={contextHeading} tabIndex={-1} className="font-semibold">Room details</h3></header>
      <div className="room-context-body">
        {canRename && onRenameRoom && <section aria-labelledby={`${id}-room-name`}>
          <div className="room-members-heading"><h4 id={`${id}-room-name`} className="font-semibold">{visibleRoom?.kind === "channel" ? "Channel" : "Group chat"} name</h4>
            <Button type="button" variant="outline" disabled={!actionable} onClick={(event) => openManagement("rename", event.currentTarget)}>Rename {visibleRoom?.kind === "channel" ? "channel" : "group chat"}</Button>
          </div><p className="room-wrap text-sm text-muted-foreground">{roomName}</p>
        </section>}
        <section aria-labelledby={`${id}-context-members`}>
          <div className="room-members-heading"><h4 id={`${id}-context-members`} className="font-semibold">Members</h4>
            {canAddMembers && <Button type="button" variant="outline" aria-expanded={memberPickerOpen} onClick={() => {
              if (!memberPickerOpen) setMemberError(undefined);
              setMemberPickerOpen(!memberPickerOpen);
            }}>Add members</Button>}
          </div>
          <ul>{participants.map((participant) => <li key={`${participant.kind}:${participant.id}`} className="room-member flex-wrap">
            <ParticipantAvatar participant={participant} size="sm" />
            <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} className="flex-1 min-w-24" />
            <span className="text-sm text-muted-foreground">{participant.kind === "agent" ? "Agent" : "Person"}</span>
            {canManageMembers && onRemoveMember && participant.role !== "owner" && <Button type="button" variant="ghost" size="sm" disabled={!actionable}
              aria-label={`Remove ${participant.displayName}`} onClick={(event) => openManagement({ kind: participant.kind, id: participant.id }, event.currentTarget)}>Remove</Button>}
            {participant.kind === "agent" && <AgentResponse participant={participant} selectId={`${id}-context-${participant.id}-mode`} onChange={onListenModeChange} />}
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
    {showDetail && <aside className="room-context" aria-label="Thread" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold room-wrap">Thread</h3></header>
      <div className="room-context-body">{parentMessage ? <>{renderMessage(parentMessage, true)}
        <div className="room-thread-replies" aria-label="Thread replies">{buildTimeline(messages.filter((message) => message.threadRootMessageId === parentMessage.messageId), { readSeq: null }).map((item) => item.kind === "message" ? renderMessage(item.message, true, item.collapsed) : null)}
          {runs.filter((run) => run.threadRootMessageId === parentMessage.messageId).map(renderRun)}
        </div>
      </> : null}</div>
      {parentMessage && threadComposer?.parentMessageId === parentMessage.messageId && <RoomComposer {...threadComposer.composer} label="Reply to thread" disabled={!actionable || threadComposer.composer.disabled} />}
    </aside>}
    <Dialog open={management !== null && (management === "rename" ? canRename : canManageMembers)} onOpenChange={(open) => { if (!open && !managementPending) setManagement(null); }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { if (management !== "rename") { event.preventDefault(); managementCancel.current?.focus(); } }}
        onCloseAutoFocus={(event) => { event.preventDefault(); (managementTrigger.current?.isConnected ? managementTrigger.current : contextHeading.current)?.focus(); }}>
        <DialogHeader><DialogTitle>{management === "rename" ? `Rename ${visibleRoom?.kind === "channel" ? "channel" : "group chat"}` : "Remove member"}</DialogTitle>
          <DialogDescription className="break-words">{management === "rename" ? `Choose a name that helps your team find this ${visibleRoom?.kind === "channel" ? "channel" : "group chat"}.`
            : `Remove ${removalTarget?.displayName ?? "this member"} from ${roomName}? They will lose access to this channel and its messages.`}</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); void saveManagement(); }}>
          {management === "rename" && <div className="grid gap-2"><label htmlFor={`${id}-rename`} className="text-sm font-medium">{visibleRoom?.kind === "channel" ? "Channel" : "Group chat"} name</label>
            <input id={`${id}-rename`} className="room-input" value={roomNameDraft} maxLength={200} required disabled={managementPending}
              onChange={(event) => setRoomNameDraft(event.target.value)} /></div>}
          {managementError && <p role="alert" className="text-sm text-err break-words">{managementError}</p>}
          <DialogFooter className="gap-2"><Button ref={managementCancel} type="button" variant="outline" disabled={managementPending} onClick={() => setManagement(null)}>Cancel</Button>
            <Button type="submit" variant={management === "rename" ? "default" : "destructive"}
              disabled={!actionable || managementPending || (management === "rename" ? !roomNameDraft.trim() : !removalTarget)}>
              {managementPending ? (management === "rename" ? "Saving…" : "Removing…") : management === "rename" ? "Save name" : "Remove member"}
            </Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </section>;
}
