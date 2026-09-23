"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import NextLink from "next/link";
import { ArrowLeft, Check, Circle, CircleAlert, Link } from "lucide-react";
import type { Components } from "react-markdown";
import { LISTEN_MODE_LABELS, listenModeOptions, type CreateMessageRequest, type ListenMode, type MemberPrefsRequest, type MemberRef, type RoomResourceSource, type UpdateMessageRequest } from "@lightbulb/shared";
import { ToolActivityTrace } from "@/components/chat/ToolActivityTrace";
import { QuietDisclosure } from "@/components/chat/QuietDisclosure";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { RoomResources } from "./RoomResources";
import { ColumnGrip } from "./ColumnGrip";
import { useSessionColumnWidth } from "./sidebarPrefs";
import { roomMarkdown as publicMarkdown } from "./roomMarkdown";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { roomUrlTransform } from "./markdown";
import { copyText, messagePermalink, parseSessionPermalink } from "./messageLinks";
import { openSessionPanel } from "@/components/workspace/workspaceController";
import { RoomComposer, type RoomComposerProps } from "./RoomComposer";
import { RoomDetailsPanel, type DetailsTab } from "./RoomDetailsPanel";
import { RoomHeader } from "./RoomHeader";
import { readRecentEmoji } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomIntro } from "./RoomIntro";
import { OnboardingBand } from "./OnboardingBand";
import { RoomMessageItem } from "./RoomMessageItem";
import { RoomMessageSearch } from "./RoomMessageSearch";
import { RoomTasks } from "./RoomTasks";
import { taskIdentifiers, useReferencedTasks } from "./MessageTaskCard";
import { RoomTimeline } from "./RoomTimeline";
import { buildTimeline, linkifyMentions, parseMentionHref, roomDisplayName, type RoomRunView, type RoomSummary } from "@lightbulb/shared";
import { useAsyncAction } from "./useAsyncAction";
import type {
  PendingRoomMessage,
  DirectoryParticipant,
  RoomMessage,
  RoomParticipant,
  RoomState,
  WorkState,
} from "./types";
import "./messaging.css";

export interface RoomPanelProps {
  targetSession?: { sessionId: string; nonce: number } | null;
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
  historyErrorMessage?: string | null;
  onLoadHistory?: () => void;
  onLoadOlder?: () => void;
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
  // Listening is paid (backend `listening_available`): false locks the option and shows a stored Listening as Mentions.
  listeningAvailable?: boolean;
  onSetReaction?: (messageId: string, emoji: string, active: boolean) => Promise<void>;
  onUpdateMessage?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  addMemberCandidates?: readonly DirectoryParticipant[];
  onAddMember?: (member: MemberRef) => Promise<void>;
  onRenameRoom?: (name: string) => Promise<void>;
  onRemoveMember?: (member: MemberRef) => Promise<void>;
  pinnedMessageIds?: readonly string[];
  onSetPin?: (messageId: string, pinned: boolean) => Promise<void>;
  onMarkUnreadFrom?: (fromSeq: number) => Promise<void>;
  onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
  onSetArchived?: (archived: boolean) => Promise<void>;
  onAbortRun?: (runId: string) => void;
  onSteerRun?: (runId: string, text: string) => void;
  onLinkToWork?: () => void;
  onDownloadAttachment?: (roomId: string, attachmentId: string) => Promise<Blob>;
  resourceSource?: RoomResourceSource;
  /** False hides the Devices & sessions tab (a permalink can still open the pane). */
  showResourcesTab?: boolean;
}

const NO_PINNED_MESSAGES: readonly string[] = [];
const NO_TASK_IDS: readonly string[] = [];

const DEFAULT_QUICK_REACTIONS = ["👍", "✅", "🎉", "👀"];

const workLabel: Record<WorkState, string> = {
  ready: "Ready", queued: "Queued", working: "Working", paused: "Paused", stopped: "Stopped",
  "needs-approval": "Needs approval", completed: "Completed", failed: "Failed", offline: "Offline", unknown: "Unknown",
};
function AgentResponse({ participant, selectId, onChange, listeningAvailable }: {
  participant: RoomParticipant;
  selectId: string;
  onChange?: (agentId: string, mode: ListenMode) => void;
  listeningAvailable: boolean;
}) {
  const stored = participant.listenMode ?? "mentions";
  // On the free tier a stored Listening runs as Mentions (the dispatcher's plan gate), so show what it does.
  const mode = stored === "active" && !listeningAvailable ? "mentions" : stored;
  if (!onChange) return <span className="text-sm text-muted-foreground">Response: {LISTEN_MODE_LABELS[mode]}</span>;
  return <><span className="text-sm text-muted-foreground">Response</span><select id={selectId} aria-label={`Response for ${participant.displayName}`} className="room-listen-mode" value={mode}
    onChange={(event) => onChange(participant.id, event.target.value as ListenMode)}>
    {listenModeOptions(listeningAvailable).map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select></>;
}

function WorkStatus({ state }: { state: WorkState }) {
  const tone = state === "completed" ? "text-ok" : state === "failed" ? "text-err"
    : state === "working" || state === "queued" ? "text-info" : state === "needs-approval" || state === "paused" ? "text-warn" : "text-muted-foreground";
  const Icon = state === "completed" ? Check : state === "failed" || state === "needs-approval" ? CircleAlert : Circle;
  return <span role="status" aria-label="Work status" className={`inline-flex items-center gap-1 text-sm ${tone}`}><Icon size={14} aria-hidden="true" />{workLabel[state]}</span>;
}

const roomMarkdown: Partial<Components> = {
  ...publicMarkdown,
  a: ({ href, children }) => {
    const mention = href ? parseMentionHref(href) : null;
    if (mention) return <span className="room-mention" data-kind={mention.kind}>{children}</span>;
    // This overrides publicMarkdown's own `a` (for mention/session-permalink
    // handling), so it needs its own paperclip-link resolution too — see
    // roomMarkdown.tsx for the shared rationale.
    const shellUrl = shellUrlFromPaperclipPath(href);
    if (shellUrl) return <a href={shellUrl} className="text-info underline underline-offset-4" onClick={event => {
      if (shellUrl.split("?")[0] === window.location.pathname) handleShellLinkClick(event, shellUrl);
    }}>{children}</a>;
    const safe = safeHref(href);
    const session = safe && typeof window !== "undefined" ? parseSessionPermalink(safe) : null;
    if (session) return <a href={safe!} className="text-info underline underline-offset-4" onClick={event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openSessionPanel(session.roomId, session.sessionId);
    }}>{children}</a>;
    return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
  },
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
  return <RoomBoundary key={JSON.stringify([props.ownerScopeId, props.currentUserId, props.roomId])} {...props} />;
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
  ownerScopeId,
  roomId,
  currentUserId,
  room,
  readSeqAtOpen,
  targetMessageId,
  targetSession,
  state,
  httpReady,
  liveConnected,
  participants,
  messages,
  historyLoading,
  historyComplete,
  historyError,
  historyErrorMessage,
  onLoadHistory,
  onLoadOlder,
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
  listeningAvailable = true,
  onSetReaction,
  onUpdateMessage,
  addMemberCandidates = [],
  onAddMember,
  onRenameRoom,
  onRemoveMember,
  pinnedMessageIds = NO_PINNED_MESSAGES,
  onSetPin,
  onMarkUnreadFrom,
  onSetPrefs,
  onSetTopic,
  onLeaveRoom,
  onSetArchived,
  onAbortRun,
  onSteerRun,
  onLinkToWork,
  onDownloadAttachment,
  resourceSource,
  showResourcesTab = true,
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
  // Shared pending/error pair for the per-message menu actions (pin, mark unread) --
  // only one such action is ever in flight from a given message's menu at a time.
  const { pending: messageActionPending, error: messageActionError, run: runMessageAction } = useAsyncAction();
  // Computed once per mount/reaction-change (not per message render) -- reading
  // localStorage in every RoomMessageItem render was wasted work on a long list.
  const [quickReactions, setQuickReactions] = useState<readonly string[]>(() => {
    const recent = readRecentEmoji();
    return recent.length ? recent.slice(0, 4) : DEFAULT_QUICK_REACTIONS;
  });
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [addingMembers, setAddingMembers] = useState(false);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [memberError, setMemberError] = useState<string>();
  const [management, setManagement] = useState<MemberRef | null>(null);
  const [managementPending, setManagementPending] = useState(false);
  const [managementError, setManagementError] = useState<string>();
  const managementTrigger = useRef<HTMLButtonElement | null>(null);
  const managementCancel = useRef<HTMLButtonElement>(null);
  const [contextCollapsed, setContextCollapsed] = useState(true);
  const [detailsTab, setDetailsTab] = useState<DetailsTab>("members");
  const [editTarget, setEditTarget] = useState<string | null>(null);
  const [localSelection, setSelection] = useState<{ kind: "thread" | "resources" | "tasks"; id: string } | null>(null);
  const [consumedSession, setConsumedSession] = useState<number | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const column = useSessionColumnWidth(ownerScopeId, panelRef);
  const targetSessionNonce = targetSession?.nonce;
  useEffect(() => {
    if (targetSessionNonce === undefined) return;
    onSelectThread?.(null);
    setSearchTarget(null);
    setSelection({ kind: "resources", id: "resources" });
    // Only a new explicit navigation may reopen resources.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetSessionNonce]);
  const selection = useMemo(() => selectedThreadId === undefined ? localSelection
    : selectedThreadId ? { kind: "thread" as const, id: selectedThreadId }
      : localSelection?.kind !== "thread" ? localSelection : null, [selectedThreadId, localSelection]);
  const loadedThreadHistory = useRef<string | null>(null);
  useEffect(() => {
    if (selection?.kind !== "thread" || historyComplete || historyLoading || !onLoadHistory) return;
    if (loadedThreadHistory.current === selection.id) return;
    loadedThreadHistory.current = selection.id;
    onLoadHistory();
  }, [selection, historyComplete, historyLoading, onLoadHistory]);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const contextHeading = useRef<HTMLHeadingElement>(null);
  const contextTrigger = useRef<HTMLButtonElement>(null);
  const contextWasOpen = useRef(false);
  const [previousMessageTarget, setPreviousMessageTarget] = useState(targetMessageId);
  if (targetMessageId !== previousMessageTarget) {
    setPreviousMessageTarget(targetMessageId);
    if (targetMessageId && selection?.kind === "resources") {
      // Reconcile before children process the target: a hidden timeline must
      // not consume it, and closing must not restore an obsolete trigger.
      trigger.current = null;
      contextWasOpen.current = false;
      setSelection(null);
      setContextCollapsed(true);
    }
  }
  const historyRef = useRef<HTMLDivElement>(null);
  const isNarrow = useSyncExternalStore(subscribeNarrow, getNarrow, getServerNarrow);
  const visibleRoom = room?.roomId === roomId ? room : undefined;
  const actionable = httpReady && !!visibleRoom;
  const mainMessages = useMemo(() => messages.filter((message) => message.threadRootMessageId === null), [messages]);
  // One pass, as in the mobile timeline, instead of a full history scan per row.
  const repliesByRoot = useMemo(() => {
    const replies = new Map<string, { count: number; latest: RoomMessage; authors: Map<string, RoomMessage["author"]> }>();
    for (const message of messages) {
      if (!message.threadRootMessageId) continue;
      const previous = replies.get(message.threadRootMessageId);
      const authors = previous?.authors ?? new Map<string, RoomMessage["author"]>();
      authors.set(`${message.author.kind}:${message.author.id}`, message.author);
      replies.set(message.threadRootMessageId, {
        authors,
        count: (previous?.count ?? 0) + 1,
        latest: previous && previous.latest.messageSeq >= message.messageSeq ? previous.latest : message,
      });
    }
    return replies;
  }, [messages]);
  // Only membership changes invalidate history; streamed text/tool frames do not.
  const workingThreadKey = JSON.stringify(runs.filter(run => run.state === "running" && run.threadRootMessageId)
    .map(run => [run.threadRootMessageId, run.agentId]));
  const threadActivity = useMemo(() => {
    const result = new Map<string, { participants: Map<string, DirectoryParticipant>; working: Set<string> }>();
    const entry = (root: string) => {
      if (!result.has(root)) result.set(root, { participants: new Map(), working: new Set() });
      return result.get(root)!;
    };
    const identity = (kind: "human" | "agent", id: string, name?: string) =>
      participants.find(member => member.kind === kind && member.id === id)
        ?? { kind, id, displayName: name || "Unknown author", mentionHandle: null };
    for (const [root, replies] of repliesByRoot) {
      for (const [key, author] of replies.authors) {
        if (author.kind !== "system") entry(root).participants.set(key, identity(author.kind, author.id, author.displayName));
      }
    }
    for (const [root, agentId] of JSON.parse(workingThreadKey) as [string, string][]) {
      const agent = identity("agent", agentId, "Agent");
      entry(root).participants.set(`agent:${agentId}`, agent);
      entry(root).working.add(agent.displayName);
    }
    return result;
  }, [repliesByRoot, participants, workingThreadKey]);
  const newLineSeq = readSeqAtOpen ?? null;
  const ownSeqs = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId).map((message) => message.messageSeq);
  const ownLastSentSeq = ownSeqs.length ? Math.max(...ownSeqs) : null;
  // Only a main-list (non-thread) search result with no thread open is a target
  // for RoomTimeline -- a thread-scoped result is handled by the effect below.
  const activeSearchTarget = searchTarget && !searchTarget.threadRootMessageId && !selection ? searchTarget : null;
  const parentMessage = selection?.kind === "thread" ? messages.find((message) => message.messageId === selection.id) : undefined;
  const showResources = selection?.kind === "resources" && !!resourceSource;
  const showTasks = selection?.kind === "tasks" && !!onLinkToWork;
  const showDetail = !!(parentMessage || showResources || showTasks);
  const showContext = showDetail || !contextCollapsed;
  const activeRuns = runs.filter((run) => run.state === "running");
  // Any author: a human naming ISO-14 references it as much as an agent does.
  // ponytail: loaded pages only; a task named only in unloaded history is missing until scrolled in.
  const taskIds = useMemo(() => [...new Set(messages.flatMap((message) =>
    message.deletedAt || message.author.kind === "system" ? [] : taskIdentifiers(message.text)))], [messages]);
  const roomTasks = useReferencedTasks(onLinkToWork ? taskIds : NO_TASK_IDS);
  const viewer = participants.find((participant) => participant.kind === "human" && participant.id === currentUserId);
  const canAddMembers = visibleRoom?.kind === "channel" && !!onAddMember && !visibleRoom.shared;
  const isRoomAdmin = viewer?.role === "owner" || viewer?.role === "admin";
  const canManageMembers = visibleRoom?.kind === "channel" && isRoomAdmin;
  const canEditRoom = visibleRoom?.kind !== "dm" && isRoomAdmin;
  const canLeaveRoom = visibleRoom?.kind === "channel" && viewer?.role !== "owner" && !visibleRoom?.shared;
  const removalTarget = management
    ? participants.find((member) => member.kind === management.kind && member.id === management.id && member.role !== "owner")
    : undefined;

  function openManagement(action: MemberRef, button: HTMLButtonElement) {
    managementTrigger.current = button;
    setManagementError(undefined);
    setManagement(action);
  }

  async function saveManagement() {
    if (!actionable || managementPending) return;
    if (!canManageMembers || !onRemoveMember || !removalTarget) return;
    setManagementPending(true);
    setManagementError(undefined);
    try {
      await onRemoveMember({ kind: removalTarget.kind, id: removalTarget.id });
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
    // Opening only steals focus into the panel on narrow layouts (it docks
    // beside the conversation at wider widths, so nothing forces focus off
    // whatever the viewer was already doing). Closing always has to put
    // focus somewhere, though: the <aside> unmounts unconditionally at every
    // width, and with no explicit target here the browser drops focus to
    // <body> -- so the trigger gets it back regardless of viewport.
    if (!contextCollapsed && isNarrow && !selection) contextHeading.current?.focus({ preventScroll: true });
    else if (contextCollapsed && contextWasOpen.current && !selection) contextTrigger.current?.focus({ preventScroll: true });
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
    if (!showDetail || showResources) return;
    // An open dialog owns Escape — dismissing the overlay underneath it as
    // well would close two things on one keypress. A Radix popper (menu,
    // emoji picker) is portalled out of this subtree, so its own
    // Escape reaches this window listener too and needs the same exemption.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !hasBlockingShortcutDialog()
        && !(event.target as Element | null)?.closest?.("[data-radix-popper-content-wrapper]")) closeOverlay();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeOverlay, showDetail, showResources]);

  const openDetail = useCallback((kind: "thread" | "resources" | "tasks", targetId: string, button: HTMLButtonElement, fromThread = false) => {
    if (!fromThread) trigger.current = button;
    onSelectThread?.(kind === "thread" ? targetId : null);
    setSelection({ kind, id: targetId });
  }, [onSelectThread]);

  const toggleReaction = useCallback(async (message: RoomMessage, emoji: string) => {
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
  }, [onSetReaction, actionable, reactionPending, currentUserId]);

  const renderMessage = useCallback((message: RoomMessage, inThread = false, collapsed = false) => {
    const participant = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id);
    const mentionsMe = message.mentions.some((mention) => mention.kind === "human" && mention.id === currentUserId);
    const replies = inThread ? undefined : repliesByRoot.get(message.messageId);
    return <RoomMessageItem key={message.messageId} id={`${id}-${inThread ? "thread" : "main"}-${message.messageId}`}
      message={message} text={linkifyMentions(message.text, message.mentions, participants)} participant={participant} currentUserId={currentUserId} actionable={actionable} inThread={inThread} collapsed={collapsed}
      startEditing={!inThread && editTarget === message.messageId}
      onEditingClosed={() => setEditTarget((current) => current === message.messageId ? null : current)}
      replyCount={replies?.count ?? 0} lastReplyAt={replies?.latest.createdAt}
      threadParticipants={[...(threadActivity.get(message.messageId)?.participants.values() ?? [])]}
      workingNames={[...(threadActivity.get(message.messageId)?.working ?? [])]}
      highlighted={inThread && searchTarget?.messageId === message.messageId} mentionsViewer={mentionsMe} markdown={roomMarkdown} urlTransform={roomUrlTransform}
      quickReactions={quickReactions} reactionPending={reactionPending !== null} reactionError={reactionError === message.messageId}
      onToggleReaction={onSetReaction && !message.deletedAt ? (emoji) => void toggleReaction(message, emoji) : undefined}
      onOpenThread={(button) => openDetail("thread", message.messageId, button)}
      onSelectParticipant={onSelectParticipant} onUpdateMessage={onUpdateMessage} onDownloadAttachment={onDownloadAttachment}
      menuExtras={!message.deletedAt && (onSetPin || (onMarkUnreadFrom && !inThread)) ? <>
        {/* disabled/aria-busy reflect messageActionPending -- shared across every message's
            menu here, same one-pending-pair-per-surface shape as useAsyncAction's doc comment.
            The menu itself still closes on select (Radix default); reopening it while a
            request is in flight is what surfaces the disabled/pending state, so the request's
            eventual error stays visible (never trapped behind an aria-hidden open menu). */}
        {onSetPin && <DropdownMenuItem disabled={messageActionPending} aria-busy={messageActionPending} onSelect={() => void runMessageAction(
          () => onSetPin(message.messageId, !pinnedMessageIds.includes(message.messageId)),
          "The pin could not be saved.",
        )}>{messageActionPending ? (pinnedMessageIds.includes(message.messageId) ? "Unpinning…" : "Pinning…")
          : (pinnedMessageIds.includes(message.messageId) ? "Unpin from conversation" : "Pin to conversation")}</DropdownMenuItem>}
        {onMarkUnreadFrom && !inThread && <DropdownMenuItem disabled={messageActionPending} aria-busy={messageActionPending} onSelect={() => void runMessageAction(
          () => onMarkUnreadFrom(message.messageSeq),
          "The conversation could not be marked unread.",
        )}>{messageActionPending ? "Marking unread…" : "Mark unread from here"}</DropdownMenuItem>}
      </> : undefined}
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
  }, [participants, currentUserId, repliesByRoot, threadActivity, id, actionable, editTarget, searchTarget,
    quickReactions, reactionPending, reactionError, onSetReaction, toggleReaction, openDetail,
    onSelectParticipant, onUpdateMessage, onDownloadAttachment, onSetPin, onMarkUnreadFrom,
    messageActionPending, runMessageAction, pinnedMessageIds, copied, copyError, roomId]);
  const renderMainMessage = useCallback((message: RoomMessage, collapsed: boolean) =>
    renderMessage(message, false, collapsed), [renderMessage]);

  function renderRun(run: RoomRunView) {
    const agent = participants.find((participant) => participant.kind === "agent" && participant.id === run.agentId);
    const agentName = agent?.displayName ?? "Agent";
    if (run.state !== "running" && !run.text) {
      const label = run.state === "completed" ? `${agentName} finished · no reply` : run.state === "aborted" ? `${agentName} stopped` : `${agentName}'s run failed`;
      return <div key={`${run.roomId}:${run.agentId}:${run.runId}`} className={cn("room-run-line", run.state === "failed" ? "text-err" : "text-muted-foreground")} role="status">
        <Circle size={12} aria-hidden="true" />{label}
        {/* No "What it did" wrapper: the trace folds itself to one line, and a
            disclosure around a disclosure put that line behind two clicks. */}
        {run.tools.length > 0 && <div className="room-run-trace"><ToolActivityTrace calls={run.tools} /></div>}
      </div>;
    }
    const workState = run.state === "running" ? "working" : run.state === "aborted" ? "stopped" : run.state;
    return <article key={`${run.roomId}:${run.agentId}:${run.runId}`} className="room-message room-live-run" aria-label={`Live run from ${agentName}`}>
      <Identity name={agentName} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{agentName}</span><WorkStatus state={workState} /></div>
        {run.thinking && <QuietDisclosure label="Thinking" className="mb-1"><p className="room-wrap text-muted-foreground">{run.thinking}</p></QuietDisclosure>}
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

  const counterparts = participants.filter((participant) => participant.kind !== "human" || participant.id !== currentUserId);
  const roomName = roomDisplayName(visibleRoom, counterparts);
  return <section ref={panelRef} style={column.style}
    className={cn("room-panel", showContext && "room-has-context", showDetail ? "room-has-detail" : "room-has-default-context")} aria-label={roomName}>
    <div className="room-conversation" hidden={showContext && isNarrow} onClickCapture={closeOverlay} onFocusCapture={closeOverlay}>
      <RoomHeader room={visibleRoom} roomName={roomName} counterparts={counterparts} participantCount={participants.length}
        canEditRoom={canEditRoom} canLeave={canLeaveRoom} actionable={actionable} pinnedCount={pinnedMessageIds.length}
        search={<RoomMessageSearch roomId={roomId} messages={messages} participants={participants}
          loading={historyLoading} complete={historyComplete} error={historyError} errorMessage={historyErrorMessage}
          onLoad={onLoadHistory}
          onSelect={(message) => {
            const root = message.threadRootMessageId;
            // Otherwise the [selection] effect's else-branch focuses this stale trigger
            // (e.g. a "Reply in thread" button) once selection clears below, racing
            // RoomTimeline's own focus of the newly targeted main message.
            trigger.current = null;
            // Same race against the [contextCollapsed] effect below: if the room
            // details panel happens to be open, closing it here must not steal
            // focus back to the Details tab once RoomTimeline focuses this result.
            contextWasOpen.current = false;
            onSelectThread?.(root);
            setSelection(root ? { kind: "thread", id: root } : null);
            setContextCollapsed(true);
            setSearchTarget({ messageId: message.messageId, threadRootMessageId: root, token: ++searchTargetToken.current });
          }} />}
        onOpenDetails={(tab) => { closeDetail(); if (tab) setDetailsTab(tab); setContextCollapsed(false); }}
        onSetPrefs={onSetPrefs} onRenameRoom={onRenameRoom} onSetTopic={onSetTopic} onLeaveRoom={onLeaveRoom}
        onSetArchived={onSetArchived} />
      <nav className="room-tabs" aria-label="Conversation views">
        <button type="button" className="room-tab" aria-current={contextCollapsed && !showDetail ? "page" : undefined}
          onClick={() => {
            closeDetail();
            // Same [contextCollapsed]-effect race as the search/Jump call
            // sites above: this button closes the room details panel too,
            // and the click itself should keep this button focused, not get
            // yanked back onto the Details trigger.
            contextWasOpen.current = false;
            setContextCollapsed(true);
          }}>{visibleRoom?.kind === "channel" ? "Channel" : "Conversation"}</button>
        <button ref={contextTrigger} type="button" className="room-tab"
          aria-expanded={!contextCollapsed} aria-current={!contextCollapsed && !showDetail ? "page" : undefined}
          onClick={() => { closeDetail(); setContextCollapsed(!contextCollapsed); }}>Details{pinnedMessageIds.length > 0 && <span className="room-count">{pinnedMessageIds.length}</span>}</button>
        {onLinkToWork && <button type="button" className="room-tab" aria-current={showTasks ? "page" : undefined}
          onClick={event => openDetail("tasks", "tasks", event.currentTarget)}>Tasks{roomTasks.length > 0 && ` · ${roomTasks.length}`}</button>}
        {resourceSource && (showResourcesTab || showResources) && <button type="button" className="room-tab" aria-current={showResources ? "page" : undefined}
          onClick={event => openDetail("resources", "resources", event.currentTarget)}>Devices & sessions</button>}
      </nav>
      {(state === "reconnecting" || !liveConnected) && <div className="room-state"><p role="status" aria-label="Room status">Reconnecting live updates. You can keep sending messages.</p>
        {onRetry && <Button variant="outline" onClick={onRetry}>Check for updates</Button>}</div>}
      <div ref={historyRef} className="contents">
        <RoomTimeline roomId={roomId} messages={mainMessages} readSeq={sentSinceOpen ? null : newLineSeq} ownLastSentSeq={ownLastSentSeq}
          historyComplete={historyComplete} historyLoading={historyLoading} historyError={historyErrorMessage} onLoadOlder={onLoadOlder}
          targetMessageId={targetMessageId ?? (activeSearchTarget ? activeSearchTarget.messageId : null)}
          targetToken={targetMessageId ? undefined : activeSearchTarget?.token}
          renderMessage={renderMainMessage}
          leading={historyComplete ? <>
            <RoomIntro room={visibleRoom} roomName={roomName} participants={participants} />
            {/* Starter channel only, identified by the backend's `isDefault` —
                the room is renameable, so "general" is not the test. */}
            {visibleRoom?.isDefault && <OnboardingBand participants={participants} />}
          </> : null}
          empty={!historyComplete ? <p className="room-empty">Load older messages to see the start of this conversation.</p> : null}>
          {pendingMessages.filter((message) => message.threadRootMessageId === null).map((message) => <article key={message.clientMessageId} className="room-message" aria-label={`Pending message from ${viewer?.displayName ?? "You"}`}>
            <Identity name={viewer?.displayName ?? "You"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{viewer?.displayName ?? "You"}</span><span className="text-sm text-muted-foreground">Sending…</span></div><MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{linkifyMentions(message.text, message.mentions, participants)}</MarkdownView></div>
          </article>)}
          {runs.filter((run) => !run.threadRootMessageId).map(renderRun)}
        </RoomTimeline>
      </div>
      <p className="room-typing text-sm text-muted-foreground" role="status">{typingMembers.length > 0 ? `${typingMembers.slice(0, 3).map((member) => member.displayName).join(", ")}${typingMembers.length > 3 ? " and others" : ""} ${typingMembers.length === 1 ? "is" : "are"} typing…` : ""}</p>
      {copied && <p role="status" aria-label="Link copied" className="sr-only">Link copied</p>}
      {messageActionError && <p role="alert" className="room-typing text-sm text-err">{messageActionError}</p>}
      {visibleRoom?.archivedAt ? <div className="room-composer room-archived" role="status">
        <p className="text-sm text-muted-foreground">
          {isRoomAdmin && onSetArchived ? "This channel is archived. Unarchive it to post." : "This channel is archived."}
        </p>
        {isRoomAdmin && onSetArchived && <Button type="button" size="sm" variant="outline" disabled={!actionable || messageActionPending}
          onClick={() => void runMessageAction(() => onSetArchived(false), "The channel could not be unarchived.")}>Unarchive channel</Button>}
      </div> : <RoomComposer {...composer}
        onSubmit={markSent(composer.onSubmit)}
        onRetry={composer.onRetry ? markSent(composer.onRetry) : undefined}
        onEditLast={onUpdateMessage ? () => {
          const own = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId && !message.deletedAt);
          setEditTarget(own.length ? own[own.length - 1].messageId : null);
        } : undefined}
        disabled={!actionable || composer.disabled}
        dropTargetRef={historyRef} />}
    </div>
    {showContext && !showDetail && <aside className="room-context" aria-label="Room details"
      onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setContextCollapsed(true); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={() => setContextCollapsed(true)}><ArrowLeft aria-hidden="true" /></Button><h3 ref={contextHeading} tabIndex={-1} className="font-semibold">Room details</h3></header>
      <RoomDetailsPanel room={visibleRoom} roomName={roomName} tab={detailsTab} onSelectTab={setDetailsTab}
        participants={participants} actionable={actionable}
        messages={messages} pinnedMessageIds={pinnedMessageIds} activeRuns={activeRuns}
        onUnpin={onSetPin ? (messageId) => onSetPin(messageId, false) : undefined}
        onLeaveRoom={onLeaveRoom && canLeaveRoom ? onLeaveRoom : undefined}
        // Reuses the shipped search scroll/highlight path rather than adding a second one.
        onOpenMessage={(messageId) => {
          // Jumping here always closes this very panel -- skip the generic
          // [contextCollapsed] close-focus-restore (below) so it doesn't fight
          // RoomTimeline's own focus of the message just jumped to.
          contextWasOpen.current = false;
          setContextCollapsed(true);
          setSearchTarget({ messageId, threadRootMessageId: null, token: ++searchTargetToken.current });
        }}
        members={<section aria-labelledby={`${id}-context-members`}>
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
            {canManageMembers && onRemoveMember && participant.role !== "owner" && !(visibleRoom?.shared && participant.kind === "human") && <Button type="button" variant="ghost" size="sm" disabled={!actionable}
              aria-label={`Remove ${participant.displayName}`} onClick={(event) => openManagement({ kind: participant.kind, id: participant.id }, event.currentTarget)}>Remove</Button>}
            {participant.kind === "agent" && <AgentResponse participant={participant} selectId={`${id}-context-${participant.id}-mode`} onChange={isRoomAdmin || listeningAvailable ? onListenModeChange : undefined} listeningAvailable={listeningAvailable} />}
          </li>)}</ul>
          {isRoomAdmin && onListenModeChange && !listeningAvailable && <p className="text-sm text-muted-foreground">
            Listening lets an agent reply without being mentioned. It is part of the paid plan. <NextLink href="/settings?panel=billing">See plans</NextLink></p>}
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
        </section>} />
    </aside>}
    {showResources && <RoomResources key={JSON.stringify([ownerScopeId, currentUserId, roomId, participants.filter(p => p.kind === "human").map(p => p.id).sort()])}
      roomId={roomId} source={resourceSource!} participants={participants} currentUserId={currentUserId} onClose={closeDetail}
      target={targetSession?.nonce !== consumedSession ? targetSession : null}
      onTargetConsumed={() => setConsumedSession(targetSession?.nonce ?? null)}
      resizeHandle={<ColumnGrip {...column.grip} />}
      onShare={url => composer.onTextChange(composer.text ? `${composer.text}\n${url}` : url)} />}
    {showTasks && <aside className="room-context" aria-label="Tasks" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold">Tasks</h3></header>
      <div className="room-context-body"><RoomTasks tasks={roomTasks} onOpenAll={onLinkToWork!} /></div>
    </aside>}
    {showDetail && !showResources && !showTasks && <aside className="room-context" aria-label="Thread" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold room-wrap">Thread</h3></header>
      <div className="room-context-body">{parentMessage ? <>{renderMessage(parentMessage, true)}
        <div className="room-thread-replies" aria-label="Thread replies">{buildTimeline(messages.filter((message) => message.threadRootMessageId === parentMessage.messageId), { readSeq: null }).map((item) => item.kind === "message" ? renderMessage(item.message, true, item.collapsed) : null)}
          {runs.filter((run) => run.threadRootMessageId === parentMessage.messageId).map(renderRun)}
        </div>
      </> : null}</div>
      {parentMessage && threadComposer?.parentMessageId === parentMessage.messageId && <RoomComposer {...threadComposer.composer} label="Reply to thread" disabled={!actionable || !!visibleRoom?.archivedAt || threadComposer.composer.disabled} />}
    </aside>}
    <Dialog open={management !== null && canManageMembers} onOpenChange={(open) => { if (!open && !managementPending) setManagement(null); }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { event.preventDefault(); managementCancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); (managementTrigger.current?.isConnected ? managementTrigger.current : contextHeading.current)?.focus(); }}>
        <DialogHeader><DialogTitle>Remove member</DialogTitle>
          <DialogDescription className="break-words">{`Remove ${removalTarget?.displayName ?? "this member"} from ${roomName}? They will lose access to this channel and its messages.`}</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); void saveManagement(); }}>
          {managementError && <p role="alert" className="text-sm text-err break-words">{managementError}</p>}
          <DialogFooter className="gap-2"><Button ref={managementCancel} type="button" variant="outline" disabled={managementPending} onClick={() => setManagement(null)}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={!actionable || managementPending || !removalTarget}>
              {managementPending ? "Removing…" : "Remove member"}
            </Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </section>;
}
